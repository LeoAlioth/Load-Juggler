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

from ..const.common import DEVICE_TYPE_EVSE
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


# The group starts when the paced rate (planned energy over the hours to the
# next full charge) is this far above its power, and keeps going until it has
# slipped this far below it (owner, 2026-10-20: 10 % each side, the group's
# power being what its loads really pull), so a planned energy or full-charge
# time wobbling at the boundary does not start and stop it each cycle.
EARLY_START_MARGIN = 1.1
EARLY_START_RELEASE_RATIO = 0.9


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
    ratio = EARLY_START_RELEASE_RATIO if was_on else EARLY_START_MARGIN
    return planned_kwh * 1000.0 >= ratio * load_min_w * hours_left


# A thermostat load this close to its boost temperature (K) cannot pull its
# element for any length of time, and one with less than this much room left today
# (kWh) has nothing to add: neither counts in the early-start group.
EARLY_MIN_HEADROOM_K = 2.0
EARLY_MIN_ROOM_KWH = 0.1


# An EVSE is a taker only with a car that can charge: plugged in and not holding
# the charge itself (SuspendedEV = the car said it wants nothing).
EARLY_EVSE_READY = ("Preparing", "Charging", "SuspendedEVSE")


def early_blocker(load):
    """Why this load sits out the early-start group, or None when it may join.
    Direct checks, no forecast: a car that is not plugged in or not asking, a
    thermostat load already within ``EARLY_MIN_HEADROOM_K`` of its boost
    temperature, a load with under ``EARLY_MIN_ROOM_KWH`` left to absorb today.
    A reading that is not known (None) never blocks."""
    if load.device_type == DEVICE_TYPE_EVSE and load.connector_status not in EARLY_EVSE_READY:
        return "no car ready to charge"
    if load.thermal_headroom_k is not None and load.thermal_headroom_k < EARLY_MIN_HEADROOM_K:
        return "already near its boost temperature"
    if load.early_room_kwh is not None and load.early_room_kwh < EARLY_MIN_ROOM_KWH:
        return "no room left to absorb today"
    return None


def early_group(takers_w, rooms_kwh, planned_kwh, hours_left, cap_w=None, was_on=False):
    """``(due, group_w, usable_kwh)`` for the loads that can take the early
    verdict (owner, 2026-10-18: they share the planned energy, so they start
    together at full power). ``takers_w``: each one's minimum power; ``rooms_kwh``:
    what each can still absorb today (None = unlimited). The group's power is their
    sum, capped by ``cap_w`` (the battery's charge rating) when given; the energy
    it can use is the planned energy or the rooms' sum, whichever is less; it is
    due when ``starts_early`` says that energy fits the hours left at that power.
    """
    if not takers_w:
        return False, 0.0, 0.0
    group_w = float(sum(takers_w))
    if cap_w and cap_w > 0:
        group_w = min(group_w, float(cap_w))
    room = float("inf") if any(r is None for r in rooms_kwh) else float(sum(rooms_kwh))
    usable = min(planned_kwh, room) if planned_kwh is not None else None
    return starts_early(usable, hours_left, group_w, was_on), group_w, usable


def clip_load_room_kwh(room_kwh, load_max_w, span_hours):
    """What one load can absorb of the forecast clip, kWh: its own *Room to
    absorb today* where it has the sensor (``room_kwh``, None when not set),
    else the most it could take over the clip - its power times the length of
    the clipping span. Either way ``early_group`` holds the group's sum to the
    clipped surplus.
    """
    if room_kwh is not None:
        return room_kwh
    return max(0.0, load_max_w) * max(0.0, span_hours) / 1000.0


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
