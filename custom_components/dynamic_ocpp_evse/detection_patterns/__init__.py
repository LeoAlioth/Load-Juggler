"""Auto-detection patterns for grid CTs and plug monitors.

Each brand is defined in its own module. To add support for a new brand:

1. Create a new file in this package (e.g., ``mybrand.py``)
2. Define GRID_CT
3. Add the module to ``_BRANDS`` below

**Phase patterns** (GRID_CT):
  Each entry has a ``patterns`` dict with keys phase_a / phase_b / phase_c.
  Tried in order - first complete 3-phase match wins.

**Single-entity patterns** (PLUG_POWER_MONITOR, in ``smart_plugs``):
  Each entry has a single ``pattern`` regex.  First match wins.
"""

from . import (
    solaredge,
    solarman_deye,
    fronius,
    huawei,
    enphase,
    victron,
    sofar,
    sungrow,
    smart_plugs,
)

# Brand modules in detection priority order.
_BRANDS = [
    solaredge,
    solarman_deye,
    fronius,
    huawei,
    enphase,
    victron,
    sofar,
    sungrow,
]


def _collect(attr: str) -> list:
    """Collect pattern lists from all brand modules."""
    return [p for brand in _BRANDS for p in getattr(brand, attr, [])]


def _power_first(pattern_sets: list) -> list:
    """Order watt-based pattern sets ahead of amp-based ones.

    A grid CT's POWER entity is signed - negative while exporting - but the
    CURRENT entity from the same meter is very often magnitude-only. Picking
    the latter makes export structurally invisible: the export term is always
    zero, so grid-side Excess can never trigger and exported power is counted
    as household consumption. Neither is detectable at config time, which is
    why the preference belongs here rather than in a warning.

    A stable sort, so brand priority still decides within each group and a
    current-only meter is still detected - just after every power option has
    been ruled out.
    """
    return sorted(pattern_sets, key=lambda p: 0 if p.get("unit") == "W" else 1)


PHASE_PATTERNS = _power_first(_collect("GRID_CT"))
# Smart plugs are not solar/inverter brands - collect directly.
PLUG_POWER_MONITOR_PATTERNS = smart_plugs.PLUG_POWER_MONITOR
