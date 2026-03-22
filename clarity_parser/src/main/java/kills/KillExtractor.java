package kills;

import skadistats.clarity.model.CombatLogEntry;
import skadistats.clarity.processor.gameevents.OnCombatLogEntry;
import skadistats.clarity.processor.runner.SimpleRunner;
import skadistats.clarity.source.MappedFileSource;
import skadistats.clarity.wire.dota.common.proto.DOTAUserMessages.DOTA_COMBATLOG_TYPES;

import java.io.OutputStreamWriter;
import java.io.PrintWriter;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;

public class KillExtractor {

    private final PrintWriter out;

    // Pre-game offset: the raw timestamp when game state transitions to 5
    // (in-game clock = 0:00, creeps spawn). Subtracted from all event timestamps.
    private float gameStartTime = 0.0f;

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

    @OnCombatLogEntry
    public void onCombatLogEntry(CombatLogEntry cle) throws Exception {

        // === Detect game start (state 5) to capture pre-game offset ===
        if (cle.getType() == DOTA_COMBATLOG_TYPES.DOTA_COMBATLOG_GAME_STATE) {
            if (cle.getValue() == 5) {
                gameStartTime = cle.getTimestamp();
            }
            return;
        }

        // === Aegis pickup ===
        // DOTA_COMBATLOG_MODIFIER_ADD with modifier_aegis_regen fires when a hero picks up
        // the Aegis of the Immortal. (DOTA_COMBATLOG_AEGIS_TAKEN exists in the protobuf
        // schema but Clarity 3.1.3 never emits it in practice.)
        if (cle.getType() == DOTA_COMBATLOG_TYPES.DOTA_COMBATLOG_MODIFIER_ADD) {
            String inflictor = cle.getInflictorName();
            if ("modifier_aegis_regen".equals(inflictor)) {
                String heroName = cle.getTargetName();
                if (heroName == null) heroName = "";
                int team = cle.getTargetTeam();
                if (team == 2 || team == 3) {
                    bufferEvent(cle.getTimestamp(), String.format(
                        "{\"type\":\"aegis\",\"killer_team\":%d,\"target\":\"%s\",\"time\":%%TIME%%,\"time_f\":%%TIMEF%%}",
                        team, heroName
                    ));
                }
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
        // Match only the actual Roshan NPC, not Roshan's Banner (npc_dota_unit_roshans_banner).
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
