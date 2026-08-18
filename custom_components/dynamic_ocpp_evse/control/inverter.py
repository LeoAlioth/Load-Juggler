"""Inverter battery charge write-control.

The clipping forecast says how fast this battery may fill so the midday peak
still has somewhere to go. This module turns that number into a write to the
inverter's own charge-limit register (Deye: maximum battery charge current).

Three rules shape it, and all three exist because the target is a Modbus
register rather than a software knob:

1. **Opt-in.** Nothing is written until the user turns the inverter's Battery
   Charge Control switch on. A mis-picked entity should be harmless.
2. **Paced.** A write happens only on a meaningful change (deadband, a
   percentage of the normal value) and never more often than the configured
   interval. Some firmwares commit these registers to EEPROM.
3. **Restored exactly once.** When the advice releases — evening, no clipping
   expected, control switched off — the normal value goes back and the
   applied-state marker clears, so a released limit is not rewritten every
   cycle for the rest of the night.
"""

import logging

from ..const import (
    DOMAIN,
    CONF_CHARGE_LIMIT_ENTITY_ID,
    CONF_CHARGE_LIMIT_UNIT,
    CONF_CHARGE_LIMIT_NORMAL,
    CONF_CHARGE_CONTROL_INTERVAL,
    CONF_CHARGE_CONTROL_DEADBAND,
    CONF_BATTERY_VOLTAGE_ENTITY_ID,
    CONF_BATTERY_NOMINAL_VOLTAGE,
    CHARGE_LIMIT_UNIT_AMPS,
    DEFAULT_CHARGE_LIMIT_UNIT,
    DEFAULT_CHARGE_LIMIT_NORMAL,
    DEFAULT_CHARGE_CONTROL_INTERVAL,
    DEFAULT_CHARGE_CONTROL_DEADBAND,
    DEFAULT_BATTERY_NOMINAL_VOLTAGE,
    INVERTER_RT_APPLIED,
    INVERTER_RT_CONTROL_ENABLED,
    INVERTER_RT_LAST_WRITE,
    INVERTER_RT_STATUS,
)
from ..helpers import get_entry_value
from .. import units

_LOGGER = logging.getLogger(__name__)


def _read_number(hass, entity_id, unit=None):
    """Current numeric state of ``entity_id``, or None if unusable.

    ``unit`` converts through units.py — needed for the battery voltage, whose
    sensor may publish millivolts. Left None for the target register itself:
    we read it back only to compare against the value we are about to write,
    in whatever unit that register uses.
    """
    if not entity_id:
        return None
    state = hass.states.get(entity_id)
    if units.is_unavailable(state):
        return None
    try:
        value = float(state.state)
    except (TypeError, ValueError):
        return None
    if unit == units.DOMAIN_VOLTS:
        value = units.to_volts(value, state.attributes.get("unit_of_measurement"))
    return None if units.is_unusable_number(value) else value


def _entity_max(hass, entity_id):
    """The target number entity's own maximum, when it advertises one."""
    state = hass.states.get(entity_id) if entity_id else None
    if state is None:
        return None
    try:
        return float(state.attributes.get("max"))
    except (TypeError, ValueError):
        return None


def battery_voltage(hass, entry) -> float:
    """DC battery voltage for the W↔A conversion: the live sensor when one is
    configured and readable, else the configured nominal."""
    nominal = (
        get_entry_value(entry, CONF_BATTERY_NOMINAL_VOLTAGE, None)
        or DEFAULT_BATTERY_NOMINAL_VOLTAGE
    )
    live = _read_number(
        hass,
        get_entry_value(entry, CONF_BATTERY_VOLTAGE_ENTITY_ID, None),
        unit=units.DOMAIN_VOLTS,
    )
    if live and live > 0:
        return live
    return float(nominal)


def to_target_units(watts: float, unit: str, voltage: float) -> float:
    """Convert a watt figure into what the target register expects."""
    if unit == CHARGE_LIMIT_UNIT_AMPS:
        return watts / voltage if voltage > 0 else 0.0
    return watts


def resolve_normal_value(hass, entry, target_entity):
    """The value written when the limit is released.

    A configured normal wins (stored in the target's own units); 0 means "the
    register's own maximum", which is where an unmanaged inverter sits.
    Returns None when neither is knowable — in that case a release simply
    leaves the register alone rather than guessing a number.
    """
    configured = get_entry_value(
        entry, CONF_CHARGE_LIMIT_NORMAL, DEFAULT_CHARGE_LIMIT_NORMAL
    )
    if configured:
        return float(configured)
    return _entity_max(hass, target_entity)


def should_write(current, desired, previous_applied, deadband) -> bool:
    """Whether ``desired`` is far enough from what the register already holds.

    Compared against the register's live value, so an inverter that rounded or
    rejected the last write is corrected rather than assumed. ``previous_applied``
    covers the case where the register cannot be read back at all.
    """
    reference = current if current is not None else previous_applied
    if reference is None:
        return True
    return abs(reference - desired) >= max(deadband, 0)


async def send_inverter_charge_limit(hass, entry, advice_w, now_mono) -> None:
    """Apply (or release) this inverter's battery charge limit.

    ``advice_w`` is the forecast's recommended charge limit in watts, or None
    when the forecast has nothing to say — which is also the release signal.
    """
    target_entity = get_entry_value(entry, CONF_CHARGE_LIMIT_ENTITY_ID, None)
    if not target_entity:
        return

    inverter_rt = (
        hass.data.get(DOMAIN, {}).get("inverters", {}).setdefault(entry.entry_id, {})
    )
    enabled = inverter_rt.get(INVERTER_RT_CONTROL_ENABLED, False)
    unit = (
        get_entry_value(entry, CONF_CHARGE_LIMIT_UNIT, DEFAULT_CHARGE_LIMIT_UNIT)
        or DEFAULT_CHARGE_LIMIT_UNIT
    )
    voltage = battery_voltage(hass, entry)
    normal = resolve_normal_value(hass, entry, target_entity)

    interval = (
        get_entry_value(entry, CONF_CHARGE_CONTROL_INTERVAL, None)
        or DEFAULT_CHARGE_CONTROL_INTERVAL
    )
    deadband_pct = get_entry_value(
        entry, CONF_CHARGE_CONTROL_DEADBAND, DEFAULT_CHARGE_CONTROL_DEADBAND
    )
    # The deadband is a percentage of the normal value so one setting works
    # whether the register counts amps or watts.
    deadband = abs((normal or 0) * (deadband_pct or 0) / 100.0)

    applied = inverter_rt.get(INVERTER_RT_APPLIED)
    releasing = not enabled or advice_w is None

    if releasing:
        # Steady state, not the event: the restore itself is a log line, so
        # the sensor doesn't flash a one-cycle "Released" nobody sees.
        inverter_rt[INVERTER_RT_STATUS] = "Off" if not enabled else "Not limiting"
        # Only ever undo our own limit, and only once. Never having written
        # (applied is None) means the register is the user's, not ours.
        if applied is None or normal is None:
            return
        await _write(hass, entry, target_entity, normal, unit)
        inverter_rt[INVERTER_RT_APPLIED] = None
        inverter_rt[INVERTER_RT_LAST_WRITE] = now_mono
        _LOGGER.info(
            "%s: charge limit released — restored %s to %.1f%s",
            entry.title,
            target_entity,
            normal,
            unit,
        )
        return

    desired = round(to_target_units(advice_w, unit, voltage), 1)
    inverter_rt[INVERTER_RT_STATUS] = f"Limiting to {desired:.1f}{unit}"

    last_write = inverter_rt.get(INVERTER_RT_LAST_WRITE)
    if last_write is not None and (now_mono - last_write) < interval:
        return
    current = _read_number(hass, target_entity)
    if not should_write(current, desired, applied, deadband):
        return

    await _write(hass, entry, target_entity, desired, unit)
    inverter_rt[INVERTER_RT_APPLIED] = desired
    inverter_rt[INVERTER_RT_LAST_WRITE] = now_mono
    _LOGGER.info(
        "%s: forecast advises %.0f W — wrote %.1f%s to %s (was %s)",
        entry.title,
        advice_w,
        desired,
        unit,
        target_entity,
        f"{current:.1f}" if current is not None else "unknown",
    )


async def _write(hass, entry, entity_id, value, unit) -> None:
    """Write a value to the target number entity, failing soft."""
    try:
        await hass.services.async_call(
            "number",
            "set_value",
            {"entity_id": entity_id, "value": value},
            blocking=False,
        )
    except Exception as err:  # noqa: BLE001 — a dead inverter must not stop the loop
        _LOGGER.warning(
            "Charge-limit write failed for %s (%s = %.1f%s): %s",
            entry.title,
            entity_id,
            value,
            unit,
            err,
        )
