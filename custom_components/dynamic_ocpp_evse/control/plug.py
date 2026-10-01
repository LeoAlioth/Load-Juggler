import logging
from datetime import datetime, timezone
from ..const import CONF_PLUG_SWITCH_ENTITY_ID
from ..helpers import get_entry_value

_LOGGER = logging.getLogger(__name__)


async def send_plug_command(sensor, limit: float, now_mono: float) -> None:
    """Send on/off command to a smart load device."""
    # The settings page saves a changed switch to options, so read it there first.
    plug_switch_entity = get_entry_value(sensor.config_entry, CONF_PLUG_SWITCH_ENTITY_ID)
    if not plug_switch_entity:
        _LOGGER.error(f"No switch entity configured for plug {sensor._attr_name}")
        return

    on = limit > 0
    try:
        _LOGGER.debug(
            f"Smart load {sensor._attr_name}: turning {'ON' if on else 'OFF'} (limit={limit}A)"
        )
        await sensor.hass.services.async_call(
            "switch",
            "turn_on" if on else "turn_off",
            {"entity_id": plug_switch_entity},
            blocking=False,
        )
    except Exception as e:
        _LOGGER.warning(
            "Smart load switch command failed for %s: %s", sensor._attr_name, e
        )

    sensor._last_update = datetime.now(timezone.utc)
    sensor._last_command_time = now_mono
