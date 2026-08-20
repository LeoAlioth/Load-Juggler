"""Portable power station control — charge speed and backup reserve.

Two writes per command cycle, and the interesting one is the reserve. The
station's charge-speed knob has no zero (200 W is its floor), so "stop
charging" cannot be expressed as a rate. What stops it is the backup reserve:
below the current battery level the station draws nothing from the wall and
serves its own loads from its battery instead.

So the engine's allocation sets *how fast*, and the reserve sets *whether*.
"""

import logging
from datetime import datetime, timezone

from ..const import (
    DOMAIN,
    CONF_CONNECTED_TO_PHASE,
    CONF_PHASE_VOLTAGE,
    DEFAULT_PHASE_VOLTAGE,
    CONF_STATION_CHARGE_SPEED_ENTITY_ID,
    CONF_STATION_RESERVE_ENTITY_ID,
    CONF_STATION_CHARGE_LIMIT_ENTITY_ID,
    CONF_STATION_MIN_CHARGE_POWER,
    CONF_STATION_MAX_CHARGE_POWER,
    CONF_STATION_NORMAL_RESERVE,
    CONF_STATION_STORM_RESERVE,
    DEFAULT_STATION_MIN_CHARGE_POWER,
    DEFAULT_STATION_MAX_CHARGE_POWER,
    DEFAULT_STATION_NORMAL_RESERVE,
    DEFAULT_STATION_STORM_RESERVE,
    DEFAULT_STATION_CHARGE_LIMIT,
    STATION_CHARGE_POWER_STEP,
    resolve_station_charge_speed,
    resolve_station_reserve,
)
from ..helpers import get_entry_value
from .. import units

_LOGGER = logging.getLogger(__name__)


async def send_power_station_command(
    sensor, limit: float, hub_entry, now_mono: float
) -> None:
    """Drive a power station: write the charge speed and the backup reserve.

    ``limit`` is the engine's allocated current after smoothing. Converted to
    watts and quantised down to the device's step, it becomes the charge speed;
    if it cannot reach the station's minimum charge rate there is nothing to
    write and the reserve drops instead. ``hub_entry`` supplies the site voltage
    for that conversion.
    """
    entry = sensor.config_entry
    speed_entity = entry.data.get(CONF_STATION_CHARGE_SPEED_ENTITY_ID)
    reserve_entity = entry.data.get(CONF_STATION_RESERVE_ENTITY_ID)
    if not speed_entity or not reserve_entity:
        _LOGGER.error(
            "Power station %s is missing its charge-speed or reserve entity",
            sensor._attr_name,
        )
        return

    load_rt = (
        sensor.hass.data.get(DOMAIN, {})
        .get("loads", {})
        .get(entry.entry_id, {})
    )

    voltage = get_entry_value(hub_entry, CONF_PHASE_VOLTAGE, DEFAULT_PHASE_VOLTAGE)
    phases = len(get_entry_value(entry, CONF_CONNECTED_TO_PHASE, "A") or "A")
    allocated_w = limit * voltage * phases

    min_power = load_rt.get("station_min_charge_power") or get_entry_value(
        entry, CONF_STATION_MIN_CHARGE_POWER, DEFAULT_STATION_MIN_CHARGE_POWER
    )
    max_power = load_rt.get("station_max_charge_power") or get_entry_value(
        entry, CONF_STATION_MAX_CHARGE_POWER, DEFAULT_STATION_MAX_CHARGE_POWER
    )
    normal_reserve = load_rt.get("station_normal_reserve") or get_entry_value(
        entry, CONF_STATION_NORMAL_RESERVE, DEFAULT_STATION_NORMAL_RESERVE
    )
    storm_reserve = load_rt.get("station_storm_reserve_level") or get_entry_value(
        entry, CONF_STATION_STORM_RESERVE, DEFAULT_STATION_STORM_RESERVE
    )
    storm_on = bool(load_rt.get("station_storm_reserve"))

    # The station's own max charge limit is the user's battery-health cap; the
    # reserve is never raised above it.
    charge_limit = _read_number(
        sensor.hass, get_entry_value(entry, CONF_STATION_CHARGE_LIMIT_ENTITY_ID, None)
    )
    if charge_limit is None:
        charge_limit = DEFAULT_STATION_CHARGE_LIMIT

    speed = resolve_station_charge_speed(allocated_w, min_power, max_power)
    if storm_on:
        # A storm reserve fills as fast as the station allows.
        speed = max_power
    reserve, reserve_label = resolve_station_reserve(
        charging=speed is not None,
        normal_reserve=normal_reserve,
        storm_reserve=storm_reserve,
        charge_limit=charge_limit,
        storm_on=storm_on,
    )

    # Publish state for the status sensor and for the next engine cycle (the
    # builder needs to know whether we asked the station to charge).
    load_rt["station_charging"] = speed is not None
    load_rt["station_charge_speed"] = speed
    load_rt["station_reserve"] = reserve
    load_rt["station_reserve_label"] = reserve_label

    _LOGGER.debug(
        "Power station %s: allocated %.0fW -> speed %s, reserve %s%% (%s)",
        sensor._attr_name,
        allocated_w,
        f"{speed:.0f}W" if speed is not None else "not charging",
        reserve,
        reserve_label,
    )

    # These integrations talk BLE, so writes are cheap but not free — and a
    # value the device would round to what it already has is pure churn. Only
    # write on a change of at least one device step.
    try:
        if speed is not None:
            current_speed = _read_number(sensor.hass, speed_entity)
            if (
                current_speed is None
                or abs(current_speed - speed) >= STATION_CHARGE_POWER_STEP
            ):
                await sensor.hass.services.async_call(
                    "number",
                    "set_value",
                    {"entity_id": speed_entity, "value": speed},
                    blocking=False,
                )

        current_reserve = _read_number(sensor.hass, reserve_entity)
        if current_reserve is None or abs(current_reserve - reserve) >= 1:
            await sensor.hass.services.async_call(
                "number",
                "set_value",
                {"entity_id": reserve_entity, "value": reserve},
                blocking=False,
            )
    except Exception as e:
        _LOGGER.warning(
            "Power station command failed for %s: %s", sensor._attr_name, e
        )

    sensor._last_update = datetime.now(timezone.utc)
    sensor._last_command_time = now_mono


def _read_number(hass, entity_id):
    """Current numeric state of ``entity_id``, or None if unusable."""
    if not entity_id:
        return None
    state = hass.states.get(entity_id)
    if units.is_unavailable(state):
        return None
    try:
        value = float(state.state)
    except (TypeError, ValueError):
        return None
    return None if units.is_unusable_number(value) else value
