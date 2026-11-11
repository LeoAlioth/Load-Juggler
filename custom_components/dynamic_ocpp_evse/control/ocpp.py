import logging
from datetime import datetime, timedelta, timezone
from ..const import (
    CONF_OCPP_DEVICE_ID,
    CONF_SEND_NUMBER_PHASES,
    DEFAULT_SEND_NUMBER_PHASES,
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
    EVSE_RT_COMMANDED_LIMIT,
    EVSE_RT_COMMANDED_RATE_UNIT,
    EVSE_RT_COMMANDED_PHASES,
    EVSE_RT_PHASES,
    EVSE_RT_PHASE_PAUSE_AT,
    EVSE_RT_PHASE_PROBE_AT,
    EVSE_RT_PHASE_SUPPORTED,
    OCPP_KEY_PHASE_SWITCH,
    PHASE_SWITCH_PAUSE_MAX_S,
    PHASE_SWITCH_PAUSE_S,
    PHASE_SWITCH_PROBE_RETRY_S,
    RT_PHASE_SWITCH_AT,
)
from ..helpers import detect_charge_rate_unit, get_entry_value, stored_legs
from . import stamp_command
from .. import units

_LOGGER = logging.getLogger(__name__)


async def read_charger_setting(hass, devid, key):
    """The value a charger holds for an OCPP configuration key (ocpp's
    get_configuration), or None when it cannot be read."""
    try:
        got = await hass.services.async_call(
            "ocpp", "get_configuration", {"devid": devid, "ocpp_key": key},
            blocking=True, return_response=True,
        )
    except Exception as err:  # noqa: BLE001 - a read that fails decides nothing
        _LOGGER.debug("could not read the charger's %s: %s", key, err)
        return None
    return got.get("value") if isinstance(got, dict) else None


def told_phases(load_rt, entry) -> int | None:
    """The phase count a charger's profile carries (``numberPhases``): the
    engine's decision for a charger that reports it can switch, None for one
    that does not (or has not answered yet), or whose *Send the phase count*
    option is off - its profile then carries none."""
    if load_rt.get(EVSE_RT_PHASE_SUPPORTED) is not True or not get_entry_value(
        entry, CONF_SEND_NUMBER_PHASES, DEFAULT_SEND_NUMBER_PHASES
    ):
        return None
    return int(load_rt.get(EVSE_RT_PHASES, 3))


def phase_pause_over(started: float, draw: float | None, now_mono: float) -> bool:
    """Whether the pause before a phase change, begun at ``started``, has done
    its job: PHASE_SWITCH_PAUSE_S held and the charger drawing under 1 A, or
    PHASE_SWITCH_PAUSE_MAX_S held whatever it reports."""
    paused = now_mono - started
    return paused >= PHASE_SWITCH_PAUSE_MAX_S or (paused >= PHASE_SWITCH_PAUSE_S and (draw or 0.0) < 1.0)


def phase_switch_pause(
    load_rt, told: int | None, limit: float, draw: float | None, now_mono: float
) -> tuple[float, int | None]:
    """The ``(limit, numberPhases)`` to send while a phase change is due: a
    charger that would draw is first held at 0 A on its old count for at least
    PHASE_SWITCH_PAUSE_S and until it draws under 1 A (PHASE_SWITCH_PAUSE_MAX_S
    at most), and only then told the new count - no switching under load (the
    owner, 2026-11-05). A 0 A command needs no pause. Keeps its clock in
    ``load_rt[EVSE_RT_PHASE_PAUSE_AT]``."""
    commanded = load_rt.get(EVSE_RT_COMMANDED_PHASES, 3)
    if told is None or told == commanded or limit <= 0:
        load_rt.pop(EVSE_RT_PHASE_PAUSE_AT, None)
        return limit, told
    if phase_pause_over(load_rt.setdefault(EVSE_RT_PHASE_PAUSE_AT, now_mono), draw, now_mono):
        load_rt.pop(EVSE_RT_PHASE_PAUSE_AT, None)
        return limit, told
    return 0.0, commanded


def probe_phase_switching(sensor, now_mono: float) -> None:
    """Ask a three-leg charger once whether it can switch between one and
    three phases (ConnectorSwitch3to1PhaseSupported), in the background so a
    slow charger never holds up the site cycle; asked again every
    PHASE_SWITCH_PROBE_RETRY_S while it does not answer."""
    load_rt = sensor._runtime()
    if (
        load_rt.get(EVSE_RT_PHASE_SUPPORTED) is not None
        or now_mono < load_rt.get(EVSE_RT_PHASE_PROBE_AT, 0.0)
        or len(stored_legs(sensor.config_entry)) != 3
    ):
        return
    devid = get_entry_value(sensor.config_entry, CONF_OCPP_DEVICE_ID, None)
    if not devid:
        return
    load_rt[EVSE_RT_PHASE_PROBE_AT] = now_mono + PHASE_SWITCH_PROBE_RETRY_S

    async def _probe():
        value = await read_charger_setting(sensor.hass, devid, OCPP_KEY_PHASE_SWITCH)
        if value is None:
            return
        load_rt[EVSE_RT_PHASE_SUPPORTED] = str(value).strip().lower() == "true"
        _LOGGER.info(
            "%s: the charger %s switch between one and three phases (%s = %s)",
            sensor._attr_name,
            "can" if load_rt[EVSE_RT_PHASE_SUPPORTED] else "cannot",
            OCPP_KEY_PHASE_SWITCH, value,
        )

    sensor.hass.async_create_task(_probe())


async def send_ocpp_command(
    sensor, limit: float, hub_entry, now_mono: float,
    effective_status: str | None = None,
    hold_s: float | None = None,
    draw: float | None = None,
) -> bool:
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

    Reported live on the SE17K site's EvBox Elvi (2026-09-09, and seen
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
        stamp_command(sensor, now_mono)
        return

    # A charger that can switch is told its phase count on every profile, so
    # one left on one phase (an option turned off, a restart) is put back.
    # ``draw``: the charger's reported current (A, the engine's figure), which
    # tells when the pause before a phase change has taken hold.
    load_rt = sensor._runtime()
    was_pausing = EVSE_RT_PHASE_PAUSE_AT in load_rt
    paused_from = limit
    limit, told = phase_switch_pause(
        load_rt, told_phases(load_rt, sensor.config_entry), limit, draw, now_mono
    )
    if limit != paused_from:
        if was_pausing:
            # Its 0 A went out when the pause began, valid for all of it; sent
            # again it would restart a go-e's charging cycle.
            stamp_command(sensor, now_mono)
            return False
        _LOGGER.info(
            "%s: paused (0 A) before switching to %d phase(s)",
            sensor._attr_name, told_phases(load_rt, sensor.config_entry),
        )
        hold_s = max(hold_s or 0, PHASE_SWITCH_PAUSE_MAX_S)
        # Compliance and the manual-change check stay quiet through the pause too.
        load_rt[RT_PHASE_SWITCH_AT] = now_mono

    # ``hold_s``: a pause's 0 A stays valid that much longer (the rest of the
    # pause), so it is sent once - see entities/load.py. Returns whether the
    # profile went out.
    profile_timeout = int(
        get_entry_value(
            sensor.config_entry, CONF_OCPP_PROFILE_TIMEOUT, DEFAULT_OCPP_PROFILE_TIMEOUT
        )
    ) + int(hold_s or 0)
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
            detected = await detect_charge_rate_unit(sensor.hass, ocpp_device_id)
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
        phases_for_profile = (
            1 if told == 1 else sensor._car_active_phases or sensor._phases or 1
        )
        limit_for_charger = round(limit * voltage * phases_for_profile, 0)
        rate_unit = "W"
        sensor._last_set_power = limit_for_charger
        sensor._last_set_current = None
    else:
        limit_for_charger = round(limit, 1)
        rate_unit = "A"
        sensor._last_set_current = limit_for_charger
        sensor._last_set_power = None

    absolute = profile_validity_mode == PROFILE_VALIDITY_MODE_ABSOLUTE
    charging_profile = {
        "chargingProfileId": 11,
        "stackLevel": stack_level,
        "chargingProfileKind": "Absolute" if absolute else "Relative",
        "chargingProfilePurpose": "TxDefaultProfile",
    }
    schedule = {"chargingRateUnit": rate_unit}
    if absolute:
        now = datetime.now(timezone.utc)
        valid_from = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        valid_to = (now + timedelta(seconds=profile_timeout)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        charging_profile["validFrom"] = valid_from
        charging_profile["validTo"] = valid_to
        schedule["startSchedule"] = valid_from
        _LOGGER.debug(
            f"Using absolute profile validity mode: {valid_from} to {valid_to}"
        )
    else:
        schedule["duration"] = profile_timeout
        _LOGGER.debug(
            f"Using relative profile validity mode: duration={profile_timeout}s"
        )
    # Key order as before: the period list closes the schedule, the schedule
    # closes the profile.
    period = {"startPeriod": 0, "limit": limit_for_charger}
    if told is not None:
        period["numberPhases"] = told
    schedule["chargingSchedulePeriod"] = [period]
    charging_profile["chargingSchedule"] = schedule

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
        if limit != paused_from:
            load_rt.pop(EVSE_RT_PHASE_PAUSE_AT, None)   # the pause never reached it
        return

    # Recorded only after the command was actually sent successfully.
    sensor._last_commanded_limit = limit
    # ...and published where the ENGINE can read it: the stuck-readout watch
    # (engine/readout_watch.py) judges the charger's reported draw against the
    # limit it actually holds, and blind mode assumes exactly this figure. The
    # unit matters only for the tolerance a W-encoded profile is given.
    load_rt[EVSE_RT_COMMANDED_LIMIT] = float(limit)
    load_rt[EVSE_RT_COMMANDED_RATE_UNIT] = rate_unit
    if told is not None:
        if told != load_rt.get(EVSE_RT_COMMANDED_PHASES, 3):
            # The charger now switches, with its few seconds at 0 A.
            load_rt[RT_PHASE_SWITCH_AT] = now_mono
            _LOGGER.info("%s: told to charge on %d phase(s)", sensor._attr_name, told)
        load_rt[EVSE_RT_COMMANDED_PHASES] = told
    stamp_command(sensor, now_mono)
    return True
