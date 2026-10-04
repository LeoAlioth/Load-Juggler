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


def split(mode, available, cycles=3):
    """The two permits once each car draws what it was offered."""
    draws = (0.0, 0.0)
    for _ in range(cycles):
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
        draws = tuple(load.available_current for load in loads)
    return draws


@pytest.mark.parametrize("available", list(TABLE))
@pytest.mark.parametrize("mode", MODES)
def test_two_chargers_split_as_the_table_says(mode, available):
    assert split(mode, available) == TABLE[available][MODES.index(mode)]
