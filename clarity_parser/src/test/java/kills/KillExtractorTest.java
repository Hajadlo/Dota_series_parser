package kills;

import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertNull;

class KillExtractorTest {

    @Test
    void recoversInstantlyPickedInvisibilityRuneAtBottomSpot() {
        KillExtractor.RuneFallbackCandidate candidate = KillExtractor.recoverRuneFromModifier(
            "modifier_rune_invis",
            720.867f,
            1288.219f,
            -1069.594f
        );

        assertNotNull(candidate);
        assertEquals(720, candidate.spawnSecond());
        assertEquals("invisibility", candidate.runeType());
        assertEquals("bot", candidate.side());
    }

    @Test
    void ignoresStoredBottleRuneOutsideSpawnWindow() {
        assertNull(KillExtractor.recoverRuneFromModifier(
            "modifier_rune_invis",
            730.0f,
            1288.219f,
            -1069.594f
        ));
    }

    @Test
    void ignoresRuneModifierAwayFromRiverSpots() {
        assertNull(KillExtractor.recoverRuneFromModifier(
            "modifier_rune_invis",
            720.867f,
            0.0f,
            0.0f
        ));
    }
}
