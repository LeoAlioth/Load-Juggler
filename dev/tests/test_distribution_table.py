"""Two chargers, every distribution mode - Anze's table (2026-10-04).

Machine-authored tests - not yet human-reviewed.

Two three-phase EVSEs, 6-16 A each, charger 1 at the higher priority, both
cars taking whatever they are offered. The rows are the current per phase
available to the two of them; each cell is charger 1 / charger 2 (A per
phase).

* Shared - both minimums, the rest equally; below both minimums the first
  takes it all.
* Priority - both minimums, the rest to charger 1 up to its maximum, then to
  charger 2; below both minimums charger 1 takes it all.
* Optimized - charger 1 up to its maximum; with room left over beyond that it
  is trimmed (never under its own minimum) so charger 2 reaches its minimum.
  With nothing left over charger 1 takes it all and charger 2 gets 0.
  Starting charger 2 takes 1 A left over (OPTIMIZED_START_MARGIN); once
  running it keeps its minimum down to any leftover above 0, so a supply
  between 16 and 17 A does not flip the two.
* Strict - charger 2 gets only what is left with charger 1 at its maximum, and
  only when that reaches its minimum. Charger 1 is never trimmed.

The real calculator runs, cycle after cycle, with each car drawing what it was
offered the cycle before.
"""

import pytest

from custom_components.dynamic_ocpp_evse.calculations.models import (
    LoadContext,
    PhaseValues,
    SiteContext,
)
from custom_components.dynamic_ocpp_evse.calculations.target_calculator import (
    calculate_all_load_targets,
)
from custom_components.dynamic_ocpp_evse.const import (
    DISTRIBUTION_MODE_PRIORITY,
    DISTRIBUTION_MODE_SEQUENTIAL_OPTIMIZED,
    DISTRIBUTION_MODE_SEQUENTIAL_STRICT,
    DISTRIBUTION_MODE_SHARED,
)

MODES = (
    DISTRIBUTION_MODE_SHARED,
    DISTRIBUTION_MODE_PRIORITY,
    DISTRIBUTION_MODE_SEQUENTIAL_OPTIMIZED,
    DISTRIBUTION_MODE_SEQUENTIAL_STRICT,
)

# available A per phase: one (charger 1, charger 2) per mode, in MODES order
TABLE = {
    26: ((13, 13), (16, 10), (16, 10), (16, 10)),
    24: ((12, 12), (16, 8), (16, 8), (16, 8)),
    22: ((11, 11), (16, 6), (16, 6), (16, 6)),
    20: ((10, 10), (14, 6), (14, 6), (16, 0)),
    18: ((9, 9), (12, 6), (12, 6), (16, 0)),
    16: ((8, 8), (10, 6), (16, 0), (16, 0)),
    12: ((6, 6), (6, 6), (12, 0), (12, 0)),
    11: ((11, 0), (11, 0), (11, 0), (11, 0)),
}


def cycle(mode, available, draws):
    """One site cycle: the two permits, each car drawing ``draws``."""
    loads = [
        LoadContext(
            load_id=f"c{i}", entity_id=f"c{i}", min_current=6, max_current=16,
            phases=3, priority=i,
            l1_current=draw, l2_current=draw, l3_current=draw,
        )
        for i, draw in ((1, draws[0]), (2, draws[1]))
    ]
    site = SiteContext(
        voltage=230,
        main_breaker_rating=available,
        consumption=PhaseValues(0.0, 0.0, 0.0),
        export_current=PhaseValues(0.0, 0.0, 0.0),
        distribution_mode=mode,
        loads=loads,
    )
    calculate_all_load_targets(site)
    return tuple(load.available_current for load in loads)


def split(mode, available, cycles=3, draws=(0.0, 0.0)):
    """The two permits once each car draws what it was offered."""
    for _ in range(cycles):
        draws = cycle(mode, available, draws)
    return draws


@pytest.mark.parametrize("available", list(TABLE))
@pytest.mark.parametrize("mode", MODES)
def test_two_chargers_split_as_the_table_says(mode, available):
    assert split(mode, available) == TABLE[available][MODES.index(mode)]


def walk(steps):
    """Optimized, the available current walked through ``steps``, each car
    drawing what it was offered: (available, charger 2 running) per step."""
    draws, seen = (0.0, 0.0), []
    for available in steps:
        draws = split(DISTRIBUTION_MODE_SEQUENTIAL_OPTIMIZED, available, draws=draws)
        seen.append((available, draws[1] > 0))
    return seen


def _tenths(lo, hi):
    return [round(lo + i / 10, 1) for i in range(round((hi - lo) * 10) + 1)]


def test_optimized_starts_the_second_charger_a_margin_above_the_first_ones_maximum():
    """Walking up from 15 to 19 A the second charger starts once 1 A is left
    beyond the first one's 16 A - at 17 A (11/6) - and runs from there on."""
    seen = walk(_tenths(15, 19))
    assert [a for a, running in seen if running] == _tenths(17, 19), seen


def test_optimized_keeps_the_second_charger_until_nothing_is_left_over():
    """Walking back down from 19 A it keeps running to 16.1 A (10.1/6) and
    stops at 16 A, where nothing is left beyond the first one's maximum."""
    seen = walk(_tenths(17, 19) + _tenths(15, 19)[::-1])
    down = seen[len(_tenths(17, 19)):]
    assert [a for a, running in down if running] == _tenths(16.1, 19)[::-1], down


def test_optimized_does_not_flip_inside_the_band():
    """Between 16 and 17 A the supply hunting up and down changes nothing:
    a second charger that was off stays off, one that was running runs on."""
    band = [16.1, 16.9, 16.5, 16.2, 16.8, 16.3, 16.7] * 3
    assert not any(running for _, running in walk([15.0, *band])[1:])
    assert all(running for _, running in walk([18.0, *band])[1:])
