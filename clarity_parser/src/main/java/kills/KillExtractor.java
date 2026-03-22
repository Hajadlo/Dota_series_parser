package kills;

import skadistats.clarity.model.CombatLogEntry;
import skadistats.clarity.model.Entity;
import skadistats.clarity.model.FieldPath;
import skadistats.clarity.processor.entities.Entities;
import skadistats.clarity.processor.entities.OnEntityUpdated;
import skadistats.clarity.processor.entities.UsesEntities;
import skadistats.clarity.processor.gameevents.OnCombatLogEntry;
import skadistats.clarity.processor.runner.Context;
import skadistats.clarity.processor.runner.SimpleRunner;
import skadistats.clarity.source.MappedFileSource;
import skadistats.clarity.wire.dota.common.proto.DOTAUserMessages.DOTA_COMBATLOG_TYPES;

import java.io.OutputStreamWriter;
import java.io.PrintWriter;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;

@UsesEntities
public class KillExtractor {

    private final PrintWriter out;

    // Pre-game offset: the raw timestamp when game state transitions to 5
    // (in-game clock = 0:00, creeps spawn). Subtracted from all event timestamps.
    private float gameStartTime = 0.0f;

    // Approximate current raw time, updated from each combat log entry.
    // Used to timestamp entity-based events (e.g. aegis pickup from inventory).
    private float currentRawTime = 0.0f;

    // Buffer all events with raw timestamps during parsing. After the replay is
    // fully processed (gameStartTime is known), we compute corrected in-game
    // timestamps and flush everything. This handles kills that happen before
    // game state 5 fires (e.g. pre-horn first blood) — they get negative times.
    private final List<RawEvent> buffer = new ArrayList<>();

    public KillExtractor() {
        this.out = new PrintWriter(new OutputStreamWriter(System.out, StandardCharsets.UTF_8), true);
    }

    /** Buffered event — stores raw timestamp and pre-built JSON template. */
    private static class RawEvent {
        final float rawTime;
        final String jsonTemplate; // contains %TIME% and %TIMEF% placeholders

        RawEvent(float rawTime, String jsonTemplate) {
            this.rawTime = rawTime;
            this.jsonTemplate = jsonTemplate;
        }
    }

    private void bufferEvent(float rawTime, String json) {
        buffer.add(new RawEvent(rawTime, json));
    }

    // ── Combat log listener ──────────────────────────────────────────────────

    @OnCombatLogEntry
    public void onCombatLogEntry(CombatLogEntry cle) throws Exception {

        currentRawTime = cle.getTimestamp();

        // === Detect game start (state 5) to capture pre-game offset ===
        if (cle.getType() == DOTA_COMBATLOG_TYPES.DOTA_COMBATLOG_GAME_STATE) {
            if (cle.getValue() == 5) {
                gameStartTime = cle.getTimestamp();
            }
            return;
        }

        if (cle.getType() != DOTA_COMBATLOG_TYPES.DOTA_COMBATLOG_DEATH) {
            return;
        }

        String targetName = cle.getTargetName();
        if (targetName == null) targetName = "";
        float rawTime = cle.getTimestamp();

        // === Tower deaths ===
        if (targetName.startsWith("npc_dota_goodguys_tower") ||
                targetName.startsWith("npc_dota_badguys_tower")) {
            int lostTeam = targetName.startsWith("npc_dota_goodguys") ? 2 : 3;
            bufferEvent(rawTime, String.format(
                "{\"type\":\"tower\",\"lost_team\":%d,\"target\":\"%s\",\"time\":%%TIME%%,\"time_f\":%%TIMEF%%}",
                lostTeam, targetName
            ));
            return;
        }

        // === Barracks deaths ===
        if (targetName.contains("_rax_") &&
                (targetName.startsWith("npc_dota_goodguys") || targetName.startsWith("npc_dota_badguys"))) {
            int lostTeam = targetName.startsWith("npc_dota_goodguys") ? 2 : 3;
            bufferEvent(rawTime, String.format(
                "{\"type\":\"barracks\",\"lost_team\":%d,\"target\":\"%s\",\"time\":%%TIME%%,\"time_f\":%%TIMEF%%}",
                lostTeam, targetName
            ));
            return;
        }

        // === Roshan kill ===
        if (targetName.equals("npc_dota_roshan")) {
            int killerTeam = cle.getAttackerTeam();
            if (killerTeam == 2 || killerTeam == 3) {
                bufferEvent(rawTime, String.format(
                    "{\"type\":\"roshan\",\"killer_team\":%d,\"target\":\"%s\",\"time\":%%TIME%%,\"time_f\":%%TIMEF%%}",
                    killerTeam, targetName
                ));
            }
            return;
        }

        // === Tormentor kill ===
        if (targetName.contains("miniboss")) {
            int killerTeam = cle.getAttackerTeam();
            if (killerTeam == 2 || killerTeam == 3) {
                bufferEvent(rawTime, String.format(
                    "{\"type\":\"tormentor\",\"killer_team\":%d,\"target\":\"%s\",\"time\":%%TIME%%,\"time_f\":%%TIMEF%%}",
                    killerTeam, targetName
                ));
            }
            return;
        }

        // === Hero kills ===
        if (!cle.isTargetHero()) return;
        if (cle.isTargetIllusion()) return;
        if (cle.isWillReincarnate()) return;

        int attackerTeam = cle.getAttackerTeam();
        if (attackerTeam != 2 && attackerTeam != 3) return;

        int targetTeam = cle.getTargetTeam();
        if (targetTeam != 2 && targetTeam != 3) return;

        String attackerName = cle.getAttackerName();
        if (attackerName == null) attackerName = "";

        // Deny detection
        if (attackerTeam == targetTeam) {
            bufferEvent(rawTime, String.format(
                "{\"type\":\"kill\",\"killer_team\":0,\"is_deny\":true,\"target\":\"%s\",\"attacker\":\"%s\",\"attacker_team_raw\":%d,\"time\":%%TIME%%,\"time_f\":%%TIMEF%%}",
                targetName, attackerName, cle.getAttackerTeam()
            ));
            return;
        }

        int killerTeam = (targetTeam == 2) ? 3 : 2;
        bufferEvent(rawTime, String.format(
            "{\"type\":\"kill\",\"killer_team\":%d,\"target\":\"%s\",\"attacker\":\"%s\",\"attacker_team_raw\":%d,\"time\":%%TIME%%,\"time_f\":%%TIMEF%%}",
            killerTeam, targetName, attackerName, cle.getAttackerTeam()
        ));
    }

    // ── Entity listener: detect Aegis pickup from hero inventory ─────────────
    //
    // When Roshan dies the Aegis item entity (CDOTA_Item_Aegis) is placed into
    // a hero's inventory slot (m_hItems.XXXX). We watch for inventory changes
    // on hero entities, resolve the entity handle, and emit an "aegis" event
    // when the resolved entity is CDOTA_Item_Aegis.

    @OnEntityUpdated
    public void onEntityUpdated(Context ctx, Entity e, FieldPath[] updatedPaths, int updateCount) {
        String dtName = e.getDtClass().getDtName();
        if (!dtName.startsWith("CDOTA_Unit_Hero")) return;

        for (int i = 0; i < updateCount; i++) {
            String pathName;
            try {
                pathName = e.getDtClass().getNameForFieldPath(updatedPaths[i]);
            } catch (Exception ex) {
                continue;
            }
            if (pathName == null || !pathName.startsWith("m_hItems.")) continue;

            Object value;
            try {
                value = e.getPropertyForFieldPath(updatedPaths[i]);
            } catch (Exception ex) {
                continue;
            }
            if (!(value instanceof Number)) continue;

            int handle = ((Number) value).intValue();
            // 16777215 (0xFFFFFF) = invalid/empty, 0 = empty
            if (handle == 16777215 || handle == 0) continue;

            try {
                Entity itemEntity = ctx.getProcessor(Entities.class).getByHandle(handle);
                if (itemEntity != null && "CDOTA_Item_Aegis".equals(itemEntity.getDtClass().getDtName())) {
                    // Read the hero's team from the entity
                    int team = 0;
                    try {
                        FieldPath teamFp = e.getDtClass().getFieldPathForName("m_iTeamNum");
                        if (teamFp != null) {
                            Object teamVal = e.getPropertyForFieldPath(teamFp);
                            if (teamVal instanceof Number) {
                                team = ((Number) teamVal).intValue();
                            }
                        }
                    } catch (Exception ex) {
                        // fall through with team=0
                    }

                    // Convert hero dtName to npc format:
                    // CDOTA_Unit_Hero_Nevermore -> npc_dota_hero_nevermore
                    String heroName = dtName.substring("CDOTA_Unit_Hero_".length()).toLowerCase();
                    heroName = "npc_dota_hero_" + heroName;

                    if (team == 2 || team == 3) {
                        bufferEvent(currentRawTime, String.format(
                            "{\"type\":\"aegis\",\"killer_team\":%d,\"target\":\"%s\",\"time\":%%TIME%%,\"time_f\":%%TIMEF%%}",
                            team, heroName
                        ));
                    }
                    return; // only one aegis pickup per entity update batch
                }
            } catch (Exception ex) {
                // ignore handle resolution errors
            }
        }
    }

    // ── Flush and output ─────────────────────────────────────────────────────

    /** After replay parsing completes, apply gameStartTime offset and print all events. */
    private void flush() {
        for (RawEvent ev : buffer) {
            float gameTime = ev.rawTime - gameStartTime;
            String json = ev.jsonTemplate
                .replace("%TIME%", String.valueOf(Math.round(gameTime)))
                .replace("%TIMEF%", String.format("%.3f", gameTime));
            out.println(json);
        }
    }

    public void run(String[] args) throws Exception {
        new SimpleRunner(new MappedFileSource(args[0])).runWith(this);
        flush();
    }

    public static void main(String[] args) throws Exception {
        if (args.length < 1) {
            System.err.println("Usage: KillExtractor <replay.dem>");
            System.exit(1);
        }
        new KillExtractor().run(args);
    }
}
