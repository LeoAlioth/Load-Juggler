"""Inverter battery write-control — charge rate, and the SOC ceiling.

The clipping forecast says how fast this battery may fill so the midday peak
still has somewhere to go. This module turns that number into a write to the
inverter's own charge-limit register (Deye: maximum battery charge current).

Four rules shape it, and all four exist because the target is a Modbus
register rather than a software knob:

1. **Opt-in.** Nothing is written until the user turns the inverter's Battery
   Charge Control switch on. A mis-picked entity should be harmless.
2. **Paced DIRECTIONALLY** (:func:`down_window_value`). The advice is now
   memoryless direct feedback (see ``calculations.forecast``), so it moves with
   the plant every cycle and the pacing is where the volatility is absorbed —
   asymmetrically, because the two directions cost different things:

   * **Upward** — eligible on every site cycle. A rise is a permission to
     refill, it is already bounded to one Excess trigger margin per write by
     rule 3, and it is the direction the masked-site self-creep escapes in
     (one margin per cycle). Nothing waits for a clock.
   * **Downward** — a reduction must PERSIST for a whole window before it is
     written, and the window is ``CONF_CHARGE_CONTROL_INTERVAL`` (300 s by
     default). Every sample in the window must agree that the register is too
     high, and the value written is the window's MAXIMUM — the least reduction
     all of them agree on, so the instant the window fills cannot pick a
     momentary dip. Any sample back at the register clears the window: the
     reduction did not persist. This is what a kettle, a passing cloud or a car
     plugging in runs into, and it is why they cost no register traffic.
   * **Exempt** — the cycle the forecast's charge gate ENGAGES (``limiting``
     False → True: the destination hold or the reservation taking hold) writes
     down at once. That is a protective regime transition, not a steady-state
     correction, and only steady-state corrections are made lazy.

   Some firmwares commit these registers to EEPROM, and the measured cost of
   the whole arrangement is ~54 writes a day against the ~31 of the design it
   replaced — for ~62 Wh of curtailment a cloudy household day instead of
   ~370 Wh.
3. **Slew-limited upward, instant downward** (:func:`slew_limited`). Lowering
   the limit is the protection direction and lands in one write. Raising it is
   a permission to refill, and the written value may climb by at most one
   Excess trigger margin's worth of watts per write — because a battery handed
   its full rate in a single step drinks the whole clipping reserve out of
   exportable power in minutes, which is the reserve's own purpose reversed.
4. **Restored, once it gets there.** When the advice releases — evening, no
   clipping expected — the normal value is the ramp's destination rather than
   its first step: each write climbs by the slew step, and only when the ramp
   lands does the applied-state marker clear, so a released limit stops being
   written for the rest of the night. That unwind keeps the interval as a plain
   minimum time between writes — it is not a steady-state correction being
   paced, it is a finite ramp with somewhere to arrive, and the reserve it is
   handing back is exactly what a faster ramp would spend. Switching the
   control OFF is the one restore that is still a single write: while disarmed
   we are not entitled to go on writing the register at all, so the unwind
   cannot be spread over a ramp we would have no standing to finish.

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

That same read-back has a second consumer, and it is not a sensor: while the
limit is engaged, the register value is *the rate the battery is permitted to
take*, so it is republished in watts as ``INVERTER_RT_ENFORCED_CHARGE_W`` for
the calculation engine — which narrows the Excess verdict's battery charge
allowance to it instead of the configured nameplate rate (see
``engine/fleet.charge_power_total``). A clipping window is exactly when the site
has surplus it cannot place, so the verdict must not go on counting a charge
rate this module is actively forbidding. None means nothing is being held back.

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
import math

from ..const import (
    DOMAIN,
    CONF_EXCESS_TRIGGER_MARGIN,
    DEFAULT_EXCESS_TRIGGER_MARGIN,
    CONF_CHARGE_LIMIT_ENTITY_ID,
    CONF_CHARGE_LIMIT_UNIT,
    CONF_CHARGE_LIMIT_NORMAL,
    CONF_CHARGE_LIMIT_MINIMUM,
    CONF_CHARGE_CONTROL_INTERVAL,
    CONF_CHARGE_CONTROL_DEADBAND_W,
    CONF_BATTERY_VOLTAGE_ENTITY_ID,
    CONF_BATTERY_NOMINAL_VOLTAGE,
    CONF_SOC_LIMIT_ENTITY_IDS,
    CONF_SOC_LIMIT_NORMAL_ENTITY_ID,
    CHARGE_LIMIT_UNIT_AMPS,
    DEFAULT_CHARGE_LIMIT_UNIT,
    DEFAULT_CHARGE_LIMIT_NORMAL,
    DEFAULT_CHARGE_LIMIT_MINIMUM,
    DEFAULT_CHARGE_CONTROL_INTERVAL,
    DEFAULT_CHARGE_CONTROL_DEADBAND_W,
    DEFAULT_BATTERY_NOMINAL_VOLTAGE,
    DEFAULT_SOC_LIMIT_NORMAL,
    SOC_LIMIT_DEADBAND,
    INVERTER_RT_APPLIED,
    INVERTER_RT_CONTROL_ENABLED,
    INVERTER_RT_ENFORCED_CHARGE_W,
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

# The downward persistence window's own state, and the gate state it needs to
# spot an engagement. Both are per inverter, both live only in the runtime dict
# (so a reload simply restarts the persistence — the safe-conservative
# direction: a fresh window can only defer a reduction, never invent one), and
# both are in the TARGET REGISTER's units like the three above.
#
# The samples are ``(monotonic, desired)`` pairs, pruned to the shortest run
# that still covers the window (see :func:`note_reduction`), so the list is
# bounded by window ÷ site cycle plus one whatever the cadence is.
INVERTER_RT_DOWN_SAMPLES = "charge_control_down_samples"
INVERTER_RT_GATE = "charge_control_gate"  # the forecast's ``limiting``, last cycle

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


def _entity_step(hass, entity_id):
    """The target register's own quantum, when it advertises one.

    Deye's ``Battery Max Charge Current`` is integer amps: written 6.2 it holds
    6. Harmless on its own, but the write decision compares against the
    register's LIVE value, so the 0.2 A it silently dropped is a standing
    disagreement between what we want and what we read. Larger than the
    deadband, that disagreement is a write every cycle, for ever — which is
    exactly the Modbus and EEPROM churn the deadband exists to prevent, caused
    by the deadband being smaller than the device's own resolution.

    Quantising the target first removes the disagreement instead of relying on
    the deadband to be wide enough to hide it. None when the entity does not
    advertise a step, which keeps every such site byte-identical to before.
    """
    state = hass.states.get(entity_id) if entity_id else None
    if state is None:
        return None
    try:
        step = float(state.attributes.get("step"))
    except (TypeError, ValueError):
        return None
    return step if step > 0 else None


def quantise(value, step):
    """``value`` snapped to the register's own grid, rounding DOWN.

    Down, not nearest: every value this module writes is a permit — a ceiling
    on what the battery may draw — so the rounding direction has a meaning.
    Rounding up would hand out a few watts more than the advice allowed, which
    on the reservation path is headroom the forecast was trying to keep.

    A step of None (unknown) leaves the value alone.
    """
    if not step or step <= 0:
        return value
    return math.floor(value / step) * step

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


def from_target_units(value: float, unit: str, voltage: float) -> float:
    """The inverse: a register value back into watts.

    Needed because the enforced rate is published for the calculation engine
    (``INVERTER_RT_ENFORCED_CHARGE_W``), which works in watts throughout and
    must not have to know that this register may count DC amps.
    """
    if unit == CHARGE_LIMIT_UNIT_AMPS:
        return value * voltage
    return value


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


def should_write(current, desired, previous_applied, deadband, deadband_up=None) -> bool:
    """Whether ``desired`` is far enough from what the register already holds.

    Compared against the register's live value, so an inverter that rounded or
    rejected the last write is corrected rather than assumed. ``previous_applied``
    covers the case where the register cannot be read back at all.

    DIRECTIONAL, for the same reason the pacing is. The configured deadband is a
    percentage of the NORMAL value, which is the right scale for a reduction —
    that is where register churn costs something, and the persistence window
    already gates those. It is the wrong scale for a rise: while the limit is
    engaged the advice lives between zero and one Excess trigger margin (the
    export overshoot above the setpoint, which is all there is to correct), so a
    deadband sized to the full register span swallows half the range the
    controller actually works in. Observed live 2026-08-30: an advice hovering
    at 4–6 A against a 4.2 A deadband left the register at 0 for fifty minutes
    while export sat ~470 W above its setpoint.

    A rise is the cheap direction to get wrong — it permits absorption, and the
    approach is self-terminating, since export reaching the setpoint takes the
    error to zero and no further rise is asked for. Coming back down is what the
    window is for. ``deadband_up`` of None keeps the old symmetric behaviour.
    """
    reference = current if current is not None else previous_applied
    if reference is None:
        return True
    band = deadband
    if deadband_up is not None and desired > reference:
        band = deadband_up
    return abs(reference - desired) >= max(band, 0)


def slew_margin_w(hub_entry) -> float:
    """How many watts the written value may RISE per write.

    The site's **Excess trigger margin**, read from the HUB entry — these
    controls are handed the inverter's own entry, and this is a site-level
    number, so the caller threads its hub alongside it exactly as
    ``control/ocpp.py`` and ``control/power_station.py`` are given the site
    voltage.

    Deliberately that setting rather than a knob of its own, and the reason is
    arithmetic rather than convenience: the engaged advice is anchored one
    trigger margin below the export limit, so on a site that masks its own
    production the advice self-creeps upward by about one margin per write (see
    ``engine/hub_result``'s anchoring comment). A ramp bounded at exactly that
    number therefore lets the escape-from-masking through untouched, while
    still refusing the one thing this exists to refuse — a step from a held-down
    limit to full rate.

    A hub that cannot be resolved falls back to the default, which is the number
    an unconfigured site would have given anyway.
    """
    if hub_entry is None:
        return float(DEFAULT_EXCESS_TRIGGER_MARGIN)
    return float(
        get_entry_value(
            hub_entry, CONF_EXCESS_TRIGGER_MARGIN, DEFAULT_EXCESS_TRIGGER_MARGIN
        )
        or 0
    )


def slew_step(margin_w, unit, voltage, deadband):
    """The largest upward move allowed per write, in the target's own units.

    None means "no rate limit at all", which is what a site with no Excess
    trigger margin gets: there is no natural step to borrow, and inventing one
    would be this module deciding site policy.

    Never smaller than the deadband. A step the deadband would swallow is not a
    slower ramp, it is *no* ramp — every write suppressed and the register left
    where it was — so a release would never complete and the battery would stay
    held down for the rest of the day. The floor makes the ramp's own steps
    always worth writing.
    """
    step = to_target_units(max(float(margin_w), 0.0), unit, voltage)
    if step <= 0:
        return None
    return max(step, abs(deadband or 0))


def slew_limited(desired, baseline, step):
    """``desired``, held to at most ``baseline + step`` on the way UP.

    Asymmetric on purpose, and the asymmetry is the whole feature. Downward is
    the protection direction: engaging a limit is what this control exists for
    and it lands in one write. Upward is a permission to refill, and a
    permission granted all at once has the battery absorb the clipping reserve
    from exportable power in a few minutes — the reserve spent on precisely the
    energy it was being kept for.

    No baseline (nothing written yet, unreadable register) or no step means
    there is no rate to measure a rise against, so the value stands.
    """
    if baseline is None or step is None or desired <= baseline:
        return desired
    return min(desired, baseline + step)


def note_reduction(samples, now_mono, value, window_s):
    """Fold one below-the-register sample into the persistence window.

    Returns the new sample list. Pruning keeps the SHORTEST run that still spans
    ``window_s``: leading samples are dropped only while the next one is already
    old enough to prove the coverage, so the list is bounded by
    ``window ÷ site cycle + 1`` and the oldest retained stamp is still what
    :func:`down_window_value` measures the window against.

    Pure function — unit-testable.
    """
    kept = list(samples or [])
    kept.append((now_mono, value))
    span = max(float(window_s or 0.0), 0.0)
    while len(kept) > 1 and (now_mono - kept[1][0]) >= span:
        kept.pop(0)
    return kept


def down_window_value(samples, now_mono, window_s):
    """The reduction a full window agrees on, or None while it is still filling.

    The window is full when its oldest sample is at least ``window_s`` old —
    every sample in it is below the register by construction, since the caller
    clears the window on any sample that is not (a reduction that did not
    persist is not a reduction). The value returned is the MAXIMUM of them: the
    least reduction all of them agree on, so the eligibility instant cannot pick
    a momentary deep dip and the register is never driven below what the plant
    sustained for the whole window.

    Pure function — unit-testable.
    """
    if not samples:
        return None
    if (now_mono - samples[0][0]) < max(float(window_s or 0.0), 0.0):
        return None
    return max(value for _stamp, value in samples)


def ramp_baseline(applied, current):
    """What an upward move is measured from: the last value we WROTE.

    Our own last write rather than the read-back, so the ramp advances on the
    same wall clock the pacing already runs on instead of on how promptly the
    inverter's integration happens to re-poll the register. A ramp that stalled
    waiting for a read-back would leave the battery limited indefinitely, which
    is the one failure worse than releasing too fast.

    The read-back is the fallback for the case with no memory to use: the first
    write after a reload, where the register itself is the only record of where
    the limit stands.
    """
    return applied if applied is not None else current


async def send_inverter_charge_limit(
    hass, entry, hub_entry, advice_w, now_mono, limiting=None
) -> None:
    """Apply (or release) this inverter's battery charge limit.

    ``advice_w`` is the forecast's recommended charge limit in watts, or None
    when the forecast has nothing to say — which is also the release signal.
    ``hub_entry`` is the inverter's hub, carrying the site-level Excess trigger
    margin that sets the upward slew step (:func:`slew_margin_w`).

    ``limiting`` is the forecast's charge GATE for this cycle
    (``forecast_charge_limiting``, published per inverter). It is not the value
    and it is not a duplicate of it: the downward persistence window has to tell
    a protective regime transition (the gate engaging) from a steady-state
    correction, and only the second is made lazy. None means the caller has no
    gate state to offer — a hub that publishes none, or a call from a test of
    something else — and then every reduction is treated as protective and
    written at once, which is the pre-window behaviour and errs toward writing.
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

    # One setting, two jobs, both "how long a change has to be true": the
    # downward persistence window while the limit is engaged, and the plain
    # minimum time between writes on the release ramp (and in the SOC control
    # below).
    interval = (
        get_entry_value(entry, CONF_CHARGE_CONTROL_INTERVAL, None)
        or DEFAULT_CHARGE_CONTROL_INTERVAL
    )
    deadband_w = get_entry_value(
        entry, CONF_CHARGE_CONTROL_DEADBAND_W, DEFAULT_CHARGE_CONTROL_DEADBAND_W
    )
    # Watts in, the register's own unit out — the same conversion the slew step
    # makes, so one setting means the same physical thing whether this register
    # counts DC amps or watts. It used to be a percentage of the normal value,
    # which scaled it to the register's full span rather than to the band the
    # control actually works in (see the constant).
    deadband = abs(to_target_units(float(deadband_w or 0), unit, voltage))
    # The register's own quantum. Every value written below is snapped to it, so
    # the read-back the write decision compares against matches what we asked
    # for instead of the device's rounding of it (see ``_entity_step``).
    reg_step = _entity_step(hass, target_entity)

    applied = inverter_rt.get(INVERTER_RT_APPLIED)
    last_write = inverter_rt.get(INVERTER_RT_LAST_WRITE)
    # The ramp: how far up one write may go, and where "up" is measured from.
    step = slew_step(slew_margin_w(hub_entry), unit, voltage, deadband)
    # The RISE deadband, scaled to the range rises actually cover rather than to
    # the register's full span: a third of the largest single rise allowed, so
    # crossing that range costs a handful of writes rather than one or ten.
    # Never larger than the configured deadband, so this can only make the
    # controller more responsive upward, never less.
    #
    # The divisor is calibrated, not chosen: it is the smallest that keeps every
    # write budget in dev/tests/test_charge_control_loop.py (a two-hour burst
    # train inside 10 writes, a cloudy day inside 120). A tenth cost 70 % more
    # writes than those budgets allow — these registers go over Modbus and some
    # firmwares put them in EEPROM. See ``should_write`` for why up and down
    # want different bands at all.
    deadband_up = deadband if step is None else min(deadband, abs(step) / 3.0)
    baseline = ramp_baseline(applied, current)
    releasing = not enabled or advice_w is None
    # The gate edge the exemption keys on. Tracked here rather than handed in as
    # an event because this module is the only thing that needs the edge — and a
    # reload starting from "nothing known" makes the first engaged cycle after it
    # an engagement, which is right: the register's standing is unknown then too,
    # so the protective write should land rather than wait out a window.
    was_gated = inverter_rt.get(INVERTER_RT_GATE)
    inverter_rt[INVERTER_RT_GATE] = limiting
    engaging = bool(limiting) and not bool(was_gated)

    if releasing:
        # Steady state, not the event: the restore itself is a log line, so
        # the sensor doesn't flash a one-cycle "released" nobody sees.
        inverter_rt[INVERTER_RT_STATUS] = (
            CONTROL_STATE_OFF if not enabled else CONTROL_STATE_IDLE
        )
        inverter_rt[INVERTER_RT_RECOMMENDED] = None
        # Nothing is being held back, so the battery is permitted its full
        # nameplate rate again — which is what None tells the engine. Set on the
        # way out rather than beside the write, which happens on some released
        # cycles and not others, while this must be true on all of them. The ramp
        # below does still hold the register down for a few minutes, and the
        # engine deliberately does not hear about it: that is a transport detail
        # of a decision already taken, and feeding it back would let a release we
        # are in the middle of unwinding steer the very advice that released it.
        inverter_rt[INVERTER_RT_ENFORCED_CHARGE_W] = None
        # Nothing is being held down, so there is no reduction to be persistent
        # about. Cleared on the way out for the same reason it is cleared after a
        # write: a window is only ever about the limit standing right now.
        inverter_rt[INVERTER_RT_DOWN_SAMPLES] = []
        # Only ever undo our own limit. Never having written (applied is None)
        # means the register is the user's, not ours.
        if applied is None or normal is None:
            return

        if not enabled:
            # Disarmed — a user event, and a terminal one: from the next cycle
            # this control writes nothing at all, so the unwind cannot be spread
            # over a ramp it would have no standing to finish. One write, now,
            # and the register is theirs again.
            await _write(
                hass, entry, target_entity, quantise(normal, reg_step), unit
            )
            inverter_rt[INVERTER_RT_APPLIED] = None
            inverter_rt[INVERTER_RT_LAST_WRITE] = now_mono
            _LOGGER.info(
                "%s: charge control switched off — restored %s to %.1f%s",
                entry.title,
                target_entity,
                normal,
                unit,
            )
            return

        # Armed, and the forecast has let go: ramp back to full rate rather than
        # stepping to it. "Less reserve needed, you may refill" must not mean
        # "refill at maximum, starting now" — the advice self-heals upward as the
        # clip burns down, and each self-heal used to hand the battery the whole
        # normal value for one burst of exportable power.
        # Against the QUANTISED normal, not the raw one. quantise rounds down,
        # so a normal value off the register's grid (84.5 on a whole-amp
        # register) is unreachable — and "landed" is what ends the ramp, so
        # comparing against the raw value would hold the release open for ever,
        # writing nothing and never clearing its marker.
        normal_q = quantise(normal, reg_step)
        target = round(quantise(slew_limited(normal, baseline, step), reg_step), 1)
        landed = target >= normal_q
        if last_write is not None and (now_mono - last_write) < interval:
            return
        writing = should_write(
            current, target, applied, deadband, deadband_up
        )
        if not writing and not landed:
            # A ramp step the deadband swallowed. Hold the marker and try again
            # next window rather than declaring the release finished here.
            return
        if writing:
            await _write(hass, entry, target_entity, target, unit)
            inverter_rt[INVERTER_RT_LAST_WRITE] = now_mono
            _LOGGER.info(
                "%s: charge limit released — %s %s to %.1f%s of %.1f%s",
                entry.title,
                "restored" if landed else "ramping",
                target_entity,
                target,
                unit,
                normal,
                unit,
            )
        else:
            _LOGGER.debug(
                "%s: charge limit released — %s is already within the deadband "
                "of its %.1f%s normal, nothing left to restore",
                entry.title,
                target_entity,
                normal,
                unit,
            )
        # The marker is what makes a finished release stop writing for the rest
        # of the night, so it clears only once the ramp has reached full rate —
        # including the case just above, where the register already sits close
        # enough to it that a write is not worth making.
        inverter_rt[INVERTER_RT_APPLIED] = None if landed else target
        return

    # The floor applies to the ENGAGED value only, and after the conversion —
    # it is stored in the register's own units, so it cannot be folded into the
    # watt advice. The release path above is deliberately untouched: restoring
    # full rate is never "too low". Nothing upstream sees this — the advice
    # pipeline and the published forecast_charge_limit_w sensors stay unclamped.
    desired = max(
        to_target_units(advice_w, unit, voltage),
        resolve_minimum_value(entry, normal),
    )
    # The ramp applies here too, and for the same reason: an engaged limit that
    # self-heals upward (98 → 99 → 100 % of the ceiling, as the clip burns down)
    # is the same "you may refill" in smaller words. Rounded AFTER the slew, so a
    # rise of exactly one margin — the masked-site self-creep — is not shaved by
    # a hundredth and passes through untouched.
    target = round(quantise(slew_limited(desired, baseline, step), reg_step), 1)
    inverter_rt[INVERTER_RT_STATUS] = CONTROL_STATE_LIMITING
    # The recommendation stays the ADVICE (floored), not the ramp's next step:
    # the sensor's job is to report what we want the register at, while the ramp
    # toward it shows up in the register read-back the same sensor graphs.
    inverter_rt[INVERTER_RT_RECOMMENDED] = round(desired, 1)
    # What the battery is PERMITTED to take right now, in watts, for the
    # calculation engine's Excess verdict (see INVERTER_RT_ENFORCED_CHARGE_W).
    # The register's live value, not our desired one: until the write below
    # actually lands — the pacing can defer it for a whole interval — the
    # battery really may still take the full rate the register still holds, and
    # narrowing the allowance before that would engage Excess against a battery
    # that has not been held back yet. Only when the register cannot be read at
    # all does the value we are driving it to stand in for it — the ramp's next
    # step, since that is what we are actually driving it to.
    inverter_rt[INVERTER_RT_ENFORCED_CHARGE_W] = from_target_units(
        target if current is None else current, unit, voltage
    )

    # --- Directional pacing (rule 2) ------------------------------------------
    # Where the reduction is measured from: the same reference the deadband uses,
    # so the two agree about what "the register already holds" means.
    reference = current if current is not None else applied
    # A reduction to be persistent about: the gate is known, this is not the
    # protective cycle the gate engaged on, and the value really is below what
    # the register holds by more than the deadband.
    reducing = (
        limiting is not None
        and not engaging
        and reference is not None
        and desired < reference - deadband
    )
    if limiting is None:
        # No gate state to reason about (see the signature): the pre-window
        # contract, one minimum interval between writes in either direction.
        inverter_rt[INVERTER_RT_DOWN_SAMPLES] = []
        if last_write is not None and (now_mono - last_write) < interval:
            return
    elif not reducing:
        # A rise, a move inside the deadband, or a protective transition. The
        # window is about one standing reduction and this is not it — and a rise
        # is not paced at all: it is bounded by the slew step above, and the
        # masked-site self-creep escapes at one margin per cycle.
        inverter_rt[INVERTER_RT_DOWN_SAMPLES] = []
    else:
        samples = note_reduction(
            inverter_rt.get(INVERTER_RT_DOWN_SAMPLES), now_mono, desired, interval
        )
        inverter_rt[INVERTER_RT_DOWN_SAMPLES] = samples
        settled = down_window_value(samples, now_mono, interval)
        if settled is None:
            # Not yet a persistent reduction. The register keeps what it holds,
            # which is the higher (charge-favouring) value.
            _LOGGER.debug(
                "%s: %.1f%s is below the register but has held for only %.0fs of"
                " %ss — deferring the reduction",
                entry.title,
                desired,
                unit,
                now_mono - samples[0][0],
                interval,
            )
            return
        # The least reduction every sample in the window agreed on.
        target = round(quantise(slew_limited(settled, baseline, step), reg_step), 1)

    if not should_write(current, target, applied, deadband, deadband_up):
        return

    await _write(hass, entry, target_entity, target, unit)
    inverter_rt[INVERTER_RT_APPLIED] = target
    inverter_rt[INVERTER_RT_LAST_WRITE] = now_mono
    # Interval-scoped, like every part of this filter: whatever the last window
    # concluded, it has been acted on.
    inverter_rt[INVERTER_RT_DOWN_SAMPLES] = []
    _LOGGER.info(
        "%s: forecast advises %.0f W — wrote %.1f%s%s to %s (was %s)",
        entry.title,
        advice_w,
        target,
        unit,
        # Only when the ramp actually held it back, so a normal write reads the
        # way it always did.
        "" if target >= desired else f" (ramping toward {desired:.1f}{unit})",
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
