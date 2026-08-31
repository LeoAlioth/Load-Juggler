"""Hot water tank control — setpoint resolution and climate-entity commands.

The climate entity owns all temperature regulation (hysteresis, min cycle,
sensor). Load Juggler only gates power (hvac_mode heat/off) and writes the
setpoint chosen by the tank's operating mode.
"""

import logging
from datetime import datetime, timezone

from ..const import (
    DOMAIN,
    CONF_CLIMATE_ENTITY_ID,
    CONF_HEATING_ELEMENT_POWER,
    DEFAULT_HEATING_ELEMENT_POWER,
    CONF_TANK_AWAY_TEMPERATURE,
    CONF_TANK_NORMAL_TEMPERATURE,
    CONF_TANK_BOOST_TEMPERATURE,
    DEFAULT_TANK_AWAY_TEMPERATURE,
    DEFAULT_TANK_NORMAL_TEMPERATURE,
    DEFAULT_TANK_BOOST_TEMPERATURE,
    TANK_MODE_FREEZE_PROTECTION,
    TANK_MODE_SOLAR_PRIORITY,
    DEFAULT_OPERATING_MODE_HOT_WATER_TANK,
)
from ..helpers import get_entry_value

_LOGGER = logging.getLogger(__name__)


def resolve_tank_setpoint(
    mode: str,
    away: float,
    normal: float,
    boost: float,
    element_power: float,
    hub_data: dict,
) -> tuple[float, str]:
    """Return (setpoint_temperature, label) for the tank's operating mode.

    Pure function — unit-testable. ``label`` is "away" / "normal" / "boost".

    - Freeze Protection: the away setpoint, raised to boost when the hub reports
      excess — the site can't absorb its own production anywhere else — or the
      battery is over its target SOC (ride free energy whenever it's available).
    - Solar Priority: away below battery-min SOC, normal up to battery-target
      SOC, boost at/above target SOC.
    - Normal: normal setpoint, raised to boost on the same surplus test as
      Freeze Protection.
    """
    soc = hub_data.get("battery_soc")
    soc_min = hub_data.get("battery_soc_min")
    soc_target = hub_data.get("battery_soc_target")
    export = hub_data.get("total_export_power") or 0

    # "There is real surplus" is decided once, by the hub's excess gate
    # (calculations.excess_margin + its hysteresis latch), and every
    # Excess-mode load reads that same verdict. Fall back to comparing export
    # against the element's own draw only if the hub published no verdict —
    # a stale hub_data shouldn't strand the tank at its floor forever.
    excess_available = hub_data.get("excess_available")
    if excess_available is None:
        excess_available = export > element_power

    # Free energy is available on that verdict, or once the battery has charged
    # past its target SOC. Both Freeze Protection and Normal ride this surplus
    # up to the boost setpoint.
    over_target = soc is not None and soc_target is not None and soc > soc_target
    surplus_available = over_target or excess_available

    if mode == TANK_MODE_FREEZE_PROTECTION.key:
        return (boost, "boost") if surplus_available else (away, "away")

    if mode == TANK_MODE_SOLAR_PRIORITY.key:
        if soc is not None and soc_min is not None and soc < soc_min:
            return away, "away"
        if soc is not None and soc_target is not None and soc >= soc_target:
            return boost, "boost"
        return normal, "normal"

    # Normal mode (and any unrecognized mode).
    return (boost, "boost") if surplus_available else (normal, "normal")


async def send_hot_water_tank_command(
    sensor, limit: float, hub_data: dict, now_mono: float
) -> None:
    """Drive a hot water tank's climate entity: gate heating and set the target.

    ``limit`` is the engine's allocated current after smoothing — > 0 means the
    engine found power for the tank, so heating is permitted.
    """
    climate_entity = sensor.config_entry.data.get(CONF_CLIMATE_ENTITY_ID)
    if not climate_entity:
        _LOGGER.error(
            "No climate entity configured for hot water tank %s", sensor._attr_name
        )
        return

    load_rt = (
        sensor.hass.data.get(DOMAIN, {})
        .get("loads", {})
        .get(sensor.config_entry.entry_id, {})
    )
    mode = load_rt.get(
        "operating_mode", DEFAULT_OPERATING_MODE_HOT_WATER_TANK.key
    )
    away = load_rt.get("tank_away_temperature") or get_entry_value(
        sensor.config_entry,
        CONF_TANK_AWAY_TEMPERATURE,
        DEFAULT_TANK_AWAY_TEMPERATURE,
    )
    normal = load_rt.get("tank_normal_temperature") or get_entry_value(
        sensor.config_entry,
        CONF_TANK_NORMAL_TEMPERATURE,
        DEFAULT_TANK_NORMAL_TEMPERATURE,
    )
    boost = load_rt.get("tank_boost_temperature") or get_entry_value(
        sensor.config_entry,
        CONF_TANK_BOOST_TEMPERATURE,
        DEFAULT_TANK_BOOST_TEMPERATURE,
    )
    element_power = get_entry_value(
        sensor.config_entry,
        CONF_HEATING_ELEMENT_POWER,
        DEFAULT_HEATING_ELEMENT_POWER,
    )

    setpoint, label = resolve_tank_setpoint(
        mode, away, normal, boost, element_power, hub_data
    )

    # Clamp to the climate entity's own limits. HA hard-rejects an
    # out-of-range set_temperature, and with blocking=False that rejection is
    # invisible here — the thermostat would silently keep its previous target
    # (e.g. a 90 °C boost against a 75 °C max_temp leaves it at the away
    # setpoint and the tank never heats). Warn once per offending setpoint.
    climate_state = sensor.hass.states.get(climate_entity)
    if climate_state is not None:
        clamped = setpoint
        max_temp = climate_state.attributes.get("max_temp")
        min_temp = climate_state.attributes.get("min_temp")
        try:
            if max_temp is not None:
                clamped = min(clamped, float(max_temp))
            if min_temp is not None:
                clamped = max(clamped, float(min_temp))
        except (TypeError, ValueError):
            clamped = setpoint
        if clamped != setpoint:
            if load_rt.get("_tank_clamp_warned_for") != setpoint:
                load_rt["_tank_clamp_warned_for"] = setpoint
                _LOGGER.warning(
                    "Hot water tank %s: %s setpoint %.0f°C is outside %s's "
                    "supported range (%s–%s°C) — clamped to %.0f°C. Adjust the "
                    "setpoint slider or the thermostat's limits.",
                    sensor._attr_name,
                    label,
                    setpoint,
                    climate_entity,
                    min_temp,
                    max_temp,
                    clamped,
                )
            setpoint = clamped
        else:
            load_rt.pop("_tank_clamp_warned_for", None)

    heating_permitted = limit > 0

    # Publish state for the tank status sensor.
    if load_rt is not None:
        load_rt["tank_setpoint"] = setpoint
        load_rt["tank_setpoint_label"] = label
        load_rt["tank_heating_permitted"] = heating_permitted

    _LOGGER.debug(
        "Hot water tank %s [%s]: setpoint=%.0f°C (%s), heating %s",
        sensor._attr_name,
        mode,
        setpoint,
        label,
        "permitted" if heating_permitted else "forbidden",
    )

    # The integration is the master controller — re-assert each command cycle.
    try:
        if heating_permitted:
            await sensor.hass.services.async_call(
                "climate",
                "set_temperature",
                {"entity_id": climate_entity, "temperature": setpoint},
                blocking=False,
            )
            await sensor.hass.services.async_call(
                "climate",
                "set_hvac_mode",
                {"entity_id": climate_entity, "hvac_mode": "heat"},
                blocking=False,
            )
        else:
            await sensor.hass.services.async_call(
                "climate",
                "set_hvac_mode",
                {"entity_id": climate_entity, "hvac_mode": "off"},
                blocking=False,
            )
    except Exception as e:
        _LOGGER.warning(
            "Hot water tank climate command failed for %s: %s",
            sensor._attr_name,
            e,
        )

    sensor._last_update = datetime.now(timezone.utc)
    sensor._last_command_time = now_mono
