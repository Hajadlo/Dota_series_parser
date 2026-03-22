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

    // Pre-game offset: the raw timestamp when game state transitions to 5
    // (in-game clock = 0:00, creeps spawn). Subtracted from all event timestamps.
    private float gameStartTime = 0.0f;

    public KillExtractor() {
        this.out = new PrintWriter(new OutputStreamWriter(System.out, StandardCharsets.UTF_8), true);
    }

    @OnCombatLogEntry
    public void onCombatLogEntry(CombatLogEntry cle) throws Exception {

        // === Detect game start (state 5) to capture pre-game offset ===
        // DOTA_COMBATLOG_GAME_STATE with value 5 = game officially begins.
        // cle.getTimestamp() at this moment is the raw pre-game duration
        // (draft + strategy + countdown). We subtract it from all kill times.
        if (cle.getType() == DOTA_COMBATLOG_TYPES.DOTA_COMBATLOG_GAME_STATE) {
            if (cle.getValue() == 5) {
                gameStartTime = cle.getTimestamp();
            }
            return;
        }

        // === Aegis pickup ===
        // DOTA_COMBATLOG_AEGIS_TAKEN fires when a hero picks up the Aegis of the Immortal.
        if (cle.getType() == DOTA_COMBATLOG_TYPES.DOTA_COMBATLOG_AEGIS_TAKEN) {
            String heroName = cle.getTargetName();
            if (heroName == null) heroName = "";
            int team = cle.getTargetTeam();
            float gameTime = cle.getTimestamp() - gameStartTime;
            if (team == 2 || team == 3) {
                out.println(String.format(
                    "{\"type\":\"aegis\",\"killer_team\":%d,\"target\":\"%s\",\"time\":%d,\"time_f\":%.3f}",
                    team, heroName, Math.round(gameTime), gameTime
                ));
            }
            return;
        }

        if (cle.getType() != DOTA_COMBATLOG_TYPES.DOTA_COMBATLOG_DEATH) {
            return;
        }

        String targetName = cle.getTargetName();
        if (targetName == null) targetName = "";
        float gameTime = cle.getTimestamp() - gameStartTime;

        // === Tower deaths ===
        // Radiant towers: npc_dota_goodguys_tower* → lost_team=2 (Radiant lost it)
        // Dire towers:    npc_dota_badguys_tower*  → lost_team=3 (Dire lost it)
        // The opposing team gets "First Tower" credit regardless of attacker.
        if (targetName.startsWith("npc_dota_goodguys_tower") ||
                targetName.startsWith("npc_dota_badguys_tower")) {
            int lostTeam = targetName.startsWith("npc_dota_goodguys") ? 2 : 3;
            out.println(String.format(
                "{\"type\":\"tower\",\"lost_team\":%d,\"target\":\"%s\",\"time\":%d,\"time_f\":%.3f}",
                lostTeam, targetName, Math.round(gameTime), gameTime
            ));
            return;
        }

        // === Barracks deaths ===
        // Radiant rax: npc_dota_goodguys_melee_rax_* / npc_dota_goodguys_range_rax_*
        // Dire rax:    npc_dota_badguys_melee_rax_*  / npc_dota_badguys_range_rax_*
        if (targetName.contains("_rax_") &&
                (targetName.startsWith("npc_dota_goodguys") || targetName.startsWith("npc_dota_badguys"))) {
            int lostTeam = targetName.startsWith("npc_dota_goodguys") ? 2 : 3;
            out.println(String.format(
                "{\"type\":\"barracks\",\"lost_team\":%d,\"target\":\"%s\",\"time\":%d,\"time_f\":%.3f}",
                lostTeam, targetName, Math.round(gameTime), gameTime
            ));
            return;
        }

        // === Roshan kill (First Aegis proxy) ===
        // The team that kills Roshan picks up the Aegis.
        if (targetName.contains("roshan")) {
            int killerTeam = cle.getAttackerTeam();
            if (killerTeam == 2 || killerTeam == 3) {
                out.println(String.format(
                    "{\"type\":\"roshan\",\"killer_team\":%d,\"target\":\"%s\",\"time\":%d,\"time_f\":%.3f}",
                    killerTeam, targetName, Math.round(gameTime), gameTime
                ));
            }
            return;
        }

        // === Tormentor kill ===
        // npc_dota_miniboss is the tormentor NPC.
        if (targetName.contains("miniboss")) {
            int killerTeam = cle.getAttackerTeam();
            if (killerTeam == 2 || killerTeam == 3) {
                out.println(String.format(
                    "{\"type\":\"tormentor\",\"killer_team\":%d,\"target\":\"%s\",\"time\":%d,\"time_f\":%.3f}",
                    killerTeam, targetName, Math.round(gameTime), gameTime
                ));
            }
            return;
        }

        // === Hero kills ===
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
        if (cle.isWillReincarnate()) {
            return;
        }

        // getAttackerTeam() guard: exclude kills by neutral units (team 4), Roshan (team 0),
        // and any other non-player-team unit.
        int attackerTeam = cle.getAttackerTeam();
        if (attackerTeam != 2 && attackerTeam != 3) {
            return;
        }

        // getTargetTeam() — direct proto int, no string-table lookup.
        int targetTeam = cle.getTargetTeam();
        if (targetTeam != 2 && targetTeam != 3) {
            return;
        }

        String attackerName = cle.getAttackerName();
        if (attackerName == null) attackerName = "";

        // Deny detection: attacker and target are on the same team.
        // In Dota 2 a deny does NOT count as a kill for anyone — emit killer_team=0
        // so the Python analyser skips it, while still preserving it in raw output
        // for debug visibility (is_deny=true).
        if (attackerTeam == targetTeam) {
            out.println(String.format(
                "{\"type\":\"kill\",\"killer_team\":0,\"is_deny\":true,\"target\":\"%s\",\"attacker\":\"%s\",\"attacker_team_raw\":%d,\"time\":%d,\"time_f\":%.3f}",
                targetName, attackerName, cle.getAttackerTeam(), Math.round(gameTime), gameTime
            ));
            return;
        }

        // 2 = Radiant hero died → Dire gets kill credit (killerTeam = 3)
        // 3 = Dire hero died   → Radiant gets kill credit (killerTeam = 2)
        int killerTeam = (targetTeam == 2) ? 3 : 2;
        out.println(String.format(
            "{\"type\":\"kill\",\"killer_team\":%d,\"target\":\"%s\",\"attacker\":\"%s\",\"attacker_team_raw\":%d,\"time\":%d,\"time_f\":%.3f}",
            killerTeam, targetName, attackerName, cle.getAttackerTeam(), Math.round(gameTime), gameTime
        ));
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
