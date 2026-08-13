package kills;

import skadistats.clarity.model.CombatLogEntry;
import skadistats.clarity.model.Entity;
import skadistats.clarity.model.FieldPath;
import skadistats.clarity.processor.entities.Entities;
import skadistats.clarity.processor.entities.OnEntityCreated;
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
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;

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

    private static final Map<Integer, String> KNOWN_RUNE_TYPES = Map.of(
        0, "double_damage",
        1, "haste",
        2, "illusion",
        3, "invisibility",
        4, "regeneration",
        5, "bounty",
        6, "arcane",
        7, "water",
        8, "wisdom",
        9, "shield"
    );
    // Bounty, water, and current wisdom/XP runes are not river power-rune
    // markets. Unknown future enum ids are still surfaced as rune_<id>.
    private static final Set<Integer> EXCLUDED_RUNE_TYPES = Set.of(5, 7, 8);
    private static final Map<String, String> RUNE_MODIFIER_TYPES = Map.of(
        "modifier_rune_doubledamage", "double_damage",
        "modifier_rune_haste", "haste",
        "modifier_rune_illusion", "illusion",
        "modifier_rune_invis", "invisibility",
        "modifier_rune_regen", "regeneration",
        "modifier_rune_arcane", "arcane",
        "modifier_rune_shield", "shield"
    );

    // Prefer the replay entity's m_szLocation="top"/"bot" when present. The
    // coordinates are only a fallback for older schemas or missing location
    // strings, and keep current behavior for known map points.
    // Calibrated from replay 8878081113: raw CBodyComponent coordinates are
    // centered by subtracting 16384. Observed river rune entity positions were
    // exactly top=(-1640, 1112), bot=(1180, -1216), matching known map points.
    private static final float MAP_ORIGIN_OFFSET = 16384.0f;
    private static final float TOP_RUNE_X = -1640.0f;
    private static final float TOP_RUNE_Y = 1112.0f;
    private static final float BOT_RUNE_X = 1180.0f;
    private static final float BOT_RUNE_Y = -1216.0f;
    private static final double MAX_POWER_RUNE_SPOT_DISTANCE = 600.0;
    private static final int FIRST_POWER_RUNE_SPAWN_SECONDS = 360;
    private static final int POWER_RUNE_SPAWN_INTERVAL_SECONDS = 120;
    private static final float RUNE_RECOVERY_WINDOW_SECONDS = 3.0f;
    private static final boolean RUNE_DEBUG = "1".equals(System.getenv("RUNE_DEBUG"));

    private final Set<Integer> emittedRuneHandles = new HashSet<>();
    private final Set<Integer> observedRuneSpawnSeconds = new HashSet<>();
    // A rune bottled and used between demo snapshots may never surface as a
    // CDOTA_Item_Rune entity. Keep tightly constrained combat-log candidates,
    // then use them only for Spawn Times that have no entity observation.
    private final Map<Integer, BufferedRuneFallback> runeFallbacks = new HashMap<>();

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

    static record RuneFallbackCandidate(int spawnSecond, String runeType, String side, float gameTime) {}

    private static record BufferedRuneFallback(RuneFallbackCandidate candidate, float rawTime) {}

    private void bufferEvent(float rawTime, String json) {
        buffer.add(new RawEvent(rawTime, json));
    }

    // ── Combat log listener ──────────────────────────────────────────────────

    @OnCombatLogEntry
    public void onCombatLogEntry(Context ctx, CombatLogEntry cle) throws Exception {

        currentRawTime = cle.getTimestamp();

        // === Detect game start (state 5) to capture pre-game offset ===
        if (cle.getType() == DOTA_COMBATLOG_TYPES.DOTA_COMBATLOG_GAME_STATE) {
            if (cle.getValue() == 5) {
                gameStartTime = cle.getTimestamp();
            }
            return;
        }

        maybeRecoverInstantRunePickup(ctx, cle);

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

    private void maybeRecoverInstantRunePickup(Context ctx, CombatLogEntry cle) {
        if (cle.getType() != DOTA_COMBATLOG_TYPES.DOTA_COMBATLOG_MODIFIER_ADD
                || !cle.hasInflictorName() || !cle.hasTargetName()
                || !RUNE_MODIFIER_TYPES.containsKey(cle.getInflictorName())) {
            return;
        }

        Entity hero = findHeroEntity(ctx, cle.getTargetName());
        if (hero == null) {
            return;
        }

        Float rawX = getCoordinate(hero, "CBodyComponent.m_cellX", "CBodyComponent.m_vecX");
        Float rawY = getCoordinate(hero, "CBodyComponent.m_cellY", "CBodyComponent.m_vecY");
        Float worldX = rawX == null ? null : rawX - MAP_ORIGIN_OFFSET;
        Float worldY = rawY == null ? null : rawY - MAP_ORIGIN_OFFSET;
        float gameTime = cle.getTimestamp() - gameStartTime;
        RuneFallbackCandidate candidate = recoverRuneFromModifier(
            cle.getInflictorName(), gameTime, worldX, worldY
        );
        if (candidate == null) {
            return;
        }

        BufferedRuneFallback fallback = new BufferedRuneFallback(candidate, cle.getTimestamp());
        runeFallbacks.merge(candidate.spawnSecond(), fallback, (existing, replacement) -> {
            float existingDrift = Math.abs(existing.candidate().gameTime() - existing.candidate().spawnSecond());
            float replacementDrift = Math.abs(replacement.candidate().gameTime() - replacement.candidate().spawnSecond());
            return replacementDrift < existingDrift ? replacement : existing;
        });
    }

    static RuneFallbackCandidate recoverRuneFromModifier(
            String modifierName, float gameTime, Float worldX, Float worldY) {
        String runeType = RUNE_MODIFIER_TYPES.get(modifierName);
        Integer spawnSecond = powerRuneSpawnSecond(gameTime);
        if (runeType == null || spawnSecond == null) {
            return null;
        }

        String side = classifyPowerRuneSide(null, worldX, worldY);
        if (side == null) {
            return null;
        }
        return new RuneFallbackCandidate(spawnSecond, runeType, side, gameTime);
    }

    private static Integer powerRuneSpawnSecond(float gameTime) {
        int nearest = Math.round(gameTime / POWER_RUNE_SPAWN_INTERVAL_SECONDS)
            * POWER_RUNE_SPAWN_INTERVAL_SECONDS;
        if (nearest < FIRST_POWER_RUNE_SPAWN_SECONDS
                || Math.abs(gameTime - nearest) > RUNE_RECOVERY_WINDOW_SECONDS) {
            return null;
        }
        return nearest;
    }

    private static Entity findHeroEntity(Context ctx, String combatLogName) {
        String normalizedName = normalizeHeroName(combatLogName);
        if (normalizedName.isEmpty()) {
            return null;
        }
        return ctx.getProcessor(Entities.class).getByPredicate(entity -> {
            String dtName = entity.getDtClass().getDtName();
            return entity.isExistent()
                && dtName.startsWith("CDOTA_Unit_Hero_")
                && !getBooleanProperty(entity, "m_bIsIllusion")
                && normalizeHeroName(dtName).equals(normalizedName);
        });
    }

    private static String normalizeHeroName(String name) {
        return name
            .replace("npc_dota_hero_", "")
            .replace("CDOTA_Unit_Hero_", "")
            .replace("_", "")
            .toLowerCase(Locale.ROOT);
    }

    // ── Entity listeners: rune spawns + Aegis pickup from hero inventory ─────

    @OnEntityCreated(classPattern = "CDOTA_Item_Rune")
    public void onRuneCreated(Entity e) {
        maybeEmitRune(e);
    }

    private void maybeEmitRune(Entity e) {
        String dtName = e.getDtClass().getDtName();
        if (!"CDOTA_Item_Rune".equals(dtName) || emittedRuneHandles.contains(e.getHandle())) {
            return;
        }

        Integer runeType = getIntegerProperty(e, "m_iRuneType");
        if (runeType == null) {
            return;
        }
        String runeTypeName = KNOWN_RUNE_TYPES.getOrDefault(runeType, "rune_" + runeType);
        Float rawX = getCoordinate(e, "CBodyComponent.m_cellX", "CBodyComponent.m_vecX");
        Float rawY = getCoordinate(e, "CBodyComponent.m_cellY", "CBodyComponent.m_vecY");
        Float runeTime = getFloatProperty(e, "m_flRuneTime");
        String location = getStringProperty(e, "m_szLocation");
        Float worldX = rawX == null ? null : rawX - MAP_ORIGIN_OFFSET;
        Float worldY = rawY == null ? null : rawY - MAP_ORIGIN_OFFSET;

        if (RUNE_DEBUG) {
            System.err.printf(
                "RUNE handle=%d type=%s type_name=%s location=%s raw=(%s,%s) world=(%s,%s) m_flRuneTime=%s currentGameTime=%.3f%n",
                e.getHandle(), String.valueOf(runeType), runeTypeName, String.valueOf(location),
                String.valueOf(rawX), String.valueOf(rawY),
                worldX == null ? "null" : String.format("%.1f", worldX),
                worldY == null ? "null" : String.format("%.1f", worldY),
                String.valueOf(runeTime), currentRawTime - gameStartTime
            );
        }

        if (EXCLUDED_RUNE_TYPES.contains(runeType)) {
            return;
        }

        String side = classifyPowerRuneSide(location, worldX, worldY);
        if (side == null) {
            return;
        }

        emittedRuneHandles.add(e.getHandle());
        Integer spawnSecond = powerRuneSpawnSecond(currentRawTime - gameStartTime);
        if (spawnSecond != null) {
            observedRuneSpawnSeconds.add(spawnSecond);
        }
        bufferEvent(currentRawTime, String.format(
            "{\"type\":\"rune\",\"rune_type\":\"%s\",\"side\":\"%s\",\"time\":%%TIME%%,\"time_f\":%%TIMEF%%}",
            runeTypeName, side
        ));
    }

    private static String classifyPowerRuneSide(String location, Float worldX, Float worldY) {
        if (location != null) {
            String normalized = location.trim().toLowerCase();
            if (normalized.equals("top") || normalized.equals("bot")) {
                return normalized;
            }
        }
        if (worldX == null || worldY == null) {
            return null;
        }
        double topDistance = Math.hypot(worldX - TOP_RUNE_X, worldY - TOP_RUNE_Y);
        double botDistance = Math.hypot(worldX - BOT_RUNE_X, worldY - BOT_RUNE_Y);
        double nearestDistance = Math.min(topDistance, botDistance);
        if (nearestDistance > MAX_POWER_RUNE_SPOT_DISTANCE) {
            return null;
        }
        return topDistance <= botDistance ? "top" : "bot";
    }

    private static Integer getIntegerProperty(Entity e, String propertyName) {
        try {
            Object value = e.getProperty(propertyName);
            if (value instanceof Number) {
                return ((Number) value).intValue();
            }
        } catch (Exception ex) {
            // unavailable on this tick/entity
        }
        return null;
    }

    private static Float getFloatProperty(Entity e, String propertyName) {
        try {
            Object value = e.getProperty(propertyName);
            if (value instanceof Number) {
                return ((Number) value).floatValue();
            }
        } catch (Exception ex) {
            // unavailable on this tick/entity
        }
        return null;
    }

    private static String getStringProperty(Entity e, String propertyName) {
        try {
            Object value = e.getProperty(propertyName);
            if (value instanceof String) {
                return (String) value;
            }
        } catch (Exception ex) {
            // unavailable on this tick/entity
        }
        return null;
    }

    private static boolean getBooleanProperty(Entity e, String propertyName) {
        try {
            Object value = e.getProperty(propertyName);
            if (value instanceof Boolean) {
                return (Boolean) value;
            }
            if (value instanceof Number) {
                return ((Number) value).intValue() != 0;
            }
        } catch (Exception ex) {
            // unavailable on this tick/entity
        }
        return false;
    }

    private static Float getCoordinate(Entity e, String cellProperty, String vecProperty) {
        Integer cell = getIntegerProperty(e, cellProperty);
        Float vec = getFloatProperty(e, vecProperty);
        if (cell == null) {
            return null;
        }
        return cell * 128.0f + (vec == null ? 0.0f : vec);
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
        if ("CDOTA_Item_Rune".equals(dtName)) {
            maybeEmitRune(e);
            return;
        }
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
        for (BufferedRuneFallback fallback : runeFallbacks.values()) {
            RuneFallbackCandidate candidate = fallback.candidate();
            if (observedRuneSpawnSeconds.add(candidate.spawnSecond())) {
                bufferEvent(fallback.rawTime(), String.format(
                    "{\"type\":\"rune\",\"rune_type\":\"%s\",\"side\":\"%s\",\"time\":%%TIME%%,\"time_f\":%%TIMEF%%}",
                    candidate.runeType(), candidate.side()
                ));
            }
        }
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
