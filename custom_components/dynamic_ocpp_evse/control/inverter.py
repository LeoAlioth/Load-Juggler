"""Inverter battery write-control — charge rate, and the SOC ceiling.

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

One optional clamp sits on top of them: a configured **minimum charge limit**
(:func:`resolve_minimum_value`) is the lowest value ever written while the
advice is engaged. It exists because a hard 0 stops the battery charging
altogether, so the pack starts serving the house and drifts down off the ceiling
the advice was holding it at. Engaged writes only — a release restores full rate
and can never be "too low" — and nothing upstream is affected: the forecast's
own numbers, and the sensors publishing them, stay unclamped.

Single reader, too. The register is read back once per call — before any branch,
so the paced-out calls and the release path read it as well — both to compare
against what we are about to write and to be republished as the charge-control
sensor's own numeric state. That sensor therefore measures the register without
ever touching it: everything it reports comes from the runtime dict this module
records (``INVERTER_RT_*``).

Single writer: the one caller is the inverter's charge-control sensor, which the
hub coordinator awaits once per site cycle as a site-cycle worker (see
``entities/inverter.py``). Nothing else — no poll, no service call — may write
this register, which is what makes "one write in flight at a time" true. The
pacing above is measured in wall-clock seconds (``now_mono``), never in cycles,
so the site's cadence changes how often the *check* runs and not how often the
register is actually written.

The second half of the module is the **SOC ceiling** control
(``send_inverter_soc_limit``). Same three rules — opt-in via its own switch,
paced, and driven by exactly one site-cycle worker (its own sensor, so that a
site which configures the SOC ceiling and no charge-current register still gets
a tick; the coordinator awaits its workers one at a time, so a single write in
flight holds across both) — and two differences that come straight from the
hardware:

* **It fans out.** On a Deye the ceiling is not one register but one per
  time-of-use slot, so the target is a *list* of entities and the deadband is
  applied per entity: each slot is read back and skipped on its own.
* **It never restores anything.** The value written is always
  ``min(normal, recommendation)``, where ``normal`` is a live entity the user's
  own automations keep owning. There is no applied-state marker and no release
  write: as the advice self-heals to 100 % through the afternoon the min() hands
  the slots straight back, and while no advice exists the control simply tracks
  the normal entity. That is why the charge-rate control's "restored exactly
  once" rule has no twin here — there is no state to unwind.
"""

import logging

from ..const import (
    DOMAIN,
    CONF_CHARGE_LIMIT_ENTITY_ID,
    CONF_CHARGE_LIMIT_UNIT,
    CONF_CHARGE_LIMIT_NORMAL,
    CONF_CHARGE_LIMIT_MINIMUM,
    CONF_CHARGE_CONTROL_INTERVAL,
    CONF_CHARGE_CONTROL_DEADBAND,
    CONF_BATTERY_VOLTAGE_ENTITY_ID,
    CONF_BATTERY_NOMINAL_VOLTAGE,
    CONF_SOC_LIMIT_ENTITY_IDS,
    CONF_SOC_LIMIT_NORMAL_ENTITY_ID,
    CHARGE_LIMIT_UNIT_AMPS,
    DEFAULT_CHARGE_LIMIT_UNIT,
    DEFAULT_CHARGE_LIMIT_NORMAL,
    DEFAULT_CHARGE_LIMIT_MINIMUM,
    DEFAULT_CHARGE_CONTROL_INTERVAL,
    DEFAULT_CHARGE_CONTROL_DEADBAND,
    DEFAULT_BATTERY_NOMINAL_VOLTAGE,
    DEFAULT_SOC_LIMIT_NORMAL,
    SOC_LIMIT_DEADBAND,
    INVERTER_RT_APPLIED,
    INVERTER_RT_CONTROL_ENABLED,
    INVERTER_RT_LAST_WRITE,
    INVERTER_RT_STATUS,
    INVERTER_RT_SOC_CONTROL_ENABLED,
    INVERTER_RT_SOC_LAST_WRITE,
    INVERTER_RT_SOC_STATUS,
)
from ..helpers import get_entry_value
from .. import units

_LOGGER = logging.getLogger(__name__)

# The three standings either control can be in, as its sensor publishes them
# (``control_state`` attribute). Lowercase and value-like on purpose: the
# sensors' own states are numbers, so this is data to filter and template on,
# not a sentence to display. Shared by both controls — the words mean the same
# thing about each, and one definition keeps an automation's `== "limiting"`
# test working against either sensor.
CONTROL_STATE_OFF = "off"  # the switch is not armed — we write nothing
CONTROL_STATE_IDLE = "idle"  # armed, but nothing to hold back right now
CONTROL_STATE_LIMITING = "limiting"  # a forecast limit is being held

# Further runtime keys under hass.data[DOMAIN]["inverters"][entry_id], recorded
# for the charge-control sensor to publish. They live beside the code that sets
# them because this module is the only writer; the sensor imports them from here.
# All three are in the TARGET REGISTER's units (amps or watts per
# CONF_CHARGE_LIMIT_UNIT), so the sensor can report them under one unit.
INVERTER_RT_REGISTER = "charge_control_register"  # last read-back, None if unreadable
INVERTER_RT_NORMAL = "charge_control_normal"  # value a release restores
INVERTER_RT_RECOMMENDED = "charge_control_recommended"  # what we want it at

# The same, for the SOC ceiling control. Every one of these is a percentage —
# one unit for the whole set, unlike the charge-rate keys above.
INVERTER_RT_SOC_NORMAL = "soc_control_normal"  # the live normal ceiling, or 100
INVERTER_RT_SOC_RECOMMENDED = "soc_control_recommended"  # the forecast's advice
INVERTER_RT_SOC_DESIRED = "soc_control_desired"  # min() of the two — what we enforce
INVERTER_RT_SOC_SLOTS = "soc_control_slots"  # {entity_id: read-back or None}
# Throttle marker for the "cannot read the normal ceiling" warning: a misconfigured
# normal entity would otherwise log once per site cycle, forever.
INVERTER_RT_SOC_WARNED = "soc_control_normal_warned"
SOC_NORMAL_WARN_INTERVAL = 300.0  # s between repeats of that warning


def _read_number(hass, entity_id, unit=None):
    """Current numeric state of ``entity_id``, or None if unusable.

    ``unit`` converts through units.py — needed for the battery voltage, whose
    sensor may publish millivolts. Left None for the target register itself: it
    is read in whatever unit that register uses, both to compare against the
    value we are about to write and to be republished as the charge-control
    sensor's own measurement.
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


def resolve_minimum_value(entry, normal):
    """The floor the engaged limit is never written below, in target units.

    A device-protection knob, not forecast policy: a recommendation of 0 is a
    legitimate "add nothing", but a battery held at a hard 0 A serves the house
    from its own cells instead, so the SOC sags away from the ceiling the advice
    was reserving and the recharge that follows is a sawtooth. A couple of amps
    lets the battery cover the house draw and sit still.

    ``normal`` is the value a release restores — full rate, by definition — and
    so is the ceiling of the floor. Clamping happens here rather than at config
    time because the normal may be the register's own live maximum. A normal
    that is not knowable (None) leaves the configured floor as it stands: there
    is nothing to clamp against, and the floor is the user's own number.

    0 (the default) means no floor at all, which is byte-identical to the
    behaviour before this knob existed.
    """
    configured = (
        get_entry_value(entry, CONF_CHARGE_LIMIT_MINIMUM, DEFAULT_CHARGE_LIMIT_MINIMUM)
        or 0
    )
    floor = max(float(configured), 0.0)
    if normal is None:
        return floor
    return min(floor, float(normal))


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

    # The one read of the register per call, taken before any branch: it is both
    # what the deadband compares against (below) and what the charge-control
    # sensor publishes as its measurement. Reading it on every call — including
    # the release path and the paced-out calls that return early — is what makes
    # that sensor a continuous graph instead of a value that only moves when we
    # happen to write. It is a hass.states lookup, not device traffic; the actual
    # Modbus polling belongs to whoever owns the number entity.
    current = _read_number(hass, target_entity)
    inverter_rt[INVERTER_RT_REGISTER] = current
    inverter_rt[INVERTER_RT_NORMAL] = normal

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
        # the sensor doesn't flash a one-cycle "released" nobody sees.
        inverter_rt[INVERTER_RT_STATUS] = (
            CONTROL_STATE_OFF if not enabled else CONTROL_STATE_IDLE
        )
        inverter_rt[INVERTER_RT_RECOMMENDED] = None
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

    # The floor applies to the ENGAGED value only, and after the conversion —
    # it is stored in the register's own units, so it cannot be folded into the
    # watt advice. The release path above is deliberately untouched: restoring
    # full rate is never "too low". Nothing upstream sees this — the advice
    # pipeline and the published forecast_charge_limit_w sensors stay unclamped.
    desired = round(
        max(
            to_target_units(advice_w, unit, voltage),
            resolve_minimum_value(entry, normal),
        ),
        1,
    )
    inverter_rt[INVERTER_RT_STATUS] = CONTROL_STATE_LIMITING
    inverter_rt[INVERTER_RT_RECOMMENDED] = desired

    last_write = inverter_rt.get(INVERTER_RT_LAST_WRITE)
    if last_write is not None and (now_mono - last_write) < interval:
        return
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


def soc_targets(entry) -> list:
    """The SOC-ceiling entities this inverter drives, in configured order.

    A list because the ceiling is per time-of-use slot on the hardware this
    exists for. Empty (the default) means the SOC control is not configured at
    all: no switch, no sensor, nothing written.
    """
    configured = get_entry_value(entry, CONF_SOC_LIMIT_ENTITY_IDS, None) or []
    if isinstance(configured, str):  # a single pick, before HA wraps it in a list
        configured = [configured]
    return [entity_id for entity_id in configured if entity_id]


def resolve_normal_soc(hass, entry):
    """The ceiling to hold when the forecast is not asking for anything lower.

    Read live from the configured entity every cycle, which is what lets an
    external automation keep owning the slots: whatever it writes there is what
    the slots idle at. Returns:

    * the entity's value, when one is configured and readable;
    * :data:`DEFAULT_SOC_LIMIT_NORMAL` (100) when none is configured — the
      ceiling an unmanaged battery sits at, and a constant rather than a read of
      the slots themselves, which we may have written ourselves and would then
      ratchet the normal down to our own last limit;
    * None when an entity IS configured but cannot be read right now. That is
      not a value, so the caller defers every write rather than guessing one.
    """
    normal_entity = get_entry_value(entry, CONF_SOC_LIMIT_NORMAL_ENTITY_ID, None)
    if not normal_entity:
        return DEFAULT_SOC_LIMIT_NORMAL
    return _read_number(hass, normal_entity)


def desired_soc(normal, advice_soc) -> float:
    """The ceiling to enforce: the lower of the normal and the recommendation.

    The whole control, in one line. min() is what makes it safe to point at
    entities somebody else owns — we can only ever hold the battery lower than
    they asked, never higher — and it is also the release mechanism: the
    forecast's advice climbs back to 100 % as the production peak passes, at
    which point the min() is the normal again and the slots are the user's.
    """
    if advice_soc is None:
        return float(normal)
    return float(min(normal, advice_soc))


async def send_inverter_soc_limit(hass, entry, advice_soc, now_mono) -> None:
    """Drive every configured SOC-ceiling entity toward the desired ceiling.

    ``advice_soc`` is the forecast's recommended battery max SOC in percent, or
    None when the forecast has nothing to say — which, unlike the charge-rate
    control, is not a release event but simply "track the normal".

    Read-then-write, with the reads recorded for the SOC-control sensor on every
    call — including the ones the pacing makes return early and the ones taken
    while the switch is off — so that sensor reports what the slots hold without
    ever touching them itself.
    """
    targets = soc_targets(entry)
    if not targets:
        return

    inverter_rt = (
        hass.data.get(DOMAIN, {}).get("inverters", {}).setdefault(entry.entry_id, {})
    )
    enabled = inverter_rt.get(INVERTER_RT_SOC_CONTROL_ENABLED, False)
    normal = resolve_normal_soc(hass, entry)

    # One read per target per call, before any branch — same reasoning as the
    # charge-rate register above: these are hass.states lookups, and a value
    # that only refreshed when we wrote would make the sensor a step function.
    slots = {entity_id: _read_number(hass, entity_id) for entity_id in targets}
    inverter_rt[INVERTER_RT_SOC_SLOTS] = slots
    inverter_rt[INVERTER_RT_SOC_NORMAL] = normal
    inverter_rt[INVERTER_RT_SOC_RECOMMENDED] = advice_soc

    if not enabled:
        inverter_rt[INVERTER_RT_SOC_STATUS] = CONTROL_STATE_OFF
        inverter_rt[INVERTER_RT_SOC_DESIRED] = None
        return

    if normal is None:
        # A configured normal entity that is unavailable right now. Writing
        # anything here would be inventing the user's own setting, so defer the
        # whole cycle — the slots keep whatever they hold, which is the last
        # thing either we or their owner deliberately put there.
        inverter_rt[INVERTER_RT_SOC_STATUS] = CONTROL_STATE_IDLE
        inverter_rt[INVERTER_RT_SOC_DESIRED] = None
        _warn_unreadable_normal(entry, inverter_rt, now_mono)
        return

    desired = round(desired_soc(normal, advice_soc), 1)
    inverter_rt[INVERTER_RT_SOC_DESIRED] = desired
    # "limiting" is specifically "we are holding it below what its owner asked
    # for". Tracking the normal — no advice, or advice at or above it — is idle,
    # even though the writes that keep the slots there are real writes.
    inverter_rt[INVERTER_RT_SOC_STATUS] = (
        CONTROL_STATE_LIMITING if desired < normal else CONTROL_STATE_IDLE
    )

    interval = (
        get_entry_value(entry, CONF_CHARGE_CONTROL_INTERVAL, None)
        or DEFAULT_CHARGE_CONTROL_INTERVAL
    )
    last_write = inverter_rt.get(INVERTER_RT_SOC_LAST_WRITE)
    if last_write is not None and (now_mono - last_write) < interval:
        return

    # Per-target deadband: the fan-out's whole point is that the slots are
    # independent, so one slot already at the ceiling must not spend the other
    # slots' write. An unreadable slot is skipped rather than written blind —
    # its siblings are still driven, so a single dead entity degrades this to
    # partial control instead of none.
    due = []
    for entity_id, current in slots.items():
        if current is None:
            _LOGGER.debug(
                "%s: SOC slot %s is unreadable — skipping it this cycle",
                entry.title,
                entity_id,
            )
            continue
        if abs(current - desired) < SOC_LIMIT_DEADBAND:
            continue
        due.append(entity_id)

    if not due:
        return

    for entity_id in due:
        await _write(hass, entry, entity_id, desired, "%")
    # One clock for the whole fan-out: when the interval opens, every due slot
    # goes in that cycle, and the next window starts from there. Per-slot clocks
    # would let the slots drift apart in time and turn one paced control into N.
    inverter_rt[INVERTER_RT_SOC_LAST_WRITE] = now_mono
    _LOGGER.info(
        "%s: holding battery SOC ceiling at %.1f%% (normal %.1f%%, forecast %s) "
        "— wrote %d of %d slot(s): %s",
        entry.title,
        desired,
        normal,
        f"{advice_soc:.1f}%" if advice_soc is not None else "none",
        len(due),
        len(targets),
        ", ".join(due),
    )


def _warn_unreadable_normal(entry, inverter_rt, now_mono) -> None:
    """Warn about an unreadable normal-ceiling entity, at most occasionally."""
    warned_at = inverter_rt.get(INVERTER_RT_SOC_WARNED)
    if warned_at is not None and (now_mono - warned_at) < SOC_NORMAL_WARN_INTERVAL:
        return
    inverter_rt[INVERTER_RT_SOC_WARNED] = now_mono
    _LOGGER.warning(
        "%s: normal battery SOC ceiling entity %s is unavailable — deferring SOC "
        "writes rather than guessing a ceiling",
        entry.title,
        get_entry_value(entry, CONF_SOC_LIMIT_NORMAL_ENTITY_ID, None),
    )


async def _write(hass, entry, entity_id, value, unit) -> None:
    """Write a value to a target number entity, failing soft.

    The service domain comes from the entity id rather than being hard-coded:
    the SOC ceiling may live in an ``input_number`` the user maintains as well
    as in a ``number`` the inverter integration owns, and both expose the same
    ``set_value`` service with the same schema.
    """
    domain = entity_id.split(".", 1)[0]
    try:
        await hass.services.async_call(
            domain,
            "set_value",
            {"entity_id": entity_id, "value": value},
            blocking=False,
        )
    except Exception as err:  # noqa: BLE001 — a dead inverter must not stop the loop
        _LOGGER.warning(
            "Write failed for %s (%s = %.1f%s): %s",
            entry.title,
            entity_id,
            value,
            unit,
            err,
        )
