import logging
from datetime import datetime
from .const import (
    OPERATING_MODE_SOLAR_ONLY,
    OPERATING_MODE_SOLAR_PRIORITY,
    OPERATING_MODE_EXCESS,
    DEFAULT_SOLAR_GRACE_PERIOD,
    DEFAULT_CHARGE_PAUSE_DURATION,
)
from .helpers import get_entry_value

_LOGGER = logging.getLogger(__name__)


def determine_charging_status(
    sensor,
    hub_data: dict,
    limit: float,
    connector_status: str,
    dynamic_control_on: bool,
    min_charge_current: float,
) -> str:
    """Determine the human-readable charging status reason.

    Returns a status string like "Charging", "Paused: 30s", "Insufficient Solar", etc.
    """
    if connector_status in ("Available", "unknown", "unavailable"):
        return "Unplugged"
    if not dynamic_control_on:
        return "Dynamic Control Off"

    if (
        sensor._grace_started_at is not None
        and sensor._allocated_current >= sensor._min_charge_current
    ):
        grace_min = get_entry_value(
            sensor.config_entry, DEFAULT_SOLAR_GRACE_PERIOD, DEFAULT_SOLAR_GRACE_PERIOD
        )
        elapsed = (datetime.now() - sensor._grace_started_at).total_seconds()
        remaining = max(0, int(grace_min * 60 - elapsed))
        return f"Grace: {remaining}s"

    if sensor._pause_started_at is not None and limit == 0:
        pause_dur_s = (
            get_entry_value(
                sensor.config_entry,
                DEFAULT_CHARGE_PAUSE_DURATION,
                DEFAULT_CHARGE_PAUSE_DURATION,
            )
            * 60
        )
        elapsed = (datetime.now() - sensor._pause_started_at).total_seconds()
        remaining = max(0, int(pause_dur_s - elapsed))
        return f"Paused: {remaining}s"

    if limit > 0:
        return "Charging"

    mode = sensor._operating_mode
    bat_soc = hub_data.get("battery_soc")
    bat_target = hub_data.get("battery_soc_target")
    bat_below_target = (
        bat_soc is not None and bat_target is not None and bat_soc < bat_target
    )
    if (
        mode in (OPERATING_MODE_SOLAR_ONLY, OPERATING_MODE_SOLAR_PRIORITY)
        and bat_below_target
    ):
        return "Battery Priority"
    if mode == OPERATING_MODE_SOLAR_ONLY:
        return "Insufficient Solar"
    if mode == OPERATING_MODE_SOLAR_PRIORITY:
        return "Insufficient Solar"
    if mode == OPERATING_MODE_EXCESS:
        return "No Excess"
    return "Insufficient Power"
