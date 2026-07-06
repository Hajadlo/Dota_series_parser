package kills;

import skadistats.clarity.model.CombatLogEntry;
import skadistats.clarity.model.Entity;
import skadistats.clarity.processor.entities.Entities;
import skadistats.clarity.processor.entities.UsesEntities;
import skadistats.clarity.processor.gameevents.OnCombatLogEntry;
import skadistats.clarity.processor.reader.OnTickStart;
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

    @OnTickStart
    public void onTickStart(Context ctx, boolean synthetic) {
        if (nextTargetIndex >= targetTimes.length) return;
        if (!sawGameStart) return;
        if (gameStartTick < 0) {
            gameStartTick = ctx.getTick();
        }

        float clock = (ctx.getTick() - gameStartTick) * ctx.getMillisPerTick() / 1000.0f;
        if (clock + 0.001f < targetTimes[nextTargetIndex]) return;

        Entities entities = ctx.getProcessor(Entities.class);
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
