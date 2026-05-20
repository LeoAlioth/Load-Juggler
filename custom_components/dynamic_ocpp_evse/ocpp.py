import logging
from datetime import datetime, timedelta, timezone
from .const import (
    CONF_OCPP_DEVICE_ID,
    CONF_OCPP_PROFILE_TIMEOUT,
    DEFAULT_OCPP_PROFILE_TIMEOUT,
    CONF_STACK_LEVEL,
    DEFAULT_STACK_LEVEL,
    CONF_PROFILE_VALIDITY_MODE,
    DEFAULT_PROFILE_VALIDITY_MODE,
    PROFILE_VALIDITY_MODE_ABSOLUTE,
    CONF_CHARGE_RATE_UNIT,
    DEFAULT_CHARGE_RATE_UNIT,
    CHARGE_RATE_UNIT_AMPS,
    CHARGE_RATE_UNIT_WATTS,
    CONF_PHASE_VOLTAGE,
    DEFAULT_PHASE_VOLTAGE,
    CONF_UPDATE_FREQUENCY,
    DEFAULT_UPDATE_FREQUENCY,
)
from .helpers import get_entry_value

_LOGGER = logging.getLogger(__name__)


async def detect_charge_rate_unit(sensor, ocpp_device_id: str) -> str | None:
    """Query OCPP charger for ChargingScheduleAllowedChargingRateUnit."""
    if not ocpp_device_id:
        return None
    if not sensor.hass.services.has_service("ocpp", "get_configuration"):
        return None
    try:
        response = await sensor.hass.services.async_call(
            "ocpp",
            "get_configuration",
            {
                "devid": ocpp_device_id,
                "ocpp_key": "ChargingScheduleAllowedChargingRateUnit",
            },
            blocking=True,
            return_response=True,
        )
        if not response or not isinstance(response, dict):
            return None
        value = response.get("ChargingScheduleAllowedChargingRateUnit")
        if value is None:
            value = response.get("value")
        if value is None:
            for item in response.get("configurationKey", []):
                if (
                    isinstance(item, dict)
                    and item.get("key") == "ChargingScheduleAllowedChargingRateUnit"
                ):
                    value = item.get("value")
                    break
        if not value:
            return None
        value = str(value).strip()
        if "Current" in value and "Power" in value:
            return CHARGE_RATE_UNIT_AMPS
        elif "Power" in value:
            return CHARGE_RATE_UNIT_WATTS
        elif "Current" in value:
            return CHARGE_RATE_UNIT_AMPS
        return None
    except Exception:
        return None


async def send_ocpp_command(
    sensor, limit: float, hub_entry, dynamic_control_on: bool, now_mono: float
) -> None:
    """Send OCPP charging profile to an EVSE charger."""
    connector_state = sensor.hass.states.get(sensor._connector_status_entity)
    connector_status = connector_state.state if connector_state else "unknown"
    if connector_status in ("Finishing", "Faulted"):
        _LOGGER.debug(
            "Skipping OCPP command for %s — connector is %s",
            sensor._attr_name,
            connector_status,
        )
        sensor._last_update = datetime.now(timezone.utc)
        sensor._last_command_time = now_mono
        return

    profile_timeout = int(
        get_entry_value(
            sensor.config_entry, CONF_OCPP_PROFILE_TIMEOUT, DEFAULT_OCPP_PROFILE_TIMEOUT
        )
    )
    stack_level = int(
        get_entry_value(sensor.config_entry, CONF_STACK_LEVEL, DEFAULT_STACK_LEVEL)
    )
    profile_validity_mode = get_entry_value(
        sensor.config_entry, CONF_PROFILE_VALIDITY_MODE, DEFAULT_PROFILE_VALIDITY_MODE
    )

    charge_rate_unit = get_entry_value(
        sensor.config_entry, CONF_CHARGE_RATE_UNIT, DEFAULT_CHARGE_RATE_UNIT
    )

    if charge_rate_unit not in (CHARGE_RATE_UNIT_AMPS, CHARGE_RATE_UNIT_WATTS):
        cached = getattr(sensor, "_cached_charge_rate_unit", None)
        if cached:
            charge_rate_unit = cached
        else:
            ocpp_device_id = sensor.config_entry.data.get(CONF_OCPP_DEVICE_ID)
            detected = await detect_charge_rate_unit(sensor, ocpp_device_id)
            if detected:
                charge_rate_unit = detected
                sensor._cached_charge_rate_unit = detected
                _LOGGER.info(
                    "OCPP-detected charge rate unit: %s for %s",
                    detected,
                    sensor._attr_name,
                )
            else:
                charge_rate_unit = CHARGE_RATE_UNIT_AMPS
                _LOGGER.warning(
                    "Could not detect charge rate unit for %s, defaulting to Amperes",
                    sensor._attr_name,
                )

    if charge_rate_unit == CHARGE_RATE_UNIT_WATTS:
        voltage = hub_entry.data.get(CONF_PHASE_VOLTAGE, DEFAULT_PHASE_VOLTAGE)
        phases_for_profile = sensor._car_active_phases or sensor._phases or 1
        limit_for_charger = round(limit * voltage * phases_for_profile, 0)
        rate_unit = "W"
        sensor._last_set_power = limit_for_charger
        sensor._last_set_current = None
    else:
        limit_for_charger = round(limit, 1)
        rate_unit = "A"
        sensor._last_set_current = limit_for_charger
        sensor._last_set_power = None

    if profile_validity_mode == PROFILE_VALIDITY_MODE_ABSOLUTE:
        now = datetime.now(timezone.utc)
        valid_from = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        valid_to = (now + timedelta(seconds=profile_timeout)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        charging_profile = {
            "chargingProfileId": 11,
            "stackLevel": stack_level,
            "chargingProfileKind": "Absolute",
            "chargingProfilePurpose": "TxDefaultProfile",
            "validFrom": valid_from,
            "validTo": valid_to,
            "chargingSchedule": {
                "chargingRateUnit": rate_unit,
                "startSchedule": valid_from,
                "chargingSchedulePeriod": [
                    {"startPeriod": 0, "limit": limit_for_charger}
                ],
            },
        }
        _LOGGER.debug(
            f"Using absolute profile validity mode: {valid_from} to {valid_to}"
        )
    else:
        charging_profile = {
            "chargingProfileId": 11,
            "stackLevel": stack_level,
            "chargingProfileKind": "Relative",
            "chargingProfilePurpose": "TxDefaultProfile",
            "chargingSchedule": {
                "chargingRateUnit": rate_unit,
                "duration": profile_timeout,
                "chargingSchedulePeriod": [
                    {"startPeriod": 0, "limit": limit_for_charger}
                ],
            },
        }
        _LOGGER.debug(
            f"Using relative profile validity mode: duration={profile_timeout}s"
        )

    ocpp_device_id = sensor.config_entry.data.get(CONF_OCPP_DEVICE_ID)
    if not ocpp_device_id:
        _LOGGER.error(
            f"No OCPP device ID configured for {sensor._attr_name} - cannot send charging profile"
        )
        return

    _LOGGER.debug(
        f"Sending set_charge_rate to device {ocpp_device_id} for {sensor._attr_name} "
        f"with limit: {limit_for_charger}{rate_unit} (calculated from {limit}A)"
    )

    charge_control_state = sensor.hass.states.get(sensor._charge_control_entity)
    connector_status_state = sensor.hass.states.get(sensor._connector_status_entity)
    connector_status = (
        connector_status_state.state if connector_status_state else "unknown"
    )
    car_plugged_in = connector_status not in ["Available", "unknown", "unavailable"]

    _LOGGER.debug(
        f"Charge control check: entity={sensor._connector_status_entity}, "
        f"status={connector_status}, car_plugged_in={car_plugged_in}, "
        f"limit={limit}A, switch_state={charge_control_state.state if charge_control_state else 'not found'}"
    )

    if (
        charge_control_state
        and charge_control_state.state == "off"
        and limit > 0
        and car_plugged_in
    ):
        _LOGGER.info(
            f"Charge control switch {sensor._charge_control_entity} is off but limit is "
            f"{limit}A and car is plugged in (connector: {connector_status}) - turning on"
        )
        try:
            await sensor.hass.services.async_call(
                "switch", "turn_on", {"entity_id": sensor._charge_control_entity}
            )
        except Exception as e:
            _LOGGER.warning(
                f"Failed to turn on charge_control switch {sensor._charge_control_entity}: {e}"
            )

    try:
        # blocking=True so a dispatch/execution failure raises here and is
        # caught — with blocking=False the call returns before running and the
        # command would be recorded as sent even when it never reached the
        # charger, causing the compliance checker to trigger spurious resets.
        await sensor.hass.services.async_call(
            "ocpp",
            "set_charge_rate",
            {"devid": ocpp_device_id, "custom_profile": charging_profile},
            blocking=True,
        )
    except Exception as e:
        _LOGGER.warning("OCPP set_charge_rate failed for %s: %s", sensor._attr_name, e)
        return

    # Recorded only after the command was actually sent successfully.
    sensor._last_commanded_limit = limit
    sensor._last_update = datetime.now(timezone.utc)
    sensor._last_command_time = now_mono
