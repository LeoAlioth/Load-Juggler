import logging
from datetime import datetime, timezone
from ..const import DOMAIN, CONF_PLUG_SWITCH_ENTITY_ID

_LOGGER = logging.getLogger(__name__)


async def send_plug_command(
    sensor, limit: float, hub_data: dict, now_mono: float
) -> None:
    """Send on/off command to a smart load device."""
    plug_switch_entity = sensor.config_entry.data.get(CONF_PLUG_SWITCH_ENTITY_ID)
    if not plug_switch_entity:
        _LOGGER.error(f"No switch entity configured for plug {sensor._attr_name}")
        return

    try:
        if limit > 0:
            _LOGGER.debug(
                f"Smart load {sensor._attr_name}: turning ON (limit={limit}A)"
            )
            await sensor.hass.services.async_call(
                "switch",
                "turn_on",
                {"entity_id": plug_switch_entity},
                blocking=False,
            )
        else:
            _LOGGER.debug(f"Smart load {sensor._attr_name}: turning OFF (limit=0)")
            await sensor.hass.services.async_call(
                "switch",
                "turn_off",
                {"entity_id": plug_switch_entity},
                blocking=False,
            )
    except Exception as e:
        _LOGGER.warning(
            "Smart load switch command failed for %s: %s", sensor._attr_name, e
        )

    plug_auto_power = hub_data.get("plug_auto_power", {})
    auto_power = plug_auto_power.get(sensor.config_entry.entry_id)
    if auto_power is not None:
        charger_data = (
            sensor.hass.data.get(DOMAIN, {})
            .get("chargers", {})
            .get(sensor.config_entry.entry_id)
        )
        if charger_data is not None:
            charger_data["device_power"] = auto_power

    sensor._last_update = datetime.now(timezone.utc)
    sensor._last_command_time = now_mono
