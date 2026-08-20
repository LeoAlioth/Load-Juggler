"""Tests for the inverter battery write-controls — control.inverter.

Machine-authored tests — not yet human-reviewed.

Two controls, one contract each.

The forecast's recommended **charge limit** only reaches the inverter when the
user armed the control switch, the value moved by more than the deadband, and
the write interval has elapsed. Releasing puts the normal value back exactly
once.

The recommended **max SOC** is fanned out across every configured time-of-use
slot, each with its own deadband, and is always ``min(normal, recommendation)``
— so it has no release event at all, and the "normal" side is a live entity that
external automations keep owning. Its section is further down the file.

**Who calls this** changed and these tests did not have to: the control used to
be ticked by the charge-control sensor's 10 s platform poll, and is now awaited
once per site cycle (default 2 s) as a site-cycle worker — see
``entities/mixins.SiteCycleWorkerMixin``. The pacing is wall-clock, so the two
cadences are the same contract; the section at the bottom drives the control at
both of them and pins that. The entity-level half of the change (registration
with the hub's worker bucket, the write happening through the real coordinator
cycle, and ``async_update`` no longer writing) lives in ``test_sensor_update.py``
where the HA fixtures are, with source-level guards for it here.

These use a hand-rolled fake hass rather than the HA fixtures, so the file runs
in the pure tier too. Runnable two ways:
  python3 dev/tests/test_inverter_control.py   (standalone, no pytest needed)
  pytest dev/tests/test_inverter_control.py    (Docker / CI tier)
"""

import ast
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
from standalone_loader import load_pure_modules  # noqa: E402

# control/inverter.py imports nothing but const/helpers/units — the actuation
# layer's own rule — so it loads without Home Assistant installed.
load_pure_modules(calc_modules=(), control_modules=("inverter",))

from custom_components.dynamic_ocpp_evse.const import (  # noqa: E402
    DOMAIN,
    CONF_CHARGE_LIMIT_ENTITY_ID,
    CONF_CHARGE_LIMIT_UNIT,
    CONF_CHARGE_LIMIT_NORMAL,
    CONF_CHARGE_CONTROL_INTERVAL,
    CONF_CHARGE_CONTROL_DEADBAND,
    CONF_BATTERY_NOMINAL_VOLTAGE,
    CONF_BATTERY_VOLTAGE_ENTITY_ID,
    CONF_SOC_LIMIT_ENTITY_IDS,
    CONF_SOC_LIMIT_NORMAL_ENTITY_ID,
    DEFAULT_SOC_LIMIT_NORMAL,
    SOC_LIMIT_DEADBAND,
    INVERTER_RT_APPLIED,
    INVERTER_RT_CONTROL_ENABLED,
    INVERTER_RT_STATUS,
    INVERTER_RT_SOC_CONTROL_ENABLED,
    INVERTER_RT_SOC_LAST_WRITE,
    INVERTER_RT_SOC_STATUS,
)
from custom_components.dynamic_ocpp_evse.control.inverter import (  # noqa: E402
    CONTROL_STATE_IDLE,
    CONTROL_STATE_LIMITING,
    CONTROL_STATE_OFF,
    INVERTER_RT_NORMAL,
    INVERTER_RT_RECOMMENDED,
    INVERTER_RT_REGISTER,
    INVERTER_RT_SOC_DESIRED,
    INVERTER_RT_SOC_NORMAL,
    INVERTER_RT_SOC_RECOMMENDED,
    INVERTER_RT_SOC_SLOTS,
    battery_voltage,
    desired_soc,
    resolve_normal_soc,
    resolve_normal_value,
    send_inverter_charge_limit,
    send_inverter_soc_limit,
    should_write,
    soc_targets,
    to_target_units,
)

COMPONENT = Path(__file__).resolve().parents[2] / "custom_components" / "dynamic_ocpp_evse"

TARGET = "number.deye_max_charge_current"


class _States:
    def __init__(self, states):
        self._states = states

    def get(self, entity_id):
        return self._states.get(entity_id)

    def set(self, entity_id, state, **attributes):
        self._states[entity_id] = SimpleNamespace(
            state=str(state), attributes=attributes
        )


class _Services:
    def __init__(self):
        self.calls = []

    async def async_call(self, domain, service, data, blocking=False):
        self.calls.append((domain, service, data))


class _Hass:
    def __init__(self, states=None):
        self.states = _States(states or {})
        self.services = _Services()
        self.data = {DOMAIN: {"inverters": {}}}


def _entry(options=None):
    return SimpleNamespace(
        entry_id="inv1",
        title="Deye Hybrid",
        data={},
        options={CONF_CHARGE_LIMIT_ENTITY_ID: TARGET, **(options or {})},
    )


def _hass_with_target(current=50.0, maximum=100.0, **extra_states):
    hass = _Hass()
    hass.states.set(TARGET, current, max=maximum)
    for entity_id, (state, attrs) in extra_states.items():
        hass.states.set(entity_id, state, **attrs)
    return hass


def _arm(hass, entry, enabled=True):
    hass.data[DOMAIN]["inverters"][entry.entry_id] = {
        INVERTER_RT_CONTROL_ENABLED: enabled
    }
    return hass.data[DOMAIN]["inverters"][entry.entry_id]


# --- Conversion and helpers ---------------------------------------------------


def test_watts_to_amps_uses_battery_voltage():
    assert to_target_units(5120.0, "A", 51.2) == 100.0


def test_watts_passthrough_when_target_takes_watts():
    assert to_target_units(5120.0, "W", 51.2) == 5120.0


def test_battery_voltage_prefers_live_sensor():
    hass = _Hass()
    hass.states.set("sensor.batt_v", 53.4, unit_of_measurement="V")
    entry = _entry({
        CONF_BATTERY_VOLTAGE_ENTITY_ID: "sensor.batt_v",
        CONF_BATTERY_NOMINAL_VOLTAGE: 51.2,
    })
    assert battery_voltage(hass, entry) == 53.4


def test_battery_voltage_falls_back_to_nominal():
    hass = _Hass()
    entry = _entry({CONF_BATTERY_NOMINAL_VOLTAGE: 25.6})
    assert battery_voltage(hass, entry) == 25.6


def test_normal_value_defaults_to_target_entity_max():
    hass = _hass_with_target(maximum=120.0)
    assert resolve_normal_value(hass, _entry(), TARGET) == 120.0


def test_configured_normal_value_wins():
    hass = _hass_with_target(maximum=120.0)
    entry = _entry({CONF_CHARGE_LIMIT_NORMAL: 90})
    assert resolve_normal_value(hass, entry, TARGET) == 90.0


# --- Deadband -----------------------------------------------------------------


def test_deadband_blocks_a_small_move():
    assert should_write(current=50.0, desired=52.0, previous_applied=None, deadband=5) is False


def test_deadband_allows_a_real_move():
    assert should_write(current=50.0, desired=44.0, previous_applied=None, deadband=5) is True


def test_unreadable_register_falls_back_to_what_we_applied():
    # The register cannot be read back; compare against our own last write.
    assert should_write(None, 50.0, 50.0, 5) is False
    assert should_write(None, 40.0, 50.0, 5) is True


def test_never_written_and_unreadable_writes():
    assert should_write(None, 40.0, None, 5) is True


# --- The write path -----------------------------------------------------------


def test_nothing_written_while_the_switch_is_off():
    hass = _hass_with_target()
    entry = _entry()
    rt = _arm(hass, entry, enabled=False)

    asyncio.run(send_inverter_charge_limit(hass, entry, 3000.0, 0.0))

    assert hass.services.calls == []
    assert rt[INVERTER_RT_STATUS] == CONTROL_STATE_OFF


def test_armed_control_writes_the_converted_limit():
    hass = _hass_with_target(current=100.0, maximum=100.0)
    entry = _entry({CONF_BATTERY_NOMINAL_VOLTAGE: 51.2})
    rt = _arm(hass, entry)

    # 2560 W at 51.2 V = 50 A, half the register's current 100 A
    asyncio.run(send_inverter_charge_limit(hass, entry, 2560.0, 0.0))

    assert hass.services.calls == [
        ("number", "set_value", {"entity_id": TARGET, "value": 50.0})
    ]
    assert rt[INVERTER_RT_APPLIED] == 50.0
    assert rt[INVERTER_RT_STATUS] == CONTROL_STATE_LIMITING
    # The value the charge-control sensor publishes as its recommendation, in the
    # register's own units — the "Limiting to 50.0A" string used to carry it.
    assert rt[INVERTER_RT_RECOMMENDED] == 50.0


def test_second_cycle_inside_the_interval_does_not_write():
    hass = _hass_with_target(current=100.0, maximum=100.0)
    entry = _entry({
        CONF_BATTERY_NOMINAL_VOLTAGE: 51.2,
        CONF_CHARGE_CONTROL_INTERVAL: 300,
    })
    _arm(hass, entry)

    asyncio.run(send_inverter_charge_limit(hass, entry, 2560.0, 1000.0))
    hass.states.set(TARGET, 50.0, max=100.0)
    # A much lower advice, but only 60 s later
    asyncio.run(send_inverter_charge_limit(hass, entry, 1024.0, 1060.0))

    assert len(hass.services.calls) == 1


def test_write_resumes_after_the_interval():
    hass = _hass_with_target(current=100.0, maximum=100.0)
    entry = _entry({
        CONF_BATTERY_NOMINAL_VOLTAGE: 51.2,
        CONF_CHARGE_CONTROL_INTERVAL: 300,
    })
    _arm(hass, entry)

    asyncio.run(send_inverter_charge_limit(hass, entry, 2560.0, 1000.0))
    hass.states.set(TARGET, 50.0, max=100.0)
    asyncio.run(send_inverter_charge_limit(hass, entry, 1024.0, 1400.0))

    assert len(hass.services.calls) == 2
    assert hass.services.calls[-1][2]["value"] == 20.0


def test_release_restores_the_normal_value_once():
    hass = _hass_with_target(current=100.0, maximum=100.0)
    entry = _entry({
        CONF_BATTERY_NOMINAL_VOLTAGE: 51.2,
        CONF_CHARGE_CONTROL_INTERVAL: 0,
    })
    rt = _arm(hass, entry)

    asyncio.run(send_inverter_charge_limit(hass, entry, 2560.0, 0.0))
    hass.states.set(TARGET, 50.0, max=100.0)
    # Forecast has nothing to say any more (evening, or no clipping ahead)
    asyncio.run(send_inverter_charge_limit(hass, entry, None, 10.0))
    hass.states.set(TARGET, 100.0, max=100.0)
    asyncio.run(send_inverter_charge_limit(hass, entry, None, 20.0))

    assert len(hass.services.calls) == 2  # one limit, one restore
    assert hass.services.calls[-1][2]["value"] == 100.0
    assert rt[INVERTER_RT_APPLIED] is None
    assert rt[INVERTER_RT_STATUS] == CONTROL_STATE_IDLE
    assert rt[INVERTER_RT_RECOMMENDED] is None


def test_release_without_a_prior_write_leaves_the_register_alone():
    """A register we never touched is the user's — never 'restore' it."""
    hass = _hass_with_target(current=42.0, maximum=100.0)
    entry = _entry()
    rt = _arm(hass, entry)

    asyncio.run(send_inverter_charge_limit(hass, entry, None, 0.0))

    assert hass.services.calls == []
    assert rt[INVERTER_RT_STATUS] == CONTROL_STATE_IDLE


def test_disarming_restores_the_normal_value():
    hass = _hass_with_target(current=100.0, maximum=100.0)
    entry = _entry({
        CONF_BATTERY_NOMINAL_VOLTAGE: 51.2,
        CONF_CHARGE_CONTROL_INTERVAL: 0,
    })
    rt = _arm(hass, entry)
    asyncio.run(send_inverter_charge_limit(hass, entry, 2560.0, 0.0))

    rt[INVERTER_RT_CONTROL_ENABLED] = False
    asyncio.run(send_inverter_charge_limit(hass, entry, 2560.0, 10.0))

    assert hass.services.calls[-1][2]["value"] == 100.0
    assert rt[INVERTER_RT_APPLIED] is None


def test_no_target_entity_is_a_no_op():
    hass = _Hass()
    entry = SimpleNamespace(entry_id="inv1", title="Advisory", data={}, options={})

    asyncio.run(send_inverter_charge_limit(hass, entry, 2560.0, 0.0))

    assert hass.services.calls == []


def test_deadband_is_a_percentage_of_the_normal_value():
    """5 % of a 100 A normal is 5 A — a 2 A move is not worth a Modbus write."""
    hass = _hass_with_target(current=100.0, maximum=100.0)
    entry = _entry({
        CONF_BATTERY_NOMINAL_VOLTAGE: 51.2,
        CONF_CHARGE_CONTROL_INTERVAL: 0,
        CONF_CHARGE_CONTROL_DEADBAND: 5,
    })
    _arm(hass, entry)

    # 5017.6 W = 98 A, 2 A below the register's 100 A
    asyncio.run(send_inverter_charge_limit(hass, entry, 5017.6, 0.0))
    assert hass.services.calls == []

    # 4608 W = 90 A, a 10 A move
    asyncio.run(send_inverter_charge_limit(hass, entry, 4608.0, 10.0))
    assert len(hass.services.calls) == 1


# --- The register read-back the charge-control sensor publishes ---------------
#
# That sensor's state is a MEASUREMENT of the target register, and this is where
# the register is read: once per call, before any branch. The point of these is
# that the read is NOT conditional on a write happening — a value that only moved
# when we wrote would be a step function, not a graph.


def test_the_register_read_back_is_recorded_for_the_sensor():
    hass = _hass_with_target(current=100.0, maximum=100.0)
    entry = _entry({CONF_BATTERY_NOMINAL_VOLTAGE: 51.2})
    rt = _arm(hass, entry)

    asyncio.run(send_inverter_charge_limit(hass, entry, 2560.0, 0.0))

    # Read BEFORE the write, so it is the register's value as the inverter last
    # reported it — not an optimistic echo of what we just asked for.
    assert rt[INVERTER_RT_REGISTER] == 100.0
    assert rt[INVERTER_RT_NORMAL] == 100.0

    # The inverter takes the write; a paced-out call still refreshes the read-back
    # even though it writes nothing.
    hass.states.set(TARGET, 50.0, max=100.0)
    asyncio.run(send_inverter_charge_limit(hass, entry, 2560.0, 1.0))

    assert len(hass.services.calls) == 1
    assert rt[INVERTER_RT_REGISTER] == 50.0


def test_the_read_back_is_refreshed_while_the_switch_is_off():
    """The register still holds a real value when our control is off — that is
    the half of the graph the sensor would otherwise lose."""
    hass = _hass_with_target(current=80.0, maximum=100.0)
    entry = _entry()
    rt = _arm(hass, entry, enabled=False)

    asyncio.run(send_inverter_charge_limit(hass, entry, 2560.0, 0.0))

    assert hass.services.calls == []
    assert rt[INVERTER_RT_REGISTER] == 80.0
    assert rt[INVERTER_RT_STATUS] == CONTROL_STATE_OFF


def test_an_unreadable_register_records_none():
    """None is how the sensor says "unknown" — never a stale number, and never 0.
    A 0 A charge limit is a real, very different claim."""
    hass = _Hass()
    hass.states.set(TARGET, "unavailable")
    entry = _entry()
    rt = _arm(hass, entry)

    asyncio.run(send_inverter_charge_limit(hass, entry, 2560.0, 0.0))

    assert rt[INVERTER_RT_REGISTER] is None


# --- Cadence independence -----------------------------------------------------
#
# The check moved from a 10 s platform poll to the site cycle, whose default is
# 2 s — five times as many checks. What must NOT change is how often the
# register is actually written, because that is what wears EEPROM. The pacing is
# measured in wall-clock seconds (``now_mono``), never in cycles, and these
# drive the same hour at several cadences to hold it to that.

WRITE_INTERVAL = 300.0
HOUR = 3600.0


def _write_times(cadence_s, duration_s, interval=WRITE_INTERVAL, advice_w=2560.0,
                 enabled=True):
    """Drive the control every ``cadence_s`` for ``duration_s`` of wall clock.

    Returns the times at which a register write actually happened. The fake
    register never moves (nothing applies the write), so the deadband always
    passes and the interval is the only thing pacing the writes — exactly the
    worst case for a fast cadence.
    """
    hass = _hass_with_target(current=100.0, maximum=100.0)
    entry = _entry({
        CONF_BATTERY_NOMINAL_VOLTAGE: 51.2,
        CONF_CHARGE_CONTROL_INTERVAL: interval,
    })
    _arm(hass, entry, enabled=enabled)

    times = []
    seen = 0
    now = 0.0
    while now <= duration_s:
        asyncio.run(send_inverter_charge_limit(hass, entry, advice_w, now))
        if len(hass.services.calls) > seen:
            seen = len(hass.services.calls)
            times.append(now)
        now += cadence_s
    return times


def test_a_five_times_faster_cadence_writes_exactly_as_often():
    """The whole cadence question in one assertion: 1800 checks an hour and 360
    checks an hour produce the same writes, at the same times."""
    assert _write_times(2.0, HOUR) == _write_times(10.0, HOUR)


def test_no_two_writes_are_ever_closer_than_the_interval():
    """For any cadence, including ones that do not divide the interval."""
    for cadence in (0.5, 2.0, 7.0, 10.0, 30.0):
        times = _write_times(cadence, HOUR)
        gaps = [b - a for a, b in zip(times, times[1:])]
        assert gaps, cadence
        assert min(gaps) >= WRITE_INTERVAL, (cadence, gaps)
        # An hour at one write per 300 s: 13 with the first at t=0, one fewer
        # when the cadence's phase pushes the last one past the hour.
        assert 12 <= len(times) <= 13, (cadence, times)


def test_a_whole_interval_of_checks_produces_one_write():
    # 150 cycles at the 2 s default, all inside the first 300 s window.
    assert _write_times(2.0, WRITE_INTERVAL - 2) == [0.0]


def test_the_switch_gate_holds_for_every_cycle_of_an_hour():
    """The opt-in is checked per call, so a faster cadence cannot leak a write."""
    assert _write_times(2.0, HOUR, enabled=False) == []


# --- The battery SOC ceiling fan-out ------------------------------------------
#
# The SOC twin of everything above, and structurally different in two ways that
# these pin. It writes MANY entities (a Deye's SOC ceiling lives in its
# time-of-use slots, one number entity each), and it never restores anything:
# the value written is always min(normal, recommendation), so the advice climbing
# back to 100 % through the afternoon hands the slots back by itself. The
# "normal" side is a live entity the user's own automations keep owning — an
# unconfigured one means a constant 100, an unreadable one means we write nothing
# at all rather than invent their setting.

SOC_SLOTS = [
    "number.deye_tou_soc_1",
    "number.deye_tou_soc_2",
    "number.deye_tou_soc_3",
]
NORMAL_ENTITY = "input_number.battery_ceiling"


def _soc_entry(options=None, targets=None):
    return SimpleNamespace(
        entry_id="inv1",
        title="Deye Hybrid",
        data={},
        options={
            CONF_SOC_LIMIT_ENTITY_IDS: (
                list(SOC_SLOTS) if targets is None else targets
            ),
            # 1 s, not 0: a 0 reads as "unset" and falls back to the 300 s
            # default (the ``or`` in the control, shared with the charge-rate
            # side). A one-second window keeps the pacing out of the way of the
            # tests that are not about pacing — they step now_mono by 10.
            CONF_CHARGE_CONTROL_INTERVAL: 1,
            **(options or {}),
        },
    )


def _soc_hass(slots=None, normal=None):
    """A fake hass whose SOC slots all sit at 100 unless told otherwise."""
    hass = _Hass()
    for entity_id, value in (slots or {eid: 100 for eid in SOC_SLOTS}).items():
        hass.states.set(entity_id, value)
    if normal is not None:
        hass.states.set(NORMAL_ENTITY, normal)
    return hass


def _arm_soc(hass, entry, enabled=True):
    rt = hass.data[DOMAIN]["inverters"].setdefault(entry.entry_id, {})
    rt[INVERTER_RT_SOC_CONTROL_ENABLED] = enabled
    return rt


def _soc_writes(hass):
    """(entity_id, value) of every set_value call, in the order they were made."""
    return [(data["entity_id"], data["value"]) for _d, _s, data in hass.services.calls]


# --- The two inputs and the min() --------------------------------------------


def test_desired_is_the_lower_of_the_normal_and_the_advice():
    assert desired_soc(100.0, 80.0) == 80.0
    # Advice ABOVE the normal changes nothing — we may only ever hold it lower
    # than whoever owns the slots asked for.
    assert desired_soc(80.0, 90.0) == 80.0
    # No advice at all: track the normal. This is also the release path, since
    # the forecast's advice self-heals to 100 rather than disappearing.
    assert desired_soc(80.0, None) == 80.0


def test_normal_defaults_to_one_hundred_with_no_entity_configured():
    """The developer's explicit choice: a constant, not a read of the slots.

    Reading the slots would ratchet — they may hold a limit WE wrote, so the
    'normal' would follow our own last limit down and never come back up.
    """
    assert resolve_normal_soc(_soc_hass(), _soc_entry()) == DEFAULT_SOC_LIMIT_NORMAL
    assert DEFAULT_SOC_LIMIT_NORMAL == 100.0


def test_normal_is_read_live_from_the_configured_entity():
    hass = _soc_hass(normal=90)
    entry = _soc_entry({CONF_SOC_LIMIT_NORMAL_ENTITY_ID: NORMAL_ENTITY})
    assert resolve_normal_soc(hass, entry) == 90.0


def test_normal_is_none_when_the_configured_entity_cannot_be_read():
    """None is not a value — the caller defers rather than guessing."""
    hass = _soc_hass()
    hass.states.set(NORMAL_ENTITY, "unavailable")
    entry = _soc_entry({CONF_SOC_LIMIT_NORMAL_ENTITY_ID: NORMAL_ENTITY})
    assert resolve_normal_soc(hass, entry) is None


def test_soc_targets_drops_blanks_and_accepts_a_bare_string():
    assert soc_targets(_soc_entry(targets=[])) == []
    assert soc_targets(_soc_entry(targets=["number.a", None, "", "number.b"])) == [
        "number.a",
        "number.b",
    ]
    assert soc_targets(_soc_entry(targets="number.only")) == ["number.only"]


# --- The write path -----------------------------------------------------------


def test_advice_below_the_normal_is_written_to_every_slot():
    """The fan-out itself: one recommendation, N entities, one cycle."""
    hass = _soc_hass()
    entry = _soc_entry()
    rt = _arm_soc(hass, entry)

    asyncio.run(send_inverter_soc_limit(hass, entry, 70.0, 0.0))

    assert _soc_writes(hass) == [(eid, 70.0) for eid in SOC_SLOTS]
    assert rt[INVERTER_RT_SOC_DESIRED] == 70.0
    assert rt[INVERTER_RT_SOC_NORMAL] == 100.0
    assert rt[INVERTER_RT_SOC_RECOMMENDED] == 70.0
    assert rt[INVERTER_RT_SOC_STATUS] == CONTROL_STATE_LIMITING


def test_advice_above_the_normal_writes_the_normal():
    """min() in the write path, not just in the helper: an owner who asked for
    80 gets 80, even while the forecast is happy with 95."""
    hass = _soc_hass(slots={eid: 100 for eid in SOC_SLOTS}, normal=80)
    entry = _soc_entry({CONF_SOC_LIMIT_NORMAL_ENTITY_ID: NORMAL_ENTITY})
    rt = _arm_soc(hass, entry)

    asyncio.run(send_inverter_soc_limit(hass, entry, 95.0, 0.0))

    assert _soc_writes(hass) == [(eid, 80.0) for eid in SOC_SLOTS]
    # Tracking the owner's own ceiling is not "limiting" — we are holding it
    # exactly where they asked, which is the idle standing.
    assert rt[INVERTER_RT_SOC_STATUS] == CONTROL_STATE_IDLE


def test_idle_tracking_propagates_a_change_of_the_normal_entity():
    """With no advice at all the slots still follow the normal entity — that is
    what 'external automations keep owning it' means in practice."""
    hass = _soc_hass(normal=90)
    entry = _soc_entry({CONF_SOC_LIMIT_NORMAL_ENTITY_ID: NORMAL_ENTITY})
    _arm_soc(hass, entry)

    asyncio.run(send_inverter_soc_limit(hass, entry, None, 0.0))
    assert _soc_writes(hass) == [(eid, 90.0) for eid in SOC_SLOTS]

    # The owner's automation moves its ceiling; the slots follow next cycle.
    for eid in SOC_SLOTS:
        hass.states.set(eid, 90)
    hass.states.set(NORMAL_ENTITY, 60)
    asyncio.run(send_inverter_soc_limit(hass, entry, None, 10.0))

    assert _soc_writes(hass)[-3:] == [(eid, 60.0) for eid in SOC_SLOTS]


def test_the_advice_self_healing_to_full_hands_the_slots_back():
    """There is no release event. As the peak passes, the advice climbs to 100
    and the min() is the normal again — no marker, no once-only write."""
    hass = _soc_hass()
    entry = _soc_entry()
    _arm_soc(hass, entry)

    asyncio.run(send_inverter_soc_limit(hass, entry, 70.0, 0.0))
    for eid in SOC_SLOTS:
        hass.states.set(eid, 70)
    asyncio.run(send_inverter_soc_limit(hass, entry, 100.0, 10.0))

    assert _soc_writes(hass)[-3:] == [(eid, 100.0) for eid in SOC_SLOTS]


def test_the_default_hundred_is_what_gets_enforced_without_a_normal_entity():
    hass = _soc_hass(slots={eid: 70 for eid in SOC_SLOTS})
    entry = _soc_entry()
    _arm_soc(hass, entry)

    asyncio.run(send_inverter_soc_limit(hass, entry, None, 0.0))

    assert _soc_writes(hass) == [(eid, 100.0) for eid in SOC_SLOTS]


# --- Per-slot deadband --------------------------------------------------------


def test_a_slot_already_at_the_ceiling_is_skipped_while_its_siblings_are_written():
    """The deadband is applied PER TARGET — the slots are independent, so one of
    them being right must not spend the others' write, and vice versa."""
    hass = _soc_hass(
        slots={
            SOC_SLOTS[0]: 70.4,  # within 1.0 point of the desired 70 — spare it
            SOC_SLOTS[1]: 100,
            SOC_SLOTS[2]: 85,
        }
    )
    entry = _soc_entry()
    _arm_soc(hass, entry)

    asyncio.run(send_inverter_soc_limit(hass, entry, 70.0, 0.0))

    assert _soc_writes(hass) == [(SOC_SLOTS[1], 70.0), (SOC_SLOTS[2], 70.0)]


def test_the_deadband_is_a_fixed_soc_point():
    """Exactly one point, not a percentage of anything: 0.9 away is spared,
    1.0 away is written."""
    assert SOC_LIMIT_DEADBAND == 1.0
    hass = _soc_hass(slots={SOC_SLOTS[0]: 70.9, SOC_SLOTS[1]: 71.0})
    entry = _soc_entry(targets=SOC_SLOTS[:2])
    _arm_soc(hass, entry)

    asyncio.run(send_inverter_soc_limit(hass, entry, 70.0, 0.0))

    assert _soc_writes(hass) == [(SOC_SLOTS[1], 70.0)]


def test_every_slot_at_the_ceiling_writes_nothing_at_all():
    hass = _soc_hass(slots={eid: 70 for eid in SOC_SLOTS})
    entry = _soc_entry()
    _arm_soc(hass, entry)

    asyncio.run(send_inverter_soc_limit(hass, entry, 70.0, 0.0))

    assert _soc_writes(hass) == []


# --- Pacing -------------------------------------------------------------------


def test_the_interval_gates_the_whole_fan_out():
    """One clock for the set: when the window opens every due slot goes, and the
    next window starts from there — not one clock per slot."""
    hass = _soc_hass()
    entry = _soc_entry({CONF_CHARGE_CONTROL_INTERVAL: 300})
    rt = _arm_soc(hass, entry)

    asyncio.run(send_inverter_soc_limit(hass, entry, 70.0, 1000.0))
    assert len(_soc_writes(hass)) == 3
    assert rt[INVERTER_RT_SOC_LAST_WRITE] == 1000.0

    # A much lower ceiling 60 s later: nothing, the window is not open.
    asyncio.run(send_inverter_soc_limit(hass, entry, 40.0, 1060.0))
    assert len(_soc_writes(hass)) == 3

    # Past the interval it resumes, and all three slots are due again.
    asyncio.run(send_inverter_soc_limit(hass, entry, 40.0, 1400.0))
    assert _soc_writes(hass)[-3:] == [(eid, 40.0) for eid in SOC_SLOTS]


def test_a_whole_interval_of_cycles_produces_one_fan_out():
    """150 site cycles at the 2 s default, all inside the first 300 s window."""
    hass = _soc_hass()
    entry = _soc_entry({CONF_CHARGE_CONTROL_INTERVAL: 300})
    _arm_soc(hass, entry)

    now = 0.0
    while now < 298.0:
        asyncio.run(send_inverter_soc_limit(hass, entry, 70.0, now))
        now += 2.0

    assert len(_soc_writes(hass)) == 3


# --- Failure modes ------------------------------------------------------------


def test_an_unreadable_normal_defers_every_write():
    """Never write a guess for somebody else's setting. The slots keep whatever
    they hold — the last thing either we or their owner deliberately put there."""
    hass = _soc_hass()
    hass.states.set(NORMAL_ENTITY, "unavailable")
    entry = _soc_entry({CONF_SOC_LIMIT_NORMAL_ENTITY_ID: NORMAL_ENTITY})
    rt = _arm_soc(hass, entry)

    asyncio.run(send_inverter_soc_limit(hass, entry, 70.0, 0.0))

    assert _soc_writes(hass) == []
    assert rt[INVERTER_RT_SOC_DESIRED] is None
    assert rt[INVERTER_RT_SOC_STATUS] == CONTROL_STATE_IDLE
    # The read-backs are still recorded — the sensor keeps reporting the slots
    # through a failure of the normal entity beside them.
    assert rt[INVERTER_RT_SOC_SLOTS] == {eid: 100.0 for eid in SOC_SLOTS}
    assert rt[INVERTER_RT_SOC_NORMAL] is None


def test_an_unreadable_slot_is_skipped_and_the_others_proceed():
    """One dead entity degrades this to partial control, not to none."""
    hass = _soc_hass()
    hass.states.set(SOC_SLOTS[1], "unknown")
    entry = _soc_entry()
    rt = _arm_soc(hass, entry)

    asyncio.run(send_inverter_soc_limit(hass, entry, 70.0, 0.0))

    assert _soc_writes(hass) == [(SOC_SLOTS[0], 70.0), (SOC_SLOTS[2], 70.0)]
    assert rt[INVERTER_RT_SOC_SLOTS][SOC_SLOTS[1]] is None


def test_nothing_is_written_while_the_soc_switch_is_off():
    hass = _soc_hass()
    entry = _soc_entry()
    rt = _arm_soc(hass, entry, enabled=False)

    for now in range(20):
        asyncio.run(send_inverter_soc_limit(hass, entry, 40.0, float(now)))

    assert _soc_writes(hass) == []
    assert rt[INVERTER_RT_SOC_STATUS] == CONTROL_STATE_OFF
    assert rt[INVERTER_RT_SOC_DESIRED] is None
    # Still reading, though — that is the half of the graph the sensor would
    # otherwise lose while the control is disarmed.
    assert rt[INVERTER_RT_SOC_SLOTS] == {eid: 100.0 for eid in SOC_SLOTS}


def test_an_unarmed_control_is_off_even_before_the_switch_exists():
    """No runtime dict at all (before the switch has restored its state) must
    read as disarmed, not as armed-by-default."""
    hass = _soc_hass()
    entry = _soc_entry()

    asyncio.run(send_inverter_soc_limit(hass, entry, 40.0, 0.0))

    assert _soc_writes(hass) == []
    rt = hass.data[DOMAIN]["inverters"][entry.entry_id]
    assert rt[INVERTER_RT_SOC_STATUS] == CONTROL_STATE_OFF


def test_no_soc_targets_is_a_no_op():
    """An inverter that only uses the charge-rate control must be untouched by
    this one — including its runtime dict, which stays free of SOC keys."""
    hass = _soc_hass()
    entry = _soc_entry(targets=[])

    asyncio.run(send_inverter_soc_limit(hass, entry, 40.0, 0.0))

    assert _soc_writes(hass) == []
    assert hass.data[DOMAIN]["inverters"] == {}


def test_an_input_number_target_is_written_through_its_own_service():
    """The slots may be input_numbers the user maintains, so the service domain
    follows the entity id rather than being hard-coded to `number`."""
    target = "input_number.tou_soc_1"
    hass = _soc_hass(slots={target: 100})
    entry = _soc_entry(targets=[target])
    _arm_soc(hass, entry)

    asyncio.run(send_inverter_soc_limit(hass, entry, 70.0, 0.0))

    assert hass.services.calls == [
        ("input_number", "set_value", {"entity_id": target, "value": 70.0})
    ]


def test_the_slot_read_backs_are_taken_before_the_write():
    """The sensor's per-slot attribute must be what the inverter last reported,
    not an optimistic echo of what we just asked for."""
    hass = _soc_hass()
    entry = _soc_entry()
    rt = _arm_soc(hass, entry)

    asyncio.run(send_inverter_soc_limit(hass, entry, 70.0, 0.0))

    assert rt[INVERTER_RT_SOC_SLOTS] == {eid: 100.0 for eid in SOC_SLOTS}

    # The inverter takes the writes; the next call refreshes the read-backs even
    # though the deadband now makes it write nothing.
    for eid in SOC_SLOTS:
        hass.states.set(eid, 70)
    asyncio.run(send_inverter_soc_limit(hass, entry, 70.0, 10.0))

    assert len(_soc_writes(hass)) == 3
    assert rt[INVERTER_RT_SOC_SLOTS] == {eid: 70.0 for eid in SOC_SLOTS}


# --- Source-level guards on the drive mechanism -------------------------------
#
# The entity half of this lives in test_sensor_update.py (it needs a real HA
# entity). These pin the three structural decisions in a tier that runs
# anywhere: nothing polls, only the cycle worker writes, and the worker runs
# after the result it consumes has been published.


def _parse(*parts):
    return ast.parse((COMPONENT.joinpath(*parts)).read_text())


def _assigned_names(tree):
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            names += [t.id for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.append(node.target.id)
    return names


def _callee_name(node):
    """The bare name of a call's callee: ``f()`` and ``obj.f()`` both give "f"."""
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    return func.id if isinstance(func, ast.Name) else None


def _functions_calling(tree, callee):
    """Names of the functions in ``tree`` that call ``callee``."""
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if any(
            isinstance(inner, ast.Call) and _callee_name(inner) == callee
            for inner in ast.walk(node)
        ):
            hits.append(node.name)
    return hits


def _method(tree, class_name, method_name):
    cls = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and node.name == class_name
    )
    return next(
        node
        for node in cls.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == method_name
    )


def test_the_sensor_platform_declares_no_scan_interval():
    """SCAN_INTERVAL existed for exactly one polling sensor. Re-adding it would
    put every sensor on this platform back on a second, unrelated clock."""
    assert "SCAN_INTERVAL" not in _assigned_names(_parse("sensor.py"))


def test_the_charge_control_sensor_is_a_site_cycle_worker():
    """And the mixin comes before SensorEntity, so its ``_attr_should_poll =
    False`` wins the MRO over the entity base's default of True."""
    tree = _parse("entities", "inverter.py")
    cls = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef)
        and node.name == "LoadJugglerInverterChargeControlSensor"
    )
    bases = [b.id for b in cls.bases if isinstance(b, ast.Name)]
    assert "SiteCycleWorkerMixin" in bases, bases
    assert bases.index("SiteCycleWorkerMixin") < bases.index("SensorEntity"), bases


def test_the_worker_mixin_turns_polling_off():
    tree = _parse("entities", "mixins.py")
    cls = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and node.name == "SiteCycleWorkerMixin"
    )
    polls = [
        node.value.value
        for node in cls.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(t, ast.Name) and t.id == "_attr_should_poll"
            for t in node.targets
        )
    ]
    assert polls == [False], polls


def test_async_update_refreshes_the_state_and_awaits_nothing():
    """``homeassistant.update_entity`` is a service any automation can call at
    any rate. If it wrote, it would be a second writer on the register — able to
    overlap the cycle's own write and to spend the min-interval budget outside
    the one place that owns it. So it awaits nothing at all."""
    method = _method(
        _parse("entities", "inverter.py"),
        "LoadJugglerInverterChargeControlSensor",
        "async_update",
    )
    awaits = [node for node in ast.walk(method) if isinstance(node, ast.Await)]
    assert awaits == [], [node.lineno for node in awaits]
    assert _callee_name(
        next(node for node in ast.walk(method) if isinstance(node, ast.Call))
    ) == "_read_control_status"


def _callers_of(callee):
    """Which functions, in which component files, call ``callee``."""
    callers = {}
    for path in sorted(COMPONENT.rglob("*.py")):
        if "__pycache__" in str(path):
            continue
        names = _functions_calling(ast.parse(path.read_text()), callee)
        if names:
            callers[str(path.relative_to(COMPONENT))] = names
    return callers


def test_only_the_site_cycle_worker_writes_the_register():
    """One writer, and it is the one the coordinator serializes."""
    callers = _callers_of("send_inverter_charge_limit")
    assert callers == {"entities/inverter.py": ["_async_site_cycle_work"]}, callers


def test_only_a_site_cycle_worker_writes_the_soc_slots():
    """The same guarantee for the SOC fan-out, from its own worker.

    Its own rather than the charge-control sensor's: an inverter may configure
    the SOC slots and no charge-current register, and a control that quietly
    never ticked would be the worst failure available here. Nothing is given up
    — the coordinator awaits its workers one at a time, which is the same thing
    that already serializes the several workers of a multi-inverter site.
    """
    callers = _callers_of("send_inverter_soc_limit")
    assert callers == {"entities/inverter.py": ["_async_site_cycle_work"]}, callers


def test_the_soc_control_sensor_is_a_site_cycle_worker():
    """And the mixin precedes SensorEntity, so its ``_attr_should_poll = False``
    wins the MRO — a poll would be a second, unserialized writer."""
    tree = _parse("entities", "inverter.py")
    cls = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef)
        and node.name == "LoadJugglerInverterSocControlSensor"
    )
    bases = [b.id for b in cls.bases if isinstance(b, ast.Name)]
    assert "SiteCycleWorkerMixin" in bases, bases
    assert bases.index("SiteCycleWorkerMixin") < bases.index("SensorEntity"), bases
    # Availability, unlike its charge-control sibling's, follows the site cycle:
    # this sensor reports our own intention, which is only true while the cycle
    # computing it runs.
    assert "SiteFreshnessMixin" in bases, bases


def test_the_soc_control_sensor_reads_no_entity_state_itself():
    """Everything it reports comes from the runtime dict the control records —
    a second reader of the slots would be a second reader of the device."""
    source = (COMPONENT / "entities" / "inverter.py").read_text()
    assert "hass.states" not in source, "an entity is reading states directly"


def test_workers_are_awaited_after_the_result_is_published():
    """The ordering the write depends on: it consumes
    ``published["inverters"][…]["forecast_charge_limit_w"]``, which does not
    exist until publish_hub_data has stored it."""
    fn = next(
        node
        for node in ast.walk(_parse("sensor.py"))
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "async_run_hub_cycle"
    )
    lines = {
        callee: [
            node.lineno
            for node in ast.walk(fn)
            if isinstance(node, ast.Call) and _callee_name(node) == callee
        ]
        for callee in ("publish_hub_data", "async_run_site_cycle")
    }
    assert len(lines["publish_hub_data"]) == 1, lines
    assert len(lines["async_run_site_cycle"]) == 1, lines
    assert lines["publish_hub_data"][0] < lines["async_run_site_cycle"][0], lines


# --- Runner -------------------------------------------------------------------
if __name__ == "__main__":
    # Deliberately pytest-free: the pure tier has to run on the developer's
    # machine, which has no pytest (dev/tests/conftest.py imports HA anyway).
    failed = []
    for _name, _fn in sorted(list(globals().items())):
        if not _name.startswith("test_") or not callable(_fn):
            continue
        try:
            _fn()
        except Exception as exc:  # noqa: BLE001 - report and continue
            failed.append((_name, exc))
            print(f"FAIL {_name}: {type(exc).__name__}: {exc}")
        else:
            print(f"PASS {_name}")
    print(f"\n{'FAILED' if failed else 'OK'} — {len(failed)} failure(s)")
    sys.exit(1 if failed else 0)
