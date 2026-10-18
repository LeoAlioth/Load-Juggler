import logging
import time
from ..const import (
    EVSE_MODE_SOLAR_ONLY,
    EVSE_MODE_SOLAR_PRIORITY,
    EVSE_MODE_EXCESS,
    CONF_SOLAR_GRACE_PERIOD,
    CONF_CHARGE_PAUSE_DURATION,
    DEFAULT_SOLAR_GRACE_PERIOD,
    DEFAULT_CHARGE_PAUSE_DURATION,
)
from homeassistant.util import dt as dt_util
from ..helpers import get_entry_value
from .. import units

_LOGGER = logging.getLogger(__name__)


def hands_off_status(load_rt: dict) -> str:
    """The status of a load Load Juggler is not controlling: "Manual until
    HH:MM" while a manual override runs, "Dynamic Control Off" otherwise."""
    until = load_rt.get("manual_until")
    if until:
        return f"Manual until {dt_util.as_local(until).strftime('%H:%M')}"
    return "Dynamic Control Off"


def with_early_start(status: str, load_rt: dict) -> str:
    """``status`` with the load's early-start note after it ("Early start 09:30
    at 1.9 kW", set by the engine while the load waits its turn for the forecast
    Excess verdict). The caller applies it to waiting states only."""
    note = load_rt.get("early_start_note")
    return f"{status} - {note}" if note else status


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
    # "Available" is OCPP for "no car"; an unreadable status is treated the same
    # way - we cannot claim a car is plugged in on a status we cannot read.
    if connector_status == "Available" or units.is_unavailable_state(connector_status):
        return "Unplugged"
    if not dynamic_control_on:
        return hands_off_status(sensor._runtime())

    if (
        sensor._grace_started_at is not None
        and sensor._available_current >= min_charge_current
    ):
        grace_min = get_entry_value(
            sensor.config_entry, CONF_SOLAR_GRACE_PERIOD, DEFAULT_SOLAR_GRACE_PERIOD
        )
        elapsed = time.monotonic() - sensor._grace_started_at
        remaining = max(0, int(grace_min * 60 - elapsed))
        return f"Grace: {remaining}s"

    if sensor._pause_started_at is not None and limit == 0:
        pause_dur_s = (
            get_entry_value(
                sensor.config_entry,
                CONF_CHARGE_PAUSE_DURATION,
                DEFAULT_CHARGE_PAUSE_DURATION,
            )
            * 60
        )
        elapsed = time.monotonic() - sensor._pause_started_at
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
        mode in (EVSE_MODE_SOLAR_ONLY.key, EVSE_MODE_SOLAR_PRIORITY.key)
        and bat_below_target
    ):
        return "Battery Priority"
    if mode == EVSE_MODE_SOLAR_ONLY.key:
        return "Insufficient Solar"
    if mode == EVSE_MODE_SOLAR_PRIORITY.key:
        return "Insufficient Solar"
    if mode == EVSE_MODE_EXCESS.key:
        return "No Excess"
    return "Insufficient Power"
