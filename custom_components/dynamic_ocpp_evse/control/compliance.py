import logging
from datetime import datetime, timezone
from ..const import (
    DOMAIN,
    HARD_RESET_COOLDOWN_SECONDS,
    AUTO_RESET_COOLDOWN_SECONDS,
    AUTO_RESET_MISMATCH_THRESHOLD,
    ESCALATION_PROFILE_RESET_LIMIT,
    DEFAULT_UPDATE_FREQUENCY,
    RAMP_DOWN_RATE,
    DEAD_BAND,
    CONF_ENTITY_ID,
    CONF_CHARGER_ID,
    CONF_EVSE_CURRENT_OFFERED_ENTITY_ID,
    CONF_EVSE_POWER_OFFERED_ENTITY_ID,
    CONF_UPDATE_FREQUENCY,
    CONF_PHASE_VOLTAGE,
    DEFAULT_PHASE_VOLTAGE,
)
from ..helpers import get_entry_value

_LOGGER = logging.getLogger(__name__)


async def check_profile_compliance(
    sensor, limit: float, dynamic_control_on: bool
) -> None:
    """Check if the charger is following commanded profiles and auto-reset if not."""
    if not dynamic_control_on or limit <= 0:
        sensor._mismatch_count = 0
        return

    if sensor._last_commanded_limit is None or sensor._last_commanded_limit <= 0:
        return

    if sensor._last_hard_reset_at is not None:
        elapsed = (datetime.now(timezone.utc) - sensor._last_hard_reset_at).total_seconds()
        if elapsed < HARD_RESET_COOLDOWN_SECONDS:
            sensor._mismatch_count = 0
            return

    if sensor._last_auto_reset_at is not None:
        elapsed = (datetime.now(timezone.utc) - sensor._last_auto_reset_at).total_seconds()
        if elapsed < AUTO_RESET_COOLDOWN_SECONDS:
            sensor._mismatch_count = 0
            return

    connector_status_state = sensor.hass.states.get(sensor._connector_status_entity)
    connector_status = (
        connector_status_state.state if connector_status_state else "unknown"
    )
    if connector_status in ("Available", "unknown", "unavailable"):
        sensor._mismatch_count = 0
        return

    current_offered_entity_id = sensor.config_entry.data.get(
        CONF_EVSE_CURRENT_OFFERED_ENTITY_ID
    )
    power_offered_entity_id = sensor.config_entry.data.get(
        CONF_EVSE_POWER_OFFERED_ENTITY_ID
    )

    current_offered = None

    if current_offered_entity_id:
        state = sensor.hass.states.get(current_offered_entity_id)
        if state and state.state not in ("unknown", "unavailable", None, ""):
            try:
                current_offered = float(state.state)
            except (ValueError, TypeError):
                current_offered = None

    if current_offered is None and power_offered_entity_id:
        state = sensor.hass.states.get(power_offered_entity_id)
        if state and state.state not in ("unknown", "unavailable", None, ""):
            try:
                power_w = float(state.state)
                phases = sensor._phases or 1
                voltage = (
                    sensor.hub_entry.data.get(CONF_PHASE_VOLTAGE, DEFAULT_PHASE_VOLTAGE)
                    if sensor.hub_entry
                    else DEFAULT_PHASE_VOLTAGE
                )
                if voltage > 0 and phases > 0:
                    current_offered = power_w / (voltage * phases)
                    _LOGGER.debug(
                        "Using power_offered fallback for %s: %.0fW → %.1fA "
                        "(voltage=%dV, phases=%d)",
                        sensor._attr_name,
                        power_w,
                        current_offered,
                        voltage,
                        phases,
                    )
            except (ValueError, TypeError):
                pass

    if current_offered is None:
        return

    update_freq = get_entry_value(
        sensor.config_entry, CONF_UPDATE_FREQUENCY, DEFAULT_UPDATE_FREQUENCY
    )
    tolerance = RAMP_DOWN_RATE * update_freq

    # Skip while the commanded limit is still ramping — the charger's offered
    # current legitimately lags a ramp (up or down), which a single-sample diff
    # cannot tell apart from genuine non-compliance. The Schmitt trigger holds a
    # steady-state command within DEAD_BAND, so a larger change means a ramp.
    prev_limit = getattr(sensor, "_last_compliance_limit", None)
    sensor._last_compliance_limit = sensor._last_commanded_limit
    if prev_limit is not None and abs(sensor._last_commanded_limit - prev_limit) > DEAD_BAND:
        sensor._mismatch_count = 0
        return

    diff = abs(current_offered - sensor._last_commanded_limit)
    if diff > tolerance:
        sensor._mismatch_count += 1
        _LOGGER.debug(
            "Profile mismatch for %s: commanded=%.1fA, offered=%.1fA, diff=%.1fA "
            "(cycle %d/%d)",
            sensor._attr_name,
            sensor._last_commanded_limit,
            current_offered,
            diff,
            sensor._mismatch_count,
            AUTO_RESET_MISMATCH_THRESHOLD,
        )
    else:
        if sensor._mismatch_count > 0:
            _LOGGER.debug(
                "Profile compliance restored for %s (was at %d cycles, %d resets)",
                sensor._attr_name,
                sensor._mismatch_count,
                sensor._profile_reset_count,
            )
        sensor._mismatch_count = 0
        sensor._profile_reset_count = 0
        return

    if sensor._mismatch_count >= AUTO_RESET_MISMATCH_THRESHOLD:
        sensor._mismatch_count = 0
        sensor._profile_reset_count += 1

        if sensor._profile_reset_count >= ESCALATION_PROFILE_RESET_LIMIT:
            _LOGGER.warning(
                "Escalating to hard reset for %s: profile reset failed %d times",
                sensor._attr_name,
                sensor._profile_reset_count,
            )
            await perform_hard_reset(sensor)
            sensor._profile_reset_count = 0
            sensor._last_hard_reset_at = datetime.now(timezone.utc)
            sensor._last_auto_reset_at = None
        else:
            _LOGGER.info(
                "Auto-reset %d/%d for %s: charger offered %.1fA but we commanded %.1fA",
                sensor._profile_reset_count,
                ESCALATION_PROFILE_RESET_LIMIT,
                sensor._attr_name,
                current_offered,
                sensor._last_commanded_limit,
            )
            sensor._last_auto_reset_at = datetime.now(timezone.utc)
            try:
                await sensor.hass.services.async_call(
                    DOMAIN,
                    "reset_ocpp_evse",
                    {"entry_id": sensor.config_entry.entry_id},
                )
            except Exception as e:
                _LOGGER.error(
                    "Auto-reset service call failed for %s: %s", sensor._attr_name, e
                )


async def perform_hard_reset(sensor) -> None:
    """Perform an OCPP hard reset by pressing the charger's reset button entity."""
    # The OCPP reset button is named after the OCPP charge point ID, not the
    # Load Juggler entity_id — same resolution as the connector/control
    # entities in load.py and hub_calculation.py.
    charger_id = sensor.config_entry.data.get(
        CONF_CHARGER_ID
    ) or sensor.config_entry.data.get(CONF_ENTITY_ID)
    if not charger_id:
        _LOGGER.error(
            "Cannot hard reset %s: no OCPP charger ID configured", sensor._attr_name
        )
        return

    reset_entity_id = f"button.{charger_id}_reset"
    state = sensor.hass.states.get(reset_entity_id)

    if state is None:
        _LOGGER.warning(
            "Hard reset entity %s not found for %s — falling back to profile reset",
            reset_entity_id,
            sensor._attr_name,
        )
        try:
            await sensor.hass.services.async_call(
                DOMAIN,
                "reset_ocpp_evse",
                {"entry_id": sensor.config_entry.entry_id},
            )
        except Exception as e:
            _LOGGER.error(
                "Fallback profile reset failed for %s: %s", sensor._attr_name, e
            )
        return

    _LOGGER.info("Hard OCPP reset for %s via %s", sensor._attr_name, reset_entity_id)
    try:
        await sensor.hass.services.async_call(
            "button",
            "press",
            {"entity_id": reset_entity_id},
            blocking=True,
        )
    except Exception as e:
        _LOGGER.error("Hard reset failed for %s: %s", sensor._attr_name, e)
