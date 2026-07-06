# Dota Series Parser

Trader-facing tool that parses Dota 2 pro-match replays and resolves book markets
(kill milestones, interval markets, rune markets) against replay ground truth.

## Language

**Power Rune**:
One of the 7 buff runes — arcane, double_damage, haste, illusion, invisibility,
regeneration, shield — that spawns every 2 minutes from 6:00 game clock at a
single, randomly chosen river spot. Excludes bounty, water, and wisdom runes.
_Avoid_: rune (unqualified), powerup rune

**Rune Spawn Side**:
The river power-rune spot where a Power Rune appears: `top` or `bot`. Map-absolute —
independent of Home/Away and of Radiant/Dire.
_Avoid_: bottom, Radiant side, Dire side

**Spawn Time**:
The even game-clock minute (6m, 8m, 10m, ...) identifying one Power Rune spawn.
Rune markets settle one selection per Spawn Time.
_Avoid_: spawn tick, rune minute

**Selection label**:
The exact lowercase book string for a market selection (`double_damage`, `top`,
`bot`, ...), displayed verbatim so the UI cross-checks 1:1 against the resolving
tool and Dotabuff hint pastes.
_Avoid_: prettified names (Double Damage, Bottom)

**Home / Away**:
Trader-assigned team perspective for interval kill markets (blue = Home & Under,
orange = Away & Over). Rune markets do not use it.
_Avoid_: Radiant = Home default
