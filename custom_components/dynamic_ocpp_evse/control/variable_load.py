"""Variable Load control (JUG-30): publish the allowance, write the setpoint.

Every command cycle the per-leg allowance the processor settled on is turned
into the published figures (allowed current per leg and lowest across the legs
in use, allowed power, percent of maximum, the phase advice) and, when a
setpoint entity is configured, written to it in its unit. Without one the load
is output-only: it publishes and writes nothing. Phase switching is advice
only - nothing here ever writes a phase.
"""

import logging

from ..calculations.variable_load import SINGLE, outputs, phase_advice, phase_value, setpoint
from ..const import (
    CONF_VL_PHASE_ENTITY_ID,
    EVSE_MODE_HOLD,
    RT_PHASE_SWITCH_AT,
    CONF_PHASE_VOLTAGE,
    CONF_VL_BIAS,
    CONF_VL_FALLBACK_SETPOINT,
    CONF_VL_PHASE_MIN_INTERVAL,
    CONF_VL_PHASE_MULTI_ABOVE,
    CONF_VL_PHASE_SINGLE_BELOW,
    CONF_VL_RESEND_INTERVAL,
    CONF_VL_SETPOINT_ENTITY_ID,
    CONF_VL_SETPOINT_UNIT,
    DEFAULT_PHASE_VOLTAGE,
    DEFAULT_VL_BIAS,
    DEFAULT_VL_PHASE_MIN_INTERVAL,
    DEFAULT_VL_PHASE_MULTI_ABOVE,
    DEFAULT_VL_PHASE_SINGLE_BELOW,
    DEFAULT_VL_RESEND_INTERVAL,
    DEFAULT_VL_SETPOINT_UNIT,
    VL_RT_ADVICE,
    VL_RT_ALLOWED_A,
    VL_RT_LAST_WRITE,
    VL_RT_LEGS_IN_USE,
    VL_RT_OUTPUTS,
    VL_RT_SIZED_COUNT,
)
from ..helpers import get_entry_value, stored_legs, variable_load_amps
from . import call_device, stamp_command
from .. import units

_LOGGER = logging.getLogger(__name__)


def _attr(state, key):
    try:
        return float(state.attributes[key])
    except (AttributeError, KeyError, TypeError, ValueError):
        return None


async def send_variable_load_command(
    sensor, limit: float, hub_entry, now_mono: float, potential: float | None = None,
    offered: float = 0.0,
) -> None:
    """Publish this cycle's allowance (``limit``, amps per leg) and write it.

    ``potential``: per leg on all its configured legs while it runs on fewer
    (None otherwise) - the phase advice reads that, so a device on one leg is
    told to switch back when all its legs could take more than the threshold.
    ``offered``: the engine's permit for it - for a load not running (Hold, not
    connected, not charging) what it would be offered - so the advice of a held
    load is what it would resume on, not the single of its 0 A (issue 21).
    """
    entry = sensor.config_entry
    load_rt = sensor._runtime()
    voltage = get_entry_value(hub_entry, CONF_PHASE_VOLTAGE, DEFAULT_PHASE_VOLTAGE)
    legs = stored_legs(entry) or ["A"]
    sized = load_rt.get(VL_RT_SIZED_COUNT) or len(legs)
    max_amps = variable_load_amps(entry, voltage, sized)[1]

    load_rt[VL_RT_ALLOWED_A] = limit
    # What it now holds: a later cut that keeps it running goes out at once
    # (control/smoothing.cut_now), as for a charger.
    sensor._last_commanded_limit = limit
    published = outputs(limit, legs, load_rt.get(VL_RT_LEGS_IN_USE), voltage, max_amps)
    if len(legs) > 1:
        advice_power = round(max(limit, offered or 0.0) * voltage * len(load_rt.get(VL_RT_LEGS_IN_USE) or legs))
        if potential is not None:
            # capped at what the device takes on all its legs
            advice_power = round(
                min(potential, variable_load_amps(entry, voltage, len(legs))[1]) * voltage * len(legs)
            )
        if sensor._operating_mode == EVSE_MODE_HOLD.key and load_rt.get(VL_RT_ADVICE):
            # Hold claims nothing, so there is no allowance to judge a phase
            # count by: the advice stays as it was (GitHub issue 21).
            published.update(phase_advice=load_rt[VL_RT_ADVICE][0], phase_advice_reason="held (Hold mode)")
        else:
            advice, reason, load_rt[VL_RT_ADVICE] = phase_advice(
                advice_power,
                load_rt.get(VL_RT_ADVICE),
                now_mono,
                get_entry_value(entry, CONF_VL_PHASE_MULTI_ABOVE, DEFAULT_VL_PHASE_MULTI_ABOVE),
                get_entry_value(entry, CONF_VL_PHASE_SINGLE_BELOW, DEFAULT_VL_PHASE_SINGLE_BELOW),
                60 * get_entry_value(entry, CONF_VL_PHASE_MIN_INTERVAL, DEFAULT_VL_PHASE_MIN_INTERVAL),
            )
            published.update(phase_advice=advice, phase_advice_reason=reason)
            await _write_phase(sensor, load_rt, advice, now_mono)
    load_rt[VL_RT_OUTPUTS] = published

    target = get_entry_value(entry, CONF_VL_SETPOINT_ENTITY_ID, None)
    if target:
        state = sensor.hass.states.get(target)
        if units.is_unavailable(state):
            _LOGGER.debug("Variable load %s: setpoint %s unavailable", sensor._attr_name, target)
        else:
            value = setpoint(
                limit,
                get_entry_value(entry, CONF_VL_SETPOINT_UNIT, DEFAULT_VL_SETPOINT_UNIT),
                voltage,
                sized,
                float(get_entry_value(entry, CONF_VL_BIAS, DEFAULT_VL_BIAS) or 0),
                _attr(state, "min"),
                _attr(state, "max"),
                _attr(state, "step"),
            )
            current = units.read_number(sensor.hass, target)
            resend = float(get_entry_value(entry, CONF_VL_RESEND_INTERVAL, DEFAULT_VL_RESEND_INTERVAL) or 0)
            due = resend > 0 and now_mono - load_rt.get(VL_RT_LAST_WRITE, -float("inf")) >= resend
            if current is None or abs(current - value) >= 0.01 or due:
                try:
                    await call_device(
                        sensor.hass, load_rt, target.split(".", 1)[0], "set_value",
                        {"entity_id": target, "value": value}, "state", value,
                    )
                    load_rt[VL_RT_LAST_WRITE] = now_mono
                except Exception as e:
                    _LOGGER.warning("Variable load %s: setpoint write failed: %s", sensor._attr_name, e)
            _LOGGER.debug(
                "Variable load %s: %.1f A/leg -> setpoint %s (was %s)",
                sensor._attr_name, limit, value, current,
            )

    stamp_command(sensor, now_mono)


async def _write_phase(sensor, load_rt, advice, now_mono) -> None:
    """Write the advice to the device's phase entity, when one is set and it
    holds another value (1 / 3, calculations/variable_load.phase_value). A
    change starts the settling window in which the device's 0 A while it
    switches is not a manual change (control.manual_change)."""
    target = get_entry_value(sensor.config_entry, CONF_VL_PHASE_ENTITY_ID, None)
    state = sensor.hass.states.get(target) if target else None
    if units.is_unavailable(state):
        return
    domain = target.split(".", 1)[0]
    is_select = domain in ("select", "input_select")
    value = phase_value(advice, state.attributes.get("options") if is_select else None)
    if value is None:
        _LOGGER.warning(
            "Variable load %s: phase entity %s has no option for %s phase(s)",
            sensor._attr_name, target, 1 if advice == SINGLE else 3,
        )
        return
    held = state.state if is_select else units.read_number(sensor.hass, target)
    if held == value or (not is_select and held is not None and abs(held - value) < 0.5):
        return
    load_rt[RT_PHASE_SWITCH_AT] = now_mono
    service, key = ("select_option", "option") if is_select else ("set_value", "value")
    try:
        await call_device(
            sensor.hass, load_rt, domain, service, {"entity_id": target, key: value}, "state", value,
        )
        _LOGGER.info("Variable load %s: phase entity %s -> %s", sensor._attr_name, target, value)
    except Exception as e:
        _LOGGER.warning("Variable load %s: phase write failed: %s", sensor._attr_name, e)


async def write_fallback(hass, entry, load_rt=None) -> None:
    """Unload (a reload, a removal, a disable): write the configured fallback
    setpoint, the entity-setpoint stand-in for a charger's profile running out.
    Not set (the default): nothing is written and the device keeps its last
    setpoint. A device's own watchdog (the re-send interval) covers a stop
    that does not unload."""
    target = get_entry_value(entry, CONF_VL_SETPOINT_ENTITY_ID, None)
    value = get_entry_value(entry, CONF_VL_FALLBACK_SETPOINT, None)
    if not target or value is None or units.is_unavailable(hass.states.get(target)):
        return
    try:
        await call_device(
            hass, load_rt if load_rt is not None else {}, target.split(".", 1)[0], "set_value",
            {"entity_id": target, "value": float(value)}, "state", float(value),
        )
    except Exception as e:
        _LOGGER.warning("Variable load %s: fallback setpoint write failed: %s", entry.title, e)
