"""Auto-detection patterns for grid CTs, inverter outputs, battery, and solar entities.

Each brand is defined in its own module. To add support for a new brand:

1. Create a new file in this package (e.g., ``mybrand.py``)
2. Define any combination of: GRID_CT, INVERTER_OUTPUT, BATTERY_SOC,
   BATTERY_POWER, SOLAR_PRODUCTION
3. Add the module to ``_BRANDS`` below

**Phase patterns** (GRID_CT, INVERTER_OUTPUT):
  Each entry has a ``patterns`` dict with keys phase_a / phase_b / phase_c.
  Tried in order — first complete 3-phase match wins.

**Single-entity patterns** (BATTERY_SOC, BATTERY_POWER, SOLAR_PRODUCTION,
  BATTERY_MAX_CHARGE_POWER, BATTERY_MAX_DISCHARGE_POWER):
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
    sma,
    goodwe,
    growatt,
    foxess,
    generic,
    smart_plugs,
)

# Brand modules in detection priority order.
# Specific brands first; generic catch-alls last.
_BRANDS = [
    solaredge,
    solarman_deye,
    fronius,
    huawei,
    enphase,
    victron,
    sofar,
    sungrow,
    sma,
    goodwe,
    growatt,
    foxess,
    generic,
]


def _collect(attr: str) -> list:
    """Collect pattern lists from all brand modules."""
    return [p for brand in _BRANDS for p in getattr(brand, attr, [])]


def _power_first(pattern_sets: list) -> list:
    """Order watt-based pattern sets ahead of amp-based ones.

    A grid CT's POWER entity is signed — negative while exporting — but the
    CURRENT entity from the same meter is very often magnitude-only. Picking
    the latter makes export structurally invisible: the export term is always
    zero, so grid-side Excess can never trigger and exported power is counted
    as household consumption. Neither is detectable at config time, which is
    why the preference belongs here rather than in a warning.

    A stable sort, so brand priority still decides within each group and a
    current-only meter is still detected — just after every power option has
    been ruled out.
    """
    return sorted(pattern_sets, key=lambda p: 0 if p.get("unit") == "W" else 1)


PHASE_PATTERNS = _power_first(_collect("GRID_CT"))
INVERTER_OUTPUT_PATTERNS = _collect("INVERTER_OUTPUT")
BATTERY_SOC_PATTERNS = _collect("BATTERY_SOC")
BATTERY_POWER_PATTERNS = _collect("BATTERY_POWER")
SOLAR_PRODUCTION_PATTERNS = _collect("SOLAR_PRODUCTION")
BATTERY_MAX_CHARGE_POWER_PATTERNS = _collect("BATTERY_MAX_CHARGE_POWER")
BATTERY_MAX_DISCHARGE_POWER_PATTERNS = _collect("BATTERY_MAX_DISCHARGE_POWER")
# Smart plugs are not solar/inverter brands — collect directly.
PLUG_POWER_MONITOR_PATTERNS = smart_plugs.PLUG_POWER_MONITOR
