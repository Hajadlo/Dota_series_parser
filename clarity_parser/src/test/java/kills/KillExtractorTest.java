package kills;

import org.junit.jupiter.api.Test;

import java.util.Map;
import java.util.Set;

import static org.junit.jupiter.api.Assertions.assertEquals;

class KillExtractorTest {

    @Test
    void infersMissingRuneTypeFromCompletedConfirmedCycle() {
        Map<Integer, String> inferred = KillExtractor.inferMissingRuneTypes(
            Map.of(
                360, "regeneration",
                480, "illusion",
                600, "haste",
                840, "arcane",
                960, "shield",
                1080, "double_damage"
            ),
            KillExtractor.confirmUpstreamSpawnSeconds(
                Set.of(720),
                Map.of(720, Set.of(101, 202))
            ),
            Set.of(0)
        );

        assertEquals(Map.of(720, "invisibility"), inferred);
    }

    @Test
    void requiresSpectatorHandleAndBothSpawnerClocks() {
        assertEquals(
            Set.of(),
            KillExtractor.confirmUpstreamSpawnSeconds(
                Set.of(),
                Map.of(720, Set.of(101, 202))
            )
        );

        Map<Integer, String> inferred = KillExtractor.inferMissingRuneTypes(
            Map.of(
                360, "regeneration",
                480, "illusion",
                600, "haste",
                840, "arcane",
                960, "shield",
                1080, "double_damage"
            ),
            KillExtractor.confirmUpstreamSpawnSeconds(
                Set.of(720),
                Map.of(720, Set.of(101))
            ),
            Set.of(0)
        );

        assertEquals(Map.of(), inferred);
    }

    @Test
    void doesNotGuessWhenMoreThanOneCycleTypeIsMissing() {
        Map<Integer, String> inferred = KillExtractor.inferMissingRuneTypes(
            Map.of(
                360, "regeneration",
                480, "illusion",
                840, "arcane",
                960, "shield",
                1080, "double_damage"
            ),
            Set.of(600, 720),
            Set.of(0)
        );

        assertEquals(Map.of(), inferred);
    }
}
