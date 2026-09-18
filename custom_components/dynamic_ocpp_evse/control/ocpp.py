import logging
from datetime import datetime, timedelta, timezone
from ..const import (
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
from ..helpers import get_entry_value
from .. import units

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
    sensor, limit: float, hub_entry, dynamic_control_on: bool, now_mono: float,
    effective_status: str | None = None,
) -> None:
    """Send OCPP charging profile to an EVSE charger.

    ``effective_status`` is the connector status the ENGINE decided on, taken
    from ``hub_data["load_connector_status"]``. It is not always what the
    entity says, and the difference is a bug this guard used to have: when a
    car finishes charging the connector sits in **SuspendedEV** - plugged in,
    drawing nothing, transaction still open - and after
    SUSPENDED_EV_IDLE_TIMEOUT ``load_builders`` rewrites the load's status to
    "Finishing" so the engine treats the session as over and drops the permit
    to 0. That rewrite lived only on the engine's ``LoadContext``, while this
    guard re-read the raw entity and saw "SuspendedEV" - so it kept sending a
    0 A profile every command interval, for as long as the car stayed plugged
    in, to a charger that had finished.

    Reported live on the SE17K site's EvBox Elvi (Anze, 2026-09-09, and seen
    under 1.1.x too): "Set charging profile failed with response Exception",
    always with a car plugged in, arriving when that car finished charging,
    sometimes many times within the hour - one per command interval. The
    charger is entitled to refuse: OCPP 1.6 permits a 0 A schedule period, but
    plenty of firmware rejects a limit under the 6 A minimum rather than
    reading 0 as "suspend", and answering with a protocol error rather than a
    clean Rejected is what surfaces as "Exception" through ocpp-lib.

    The fix is not to special-case the zero - it is that the actuator must stop
    when the engine stops. Falls back to the entity when no status is passed,
    so a caller that does not have hub_data still behaves as before.
    """
    if effective_status is None:
        connector_state = sensor.hass.states.get(sensor._connector_status_entity)
        effective_status = units.state_or_unknown(connector_state)
    if effective_status in ("Finishing", "Faulted"):
        _LOGGER.debug(
            "Skipping OCPP command for %s - connector is %s",
            sensor._attr_name,
            effective_status,
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
            ocpp_device_id = get_entry_value(
                sensor.config_entry, CONF_OCPP_DEVICE_ID, None
            )
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
        # Options-first (get_entry_value): the hub reconfigure/options flow
        # writes the edited voltage to entry.options, so reading entry.data
        # would keep encoding W limits with the original install value.
        voltage = (
            get_entry_value(hub_entry, CONF_PHASE_VOLTAGE, DEFAULT_PHASE_VOLTAGE)
            or DEFAULT_PHASE_VOLTAGE
        )
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

    ocpp_device_id = get_entry_value(sensor.config_entry, CONF_OCPP_DEVICE_ID, None)
    if not ocpp_device_id:
        _LOGGER.error(
            f"No OCPP device ID configured for {sensor._attr_name} - cannot send charging profile"
        )
        # Treat the failed dispatch as a spent command slot: without this the
        # site cycle (every couple of seconds) re-enters here and logs the same
        # error, instead of once per command interval.
        sensor._last_command_time = now_mono
        return

    _LOGGER.debug(
        f"Sending set_charge_rate to device {ocpp_device_id} for {sensor._attr_name} "
        f"with limit: {limit_for_charger}{rate_unit} (calculated from {limit}A)"
    )

    charge_control_state = sensor.hass.states.get(sensor._charge_control_entity)
    connector_status_state = sensor.hass.states.get(sensor._connector_status_entity)
    connector_status = units.state_or_unknown(connector_status_state)
    # A car is present only on a status that says so: "Available" is OCPP for an
    # empty connector, and a status we cannot read is not evidence of a car.
    car_plugged_in = not (
        connector_status == "Available" or units.is_unavailable_state(connector_status)
    )

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
        # caught - with blocking=False the call returns before running and the
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
