"""Catch-up - a Solar load may start below the target SOC (pure Python).

Owner, 2026-10-17: a Solar Only / Solar Priority load waits for the battery to
reach its target SOC. If the energy the site still has to place today (the
pacing sensor) is more than the load could use even running flat out for the
hours left, waiting only makes the battery overfill: the load may run now.

This module holds the maths and the latch. The engine (engine/hub_calculation.py
``_apply_catch_up``) reads the sensors, carries the latch bit per load and sets
``LoadContext.catch_up_active``; the calculator only reads it.
"""

from __future__ import annotations

from ..const import (
    BEHAVIOR_BINARY_ABOVE_TARGET,
    BEHAVIOR_SOLAR_ONLY,
    BEHAVIOR_SOLAR_PRIORITY,
)
from .forecast_excess import FORECAST_EXCESS_MIN_KWH, FORECAST_EXCESS_RELEASE_KWH
from .pacing import pace_watts

# Behaviours the option acts on: the ones the target SOC gates. Plug Solar
# Priority (BINARY_ABOVE_MIN) already runs above the minimum, which is the
# catch-up's lowest floor, so it has nothing to catch up. Tanks follow the
# Excess verdict instead and are never eligible.
CATCH_UP_BEHAVIORS = frozenset(
    {BEHAVIOR_SOLAR_PRIORITY, BEHAVIOR_SOLAR_ONLY, BEHAVIOR_BINARY_ABOVE_TARGET}
)

# The load can't keep up from planned rate >= its maximum power: it would need
# every remaining hour at full power just to use the energy, so waiting
# overfills the battery. No safety factor: the planned energy is a forecast, and
# being early costs only sun the battery would have taken, never below the SOC
# floor. A held catch-up releases under RELEASE x the maximum, a 20 % band (as
# the pacing restart margin, calculations/pacing.py) so a planned energy
# wobbling at the boundary does not start and stop the load each cycle.
CATCH_UP_RELEASE_RATIO = 0.8


def max_power_w(max_current, voltage, phases) -> float:
    """The load's maximum power (W): its rated current on every phase it has."""
    return max_current * voltage * max(int(phases or 1), 1)


def cant_keep_up(planned_kwh, hours_left, load_max_w, was_on=False) -> bool:
    """Is the planned rate (kWh x 1000 / max(hours_left, 1), W) at or above the
    load's maximum power? A latch: ``was_on`` is last cycle's answer.

    Needs planned energy above FORECAST_EXCESS_MIN_KWH to start (half of it to
    hold): under it no load has anything to catch up on, whatever its size.
    Unreadable pacing (``None``) or a load with no power: False.
    """
    if planned_kwh is None or hours_left is None or load_max_w <= 0:
        return False
    rate = pace_watts(planned_kwh, hours_left)
    if was_on:
        return (
            planned_kwh > FORECAST_EXCESS_RELEASE_KWH
            and rate >= CATCH_UP_RELEASE_RATIO * load_max_w
        )
    return planned_kwh > FORECAST_EXCESS_MIN_KWH and rate >= load_max_w


def catch_up_verdict(
    behavior, soc, floor_soc, band_pct, planned_kwh, hours_left, load_max_w, was_on=False,
) -> bool:
    """Is catch-up active for one load this cycle (the caller has checked the
    load's option)?

    Needs an eligible behaviour, a battery SOC at/above the floor (a held
    catch-up tolerates ``band_pct`` below it) and the load unable to keep up.
    No battery, no SOC or no floor: off - the feature is inert, nothing else
    changes.
    """
    if behavior not in CATCH_UP_BEHAVIORS:
        return False
    if soc is None or floor_soc is None:
        return False
    if soc < floor_soc - (band_pct if was_on else 0.0):
        return False
    return cant_keep_up(planned_kwh, hours_left, load_max_w, was_on)
