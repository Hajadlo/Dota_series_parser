package kills;

import skadistats.clarity.model.CombatLogEntry;
import skadistats.clarity.processor.gameevents.OnCombatLogEntry;
import skadistats.clarity.processor.runner.SimpleRunner;
import skadistats.clarity.source.MappedFileSource;
import skadistats.clarity.wire.dota.common.proto.DOTAUserMessages.DOTA_COMBATLOG_TYPES;

import java.io.OutputStreamWriter;
import java.io.PrintWriter;
import java.nio.charset.StandardCharsets;

public class PurchaseExtractor {
    private final PrintWriter out = new PrintWriter(new OutputStreamWriter(System.out, StandardCharsets.UTF_8), true);

    private static String esc(String s) {
        if (s == null) return "";
        return s.replace("\\", "\\\\").replace("\"", "\\\"");
    }

    @OnCombatLogEntry
    public void onCombatLogEntry(CombatLogEntry cle) {
        if (cle.getType() != DOTA_COMBATLOG_TYPES.DOTA_COMBATLOG_PURCHASE) return;
        out.printf(
            "{\"raw_time\":%.3f,\"target\":\"%s\",\"value\":%d,\"value_name\":\"%s\"}%n",
            cle.getTimestamp(),
            esc(cle.getTargetName()),
            cle.getValue(),
            esc(cle.getValueName())
        );
    }

    public void run(String[] args) throws Exception {
        new SimpleRunner(new MappedFileSource(args[0])).runWith(this);
    }

    public static void main(String[] args) throws Exception {
        if (args.length < 1) {
            System.err.println("Usage: PurchaseExtractor <replay.dem>");
            System.exit(1);
        }
        new PurchaseExtractor().run(args);
    }
}
