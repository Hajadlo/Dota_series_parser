from dota_app import analyse_runes


def test_targeted_pickup_recovered_rune_keeps_type_and_side():
    result = analyse_runes(
        [
            {
                "type": "rune",
                "rune_type": "invisibility",
                "side": "bot",
                "rune_source": "cycle_inference+targeted_pickup",
                "time_f": 720.067,
            }
        ],
        duration=723,
    )

    spawn = next(item for item in result["spawns"] if item["minute"] == 12)
    assert spawn["rune_type"] == "invisibility"
    assert spawn["side"] == "bot"
    assert spawn["rune_source"] == "cycle_inference+targeted_pickup"
    assert "unresolved_sides" not in result
    assert 12 not in result["unknown_gaps"]


def test_missing_side_without_explicit_unresolved_status_is_ignored():
    result = analyse_runes(
        [{"type": "rune", "rune_type": "invisibility", "side": None, "time_f": 720.067}],
        duration=723,
    )

    assert result["spawns"] == []
    assert len(result["ignored"]) == 1
