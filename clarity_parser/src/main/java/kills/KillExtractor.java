package kills;

import skadistats.clarity.model.CombatLogEntry;
import skadistats.clarity.processor.gameevents.OnCombatLogEntry;
import skadistats.clarity.processor.runner.SimpleRunner;
import skadistats.clarity.source.MappedFileSource;
import skadistats.clarity.wire.dota.common.proto.DOTAUserMessages.DOTA_COMBATLOG_TYPES;

import java.io.OutputStreamWriter;
import java.io.PrintWriter;
import java.nio.charset.StandardCharsets;

public class KillExtractor {

    private final PrintWriter out;

    public KillExtractor() {
        this.out = new PrintWriter(new OutputStreamWriter(System.out, StandardCharsets.UTF_8), true);
    }

    @OnCombatLogEntry
    public void onCombatLogEntry(CombatLogEntry cle) throws Exception {
        if (cle.getType() != DOTA_COMBATLOG_TYPES.DOTA_COMBATLOG_DEATH) {
            return;
        }

        // isTargetHero() — direct proto boolean, no string-table lookup.
        // True only for real hero units (not towers, couriers, Roshan, neutrals, summons).
        if (!cle.isTargetHero()) {
            return;
        }

        // Filter illusion deaths — they do not count in the player kill score.
        if (cle.isTargetIllusion()) {
            return;
        }

        // Filter Aegis (Immortality) and Wraith King Reincarnation deaths.
        // Dota 2 does NOT count these in the kill score (radiant_score / dire_score).
        // Clarity fires a full DOTA_COMBATLOG_DEATH event for them anyway — this flag suppresses them.
        if (cle.isWillReincarnate()) {
            return;
        }

        // getAttackerTeam() guard: exclude kills by neutral units (team 4), Roshan (team 0),
        // Tormentors, polar furbolgs, and any other non-player-team unit.
        // Only events where a Radiant (2) or Dire (3) unit is the attacker count as real kills.
        // NOTE: attribution is still derived from targetTeam (flip), not attackerTeam —
        // this correctly handles summon kills (Spirit Bear, Warlock Golem, etc.) where the
        // summon's team matches the owner's team.
        int attackerTeam = cle.getAttackerTeam();
        if (attackerTeam != 2 && attackerTeam != 3) {
            return;
        }

        // getTargetTeam() — direct proto int, no string-table lookup.
        // 2 = Radiant hero died  → Dire gets kill credit (killerTeam = 3)
        // 3 = Dire hero died     → Radiant gets kill credit (killerTeam = 2)
        int targetTeam = cle.getTargetTeam();
        if (targetTeam != 2 && targetTeam != 3) {
            return;
        }

        float gameTime = cle.getTimestamp();
        String targetName = cle.getTargetName();
        String attackerName = cle.getAttackerName();
        if (attackerName == null) attackerName = "";

        // Deny detection: attacker and target are on the same team.
        // In Dota 2 a deny does NOT count as a kill for anyone — emit killer_team=0
        // so the Python analyser skips it, while still preserving it in raw output
        // for debug visibility (is_deny=true).
        if (attackerTeam == targetTeam) {
            String line = String.format(
                "{\"killer_team\":0,\"is_deny\":true,\"target\":\"%s\",\"attacker\":\"%s\",\"attacker_team_raw\":%d,\"time\":%d,\"time_f\":%.3f}",
                targetName, attackerName, cle.getAttackerTeam(), Math.round(gameTime), gameTime
            );
            out.println(line);
            return;
        }

        int killerTeam = (targetTeam == 2) ? 3 : 2;

        String line = String.format(
            "{\"killer_team\":%d,\"target\":\"%s\",\"attacker\":\"%s\",\"attacker_team_raw\":%d,\"time\":%d,\"time_f\":%.3f}",
            killerTeam, targetName, attackerName, cle.getAttackerTeam(), Math.round(gameTime), gameTime
        );
        out.println(line);
    }

    public void run(String[] args) throws Exception {
        new SimpleRunner(new MappedFileSource(args[0])).runWith(this);
    }

    public static void main(String[] args) throws Exception {
        if (args.length < 1) {
            System.err.println("Usage: KillExtractor <replay.dem>");
            System.exit(1);
        }
        new KillExtractor().run(args);
    }
}
