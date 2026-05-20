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
    OPERATING_MODE_FREEZE_PROTECTION,
    OPERATING_MODE_SOLAR_ONLY,
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

    - Freeze Protection: always the away setpoint.
    - Solar Only: away below battery-min SOC, normal up to battery-target SOC,
      boost at/above target SOC.
    - Normal: normal setpoint, raised to boost when there is surplus — export
      exceeds the element's draw, or the battery is over its target SOC.
    """
    soc = hub_data.get("battery_soc")
    soc_min = hub_data.get("battery_soc_min")
    soc_target = hub_data.get("battery_soc_target")
    export = hub_data.get("total_export_power") or 0

    if mode == OPERATING_MODE_FREEZE_PROTECTION:
        return away, "away"

    if mode == OPERATING_MODE_SOLAR_ONLY:
        if soc is not None and soc_min is not None and soc < soc_min:
            return away, "away"
        if soc is not None and soc_target is not None and soc >= soc_target:
            return boost, "boost"
        return normal, "normal"

    # Normal mode (and any unrecognized mode).
    over_target = (
        soc is not None and soc_target is not None and soc > soc_target
    )
    excess_available = export > element_power
    if over_target or excess_available:
        return boost, "boost"
    return normal, "normal"


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

    charger_rt = (
        sensor.hass.data.get(DOMAIN, {})
        .get("chargers", {})
        .get(sensor.config_entry.entry_id, {})
    )
    mode = charger_rt.get(
        "operating_mode", DEFAULT_OPERATING_MODE_HOT_WATER_TANK
    )
    away = charger_rt.get("tank_away_temperature") or get_entry_value(
        sensor.config_entry,
        CONF_TANK_AWAY_TEMPERATURE,
        DEFAULT_TANK_AWAY_TEMPERATURE,
    )
    normal = charger_rt.get("tank_normal_temperature") or get_entry_value(
        sensor.config_entry,
        CONF_TANK_NORMAL_TEMPERATURE,
        DEFAULT_TANK_NORMAL_TEMPERATURE,
    )
    boost = charger_rt.get("tank_boost_temperature") or get_entry_value(
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
    heating_permitted = limit > 0

    # Publish state for the tank status sensor.
    if charger_rt is not None:
        charger_rt["tank_setpoint"] = setpoint
        charger_rt["tank_setpoint_label"] = label
        charger_rt["tank_heating_permitted"] = heating_permitted

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
