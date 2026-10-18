"""Forecast-driven Excess verdict (pure Python).

Owner, 2026-10-17: the Excess verdict may start BEFORE the battery is full,
because the day's sun will overfill it anyway. The planning sensor behind the
pacing feature (``pacing_energy_entity_id``) says how many kWh the site still
has to place today; while that is above zero and the battery is at least at a
chosen SOC, the battery is not the sink of last resort any more and the
Excess-mode loads may take the sun it is absorbing, at the paced rate.

This module holds the maths: the verdict latch, and the watts the Excess pool is
topped up by. The HA layer (engine/hub_calculation.py) reads the sensors and
carries the latch bit; the calculator (target_calculator.py) reads the result
off the SiteContext.
"""

from __future__ import annotations

from ..const.hub import (
    FORECAST_EXCESS_SOURCE_FIXED,
    FORECAST_EXCESS_SOURCE_MIN,
    FORECAST_EXCESS_SOURCE_TARGET,
    FORECAST_SOC_HYSTERESIS,
)

# The planned energy must exceed this to start the verdict and stay above half
# of it to keep it. 0.2 kWh is what a 2 kW tank element uses in six minutes:
# below it the paced rate (energy / at least one hour) is under 200 W, nothing
# any load can use, and a planning sensor hovering around zero must not flip the
# verdict cycle by cycle - the band between 0.1 and 0.2 kWh is what stops that.
FORECAST_EXCESS_MIN_KWH = 0.2
FORECAST_EXCESS_RELEASE_KWH = FORECAST_EXCESS_MIN_KWH / 2


# A load that began early keeps going until its start condition has slipped by
# this much (a 20 % band, as catch-up's release and pacing's restart margin), so
# a planned energy or full-charge time wobbling at the boundary does not start
# and stop it each cycle.
EARLY_START_RELEASE_RATIO = 0.8


def starts_early(planned_kwh, hours_left, load_min_w, was_on=False) -> bool:
    """May this load start early now? Counting back from the time the battery is
    next full: the planned energy has to be spent at the load's minimum power, so
    the latest start is ``planned_kwh / load_min_kW`` hours before then - the
    paced rate (planned / hours left) is at or above that minimum. ``hours_left``
    is the real time to the full charge, 0 once it has passed (full now). A latch:
    ``was_on`` is last cycle's answer, held down to ``EARLY_START_RELEASE_RATIO``
    of the start condition. Unreadable planned energy or time: False. A load with
    no minimum power (0) is never held back.
    """
    if planned_kwh is None or hours_left is None:
        return False
    if load_min_w <= 0:
        return True
    ratio = EARLY_START_RELEASE_RATIO if was_on else 1.0
    return planned_kwh * 1000.0 >= ratio * load_min_w * hours_left


def forecast_excess_gate(source, soc_min, soc_target, fixed):
    """``(gate_soc, band_pct)``: the SOC the verdict needs, and how far below it
    the latch holds.

    Minimum and target are the hub's OWN latched values (the ones Load Juggler
    already runs on, with the battery SOC hysteresis applied by
    ``_apply_soc_hysteresis``), so their band is already in the number and a
    second one would only widen it - past the minimum for the floor, which is
    protective. The fixed value has no latch of its own, so it gets the
    forecast latches' shared band (FORECAST_SOC_HYSTERESIS) below it.
    """
    if source == FORECAST_EXCESS_SOURCE_MIN:
        return soc_min, 0.0
    if source == FORECAST_EXCESS_SOURCE_TARGET:
        return soc_target, 0.0
    if source == FORECAST_EXCESS_SOURCE_FIXED:
        return fixed, float(FORECAST_SOC_HYSTERESIS)
    return None, 0.0


def forecast_excess_verdict(
    enabled, soc, gate_soc, band_pct, planned_kwh, was_on=False
) -> bool:
    """Is the forecast Excess verdict on this cycle? A latch: ``was_on`` is last
    cycle's answer.

    On needs ALL of: the option, a battery SOC, a gate SOC, planned energy above
    FORECAST_EXCESS_MIN_KWH. Once on it holds while the SOC stays within
    ``band_pct`` of the gate and the energy above half the start margin. No
    battery, no SOC, no readable planned energy (``None``: the pacing sensors
    are unreadable past the stale timeout, see ``readers._read_pacing``) or the
    option off: off - the feature is inert, and nothing else changes.
    """
    if not enabled or soc is None or gate_soc is None or planned_kwh is None:
        return False
    if was_on:
        return (
            soc >= gate_soc - band_pct
            and planned_kwh > FORECAST_EXCESS_RELEASE_KWH
        )
    return soc >= gate_soc and planned_kwh > FORECAST_EXCESS_MIN_KWH
