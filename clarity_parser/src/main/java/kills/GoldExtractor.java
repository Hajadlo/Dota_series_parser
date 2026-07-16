package kills;

import skadistats.clarity.model.CombatLogEntry;
import skadistats.clarity.model.Entity;
import skadistats.clarity.processor.entities.Entities;
import skadistats.clarity.processor.entities.UsesEntities;
import skadistats.clarity.processor.gameevents.OnCombatLogEntry;
import skadistats.clarity.processor.reader.OnTickEnd;
import skadistats.clarity.processor.runner.Context;
import skadistats.clarity.processor.runner.SimpleRunner;
import skadistats.clarity.source.MappedFileSource;
import skadistats.clarity.wire.dota.common.proto.DOTAUserMessages.DOTA_COMBATLOG_TYPES;

import java.io.OutputStreamWriter;
import java.io.PrintWriter;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;
import java.util.Locale;

/**
 * Samples team net worth and total earned gold at one or more in-game times.
 *
 * Usage: GoldExtractor <replay.dem> [gameTimeSeconds ...]
 *
 * Defaults to 5:00, 10:00, and 15:00. Multiple times can be passed either as
 * separate args (`300 600 900`) or comma-separated (`300,600,900`). The horn is
 * detected from the combat log GAME_STATE=5 event, then subsequent sample timing
 * uses replay ticks instead of waiting for another combat-log event. This keeps
 * quiet games from drifting to the next fight before sampling net worth.
 *
 * Pauses are excluded from the clock via CDOTAGamerulesProxy.m_nTotalPausedTicks
 * so samples land at true in-game 5/10/15:00 (matching the HUD and OpenDota),
 * not wall-clock ticks. Without this, a long early pause would shift every
 * sample minutes earlier and can even flip the reported net worth leader. Net
 * worth is never sampled while the game is paused.
 *
 * Per-player gold comes from the CDOTA_DataRadiant / CDOTA_DataDire entities:
 *   m_vecDataTeam.%04d.m_iNetWorth
 *   m_vecDataTeam.%04d.m_iTotalEarnedGold
 */
@UsesEntities
public class GoldExtractor {

    private static final float[] DEFAULT_TARGET_TIMES = new float[]{300.0f, 600.0f, 900.0f};

    private final PrintWriter out =
            new PrintWriter(new OutputStreamWriter(System.out, StandardCharsets.UTF_8), true);

    private float[] targetTimes = DEFAULT_TARGET_TIMES;
    private int nextTargetIndex = 0;

    private boolean sawGameStart = false;
    private int gameStartTick = -1;
    // Total paused ticks already accumulated when the horn blew (pre-game pauses
    // during hero selection / strategy time must not shift the in-game clock).
    private int hornPausedTicks = 0;

    @OnCombatLogEntry
    public void onCombatLogEntry(CombatLogEntry cle) {
        if (cle.getType() == DOTA_COMBATLOG_TYPES.DOTA_COMBATLOG_GAME_STATE && cle.getValue() == 5) {
            sawGameStart = true;
        }
    }

    private Integer readInt(Entity e, String prop) {
        try {
            Object v = e.getProperty(prop);
            if (v instanceof Number) return ((Number) v).intValue();
        } catch (Exception ex) {
            // property not present
        }
        return null;
    }

    private int readIntOr(Entity e, String prop, int fallback) {
        Integer v = readInt(e, prop);
        return v != null ? v : fallback;
    }

    private boolean readBool(Entity e, String prop) {
        try {
            Object v = e.getProperty(prop);
            return v instanceof Boolean && (Boolean) v;
        } catch (Exception ex) {
            return false;
        }
    }

    private int[] teamTotals(Entity dataTeam) {
        // returns {netWorthSum, earnedGoldSum}
        int nw = 0, eg = 0;
        for (int i = 0; i < 5; i++) {
            Integer n = readInt(dataTeam, String.format("m_vecDataTeam.%04d.m_iNetWorth", i));
            Integer g = readInt(dataTeam, String.format("m_vecDataTeam.%04d.m_iTotalEarnedGold", i));
            if (n != null) nw += n;
            if (g != null) eg += g;
        }
        return new int[]{nw, eg};
    }

    // Sample after all messages for the tick have updated the entity state. Sampling
    // at tick start reads the previous tick and can disagree with Dotabuff when net
    // worth changes exactly on a target timestamp.
    @OnTickEnd
    public void onTickStart(Context ctx, boolean synthetic) {
        if (nextTargetIndex >= targetTimes.length) return;
        if (!sawGameStart) return;

        Entities entities = ctx.getProcessor(Entities.class);
        Entity rules = entities.getByDtName("CDOTAGamerulesProxy");
        if (rules == null) return;

        // m_nTotalPausedTicks is reconciled by the engine only when a pause ENDS.
        // While paused it stays stale, so the derived clock keeps advancing during a
        // pause. We must never sample net worth mid-pause: the board is frozen and the
        // reported clock would be wrong. Once the game resumes the counter snaps to the
        // correct value and the clock reflects true (pause-excluded) game time.
        int totalPausedTicks = readIntOr(rules, "m_pGameRules.m_nTotalPausedTicks", 0);
        boolean paused = readBool(rules, "m_pGameRules.m_bGamePaused");

        if (gameStartTick < 0) {
            gameStartTick = ctx.getTick();
            hornPausedTicks = totalPausedTicks;
        }

        if (paused) return;

        // Pause-excluded game clock: elapsed ticks since the horn minus the ticks the
        // game spent paused after the horn. This matches the in-game clock (and the
        // OpenDota/Dotabuff timeline) even across long pauses.
        int elapsedTicks = ctx.getTick() - gameStartTick - (totalPausedTicks - hornPausedTicks);
        float clock = elapsedTicks * ctx.getMillisPerTick() / 1000.0f;
        if (clock + 0.001f < targetTimes[nextTargetIndex]) return;

        Entity radiantData = entities.getByDtName("CDOTA_DataRadiant");
        Entity direData = entities.getByDtName("CDOTA_DataDire");
        if (radiantData == null || direData == null) return;

        int[] rad = teamTotals(radiantData);
        int[] dire = teamTotals(direData);

        while (nextTargetIndex < targetTimes.length && clock + 0.001f >= targetTimes[nextTargetIndex]) {
            float targetTime = targetTimes[nextTargetIndex];
            out.printf(
                Locale.US,
                "{\"target_time\":%.0f,\"target_minute\":%d,\"clock\":%.3f,"
                + "\"radiant_networth\":%d,\"dire_networth\":%d,"
                + "\"networth_diff\":%d,\"radiant_earned_gold\":%d,\"dire_earned_gold\":%d,"
                + "\"earned_gold_diff\":%d}%n",
                targetTime, Math.round(targetTime / 60.0f), clock,
                rad[0], dire[0], rad[0] - dire[0], rad[1], dire[1], rad[1] - dire[1]
            );
            nextTargetIndex++;
        }
    }

    private static float[] parseTargetTimes(String[] args) {
        if (args.length < 2) return DEFAULT_TARGET_TIMES;

        List<Float> times = new ArrayList<>();
        for (int i = 1; i < args.length; i++) {
            for (String token : args[i].split(",")) {
                String raw = token.trim();
                if (!raw.isEmpty()) {
                    times.add(Float.parseFloat(raw));
                }
            }
        }
        if (times.isEmpty()) return DEFAULT_TARGET_TIMES;

        float[] parsed = new float[times.size()];
        for (int i = 0; i < times.size(); i++) {
            parsed[i] = times.get(i);
        }
        Arrays.sort(parsed);
        return parsed;
    }

    public void run(String[] args) throws Exception {
        targetTimes = parseTargetTimes(args);
        new SimpleRunner(new MappedFileSource(args[0])).runWith(this);
    }

    public static void main(String[] args) throws Exception {
        if (args.length < 1) {
            System.err.println("Usage: GoldExtractor <replay.dem> [gameTimeSeconds ...]");
            System.exit(1);
        }
        new GoldExtractor().run(args);
    }
}
