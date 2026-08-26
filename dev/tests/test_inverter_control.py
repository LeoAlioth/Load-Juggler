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
    CONF_EXCESS_TRIGGER_MARGIN,
    DEFAULT_EXCESS_TRIGGER_MARGIN,
    CONF_CHARGE_LIMIT_ENTITY_ID,
    CONF_CHARGE_LIMIT_UNIT,
    CONF_CHARGE_LIMIT_NORMAL,
    CONF_CHARGE_LIMIT_MINIMUM,
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
    INVERTER_RT_ENFORCED_CHARGE_W,
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
    INVERTER_RT_DOWN_SAMPLES,
    INVERTER_RT_SOC_SLOTS,
    battery_voltage,
    desired_soc,
    down_window_value,
    from_target_units,
    note_reduction,
    ramp_baseline,
    resolve_minimum_value,
    resolve_normal_soc,
    resolve_normal_value,
    send_inverter_charge_limit,
    send_inverter_soc_limit,
    should_write,
    slew_limited,
    slew_margin_w,
    slew_step,
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


def _hub(margin=None):
    """The inverter's hub entry — it carries the site's Excess trigger margin.

    That margin is the upward slew step, so every call into the charge-limit
    control needs a hub beside the inverter entry. None leaves it unconfigured,
    which is the 500 W default.
    """
    return SimpleNamespace(
        entry_id="hub1",
        title="Site",
        data={},
        options={} if margin is None else {CONF_EXCESS_TRIGGER_MARGIN: margin},
    )


def _send(hass, entry, advice_w, now_mono, hub_entry=None):
    """Drive the charge-limit control for one cycle.

    Wraps the hub entry the control needs for its slew step; tests that are not
    about the slew get an unconfigured hub and therefore the default margin.
    """
    return send_inverter_charge_limit(
        hass, entry, _hub() if hub_entry is None else hub_entry, advice_w, now_mono
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

    asyncio.run(_send(hass, entry, 3000.0, 0.0))

    assert hass.services.calls == []
    assert rt[INVERTER_RT_STATUS] == CONTROL_STATE_OFF


def test_armed_control_writes_the_converted_limit():
    hass = _hass_with_target(current=100.0, maximum=100.0)
    entry = _entry({CONF_BATTERY_NOMINAL_VOLTAGE: 51.2})
    rt = _arm(hass, entry)

    # 2560 W at 51.2 V = 50 A, half the register's current 100 A
    asyncio.run(_send(hass, entry, 2560.0, 0.0))

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

    asyncio.run(_send(hass, entry, 2560.0, 1000.0))
    hass.states.set(TARGET, 50.0, max=100.0)
    # A much lower advice, but only 60 s later
    asyncio.run(_send(hass, entry, 1024.0, 1060.0))

    assert len(hass.services.calls) == 1


def test_write_resumes_after_the_interval():
    hass = _hass_with_target(current=100.0, maximum=100.0)
    entry = _entry({
        CONF_BATTERY_NOMINAL_VOLTAGE: 51.2,
        CONF_CHARGE_CONTROL_INTERVAL: 300,
    })
    _arm(hass, entry)

    asyncio.run(_send(hass, entry, 2560.0, 1000.0))
    hass.states.set(TARGET, 50.0, max=100.0)
    asyncio.run(_send(hass, entry, 1024.0, 1400.0))

    assert len(hass.services.calls) == 2
    assert hass.services.calls[-1][2]["value"] == 20.0


def test_release_ramps_back_to_the_normal_value_and_then_stops():
    """The release is a ramp, not a step — the section near the bottom of this
    file is about the ramp itself; this pins the standing it leaves behind.

    Restored exactly once still holds, just at the END of the ramp: the marker
    clears when full rate is reached, and nothing is written afterwards.
    """
    hass = _hass_with_target(current=100.0, maximum=100.0)
    entry = _entry({
        CONF_BATTERY_NOMINAL_VOLTAGE: 51.2,
        CONF_CHARGE_CONTROL_INTERVAL: 1,
    })
    rt = _arm(hass, entry)

    asyncio.run(_send(hass, entry, 2560.0, 0.0))
    hass.states.set(TARGET, 50.0, max=100.0)
    # Forecast has nothing to say any more (evening, or no clipping ahead).
    # 500 W of margin at 51.2 V is a 9.8 A step, so 50 A does not become 100 A.
    asyncio.run(_send(hass, entry, None, 10.0))
    assert hass.services.calls[-1][2]["value"] == 59.8
    assert rt[INVERTER_RT_APPLIED] == 59.8
    assert rt[INVERTER_RT_STATUS] == CONTROL_STATE_IDLE
    assert rt[INVERTER_RT_RECOMMENDED] is None

    # Let the ramp run to full rate.
    now = 20.0
    while rt[INVERTER_RT_APPLIED] is not None:
        hass.states.set(TARGET, hass.services.calls[-1][2]["value"], max=100.0)
        asyncio.run(_send(hass, entry, None, now))
        now += 10.0

    # Within the 5 A deadband of the 100 A normal, which is where the ramp
    # stops being worth a write (see the ramp section).
    assert hass.services.calls[-1][2]["value"] == 99.0
    landed = len(hass.services.calls)

    # And now it is done: no further write, for any number of released cycles.
    for _ in range(10):
        asyncio.run(_send(hass, entry, None, now))
        now += 10.0
    assert len(hass.services.calls) == landed
    assert rt[INVERTER_RT_APPLIED] is None


def test_release_without_a_prior_write_leaves_the_register_alone():
    """A register we never touched is the user's — never 'restore' it."""
    hass = _hass_with_target(current=42.0, maximum=100.0)
    entry = _entry()
    rt = _arm(hass, entry)

    asyncio.run(_send(hass, entry, None, 0.0))

    assert hass.services.calls == []
    assert rt[INVERTER_RT_STATUS] == CONTROL_STATE_IDLE


def test_disarming_restores_the_normal_value():
    hass = _hass_with_target(current=100.0, maximum=100.0)
    entry = _entry({
        CONF_BATTERY_NOMINAL_VOLTAGE: 51.2,
        CONF_CHARGE_CONTROL_INTERVAL: 0,
    })
    rt = _arm(hass, entry)
    asyncio.run(_send(hass, entry, 2560.0, 0.0))

    rt[INVERTER_RT_CONTROL_ENABLED] = False
    asyncio.run(_send(hass, entry, 2560.0, 10.0))

    assert hass.services.calls[-1][2]["value"] == 100.0
    assert rt[INVERTER_RT_APPLIED] is None


def test_no_target_entity_is_a_no_op():
    hass = _Hass()
    entry = SimpleNamespace(entry_id="inv1", title="Advisory", data={}, options={})

    asyncio.run(_send(hass, entry, 2560.0, 0.0))

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
    asyncio.run(_send(hass, entry, 5017.6, 0.0))
    assert hass.services.calls == []

    # 4608 W = 90 A, a 10 A move
    asyncio.run(_send(hass, entry, 4608.0, 10.0))
    assert len(hass.services.calls) == 1


# --- The minimum charge limit (the floor) -------------------------------------
#
# Why it exists, from a live site: the advice is legitimately 0 W while solar
# sits below the export threshold and the battery is already at the reserved
# ceiling. Written as a hard 0 A the battery stops charging and serves the house
# from its own cells, so the SOC sags a few points, the forecast latch releases,
# the inverter recharges at full rate — a sawtooth (71↔75 % was the observation)
# instead of a hold. A couple of amps is enough to cover the house draw.
#
# The floor is stored in the TARGET REGISTER's units, like the normal value, and
# applies to the engaged write only. The default of 0 must be indistinguishable
# from the behaviour before it existed.


def test_the_floor_is_zero_by_default_and_clamped_to_the_normal():
    assert resolve_minimum_value(_entry(), 100.0) == 0.0
    assert resolve_minimum_value(_entry({CONF_CHARGE_LIMIT_MINIMUM: 2}), 100.0) == 2.0
    # Never above full rate: a floor of 120 on a 100 A normal is 100.
    assert resolve_minimum_value(_entry({CONF_CHARGE_LIMIT_MINIMUM: 120}), 100.0) == 100.0
    # A negative stored value is not a ceiling-raiser either way.
    assert resolve_minimum_value(_entry({CONF_CHARGE_LIMIT_MINIMUM: -5}), 100.0) == 0.0
    # Nothing to clamp against — the configured number stands as the user typed it.
    assert resolve_minimum_value(_entry({CONF_CHARGE_LIMIT_MINIMUM: 2}), None) == 2.0


def test_a_zero_advice_writes_the_floor_instead_of_zero():
    """The bug this exists for: 0 W advice, engaged, with a 2 A floor."""
    hass = _hass_with_target(current=100.0, maximum=100.0)
    entry = _entry({
        CONF_BATTERY_NOMINAL_VOLTAGE: 51.2,
        CONF_CHARGE_LIMIT_MINIMUM: 2,
    })
    rt = _arm(hass, entry)

    asyncio.run(_send(hass, entry, 0.0, 0.0))

    assert hass.services.calls == [
        ("number", "set_value", {"entity_id": TARGET, "value": 2.0})
    ]
    assert rt[INVERTER_RT_APPLIED] == 2.0
    assert rt[INVERTER_RT_STATUS] == CONTROL_STATE_LIMITING
    assert rt[INVERTER_RT_RECOMMENDED] == 2.0


def test_the_default_floor_of_zero_writes_the_advice_as_is():
    """Byte-identical to before the knob existed: 0 W means 0 A."""
    hass = _hass_with_target(current=100.0, maximum=100.0)
    entry = _entry({CONF_BATTERY_NOMINAL_VOLTAGE: 51.2})
    _arm(hass, entry)

    asyncio.run(_send(hass, entry, 0.0, 0.0))

    assert hass.services.calls == [
        ("number", "set_value", {"entity_id": TARGET, "value": 0.0})
    ]


def test_an_advice_above_the_floor_is_untouched():
    hass = _hass_with_target(current=100.0, maximum=100.0)
    entry = _entry({
        CONF_BATTERY_NOMINAL_VOLTAGE: 51.2,
        CONF_CHARGE_LIMIT_MINIMUM: 2,
    })
    _arm(hass, entry)

    # 2560 W at 51.2 V = 50 A, far above the 2 A floor
    asyncio.run(_send(hass, entry, 2560.0, 0.0))

    assert hass.services.calls[-1][2]["value"] == 50.0


def test_the_floor_does_not_apply_to_the_release_write():
    """A release climbs back to full rate — the floor is a lower bound on how far
    we hold the battery back, never a cap on handing it back.

    It climbs there rather than jumping (see the ramp section), so what this pins
    is that the floor is not the destination: every step is above it, and the
    ramp ends at the normal value, not at 2 A.
    """
    hass = _hass_with_target(current=100.0, maximum=100.0)
    entry = _entry({
        CONF_BATTERY_NOMINAL_VOLTAGE: 51.2,
        CONF_CHARGE_CONTROL_INTERVAL: 1,
        CONF_CHARGE_LIMIT_MINIMUM: 2,
    })
    rt = _arm(hass, entry)

    asyncio.run(_send(hass, entry, 0.0, 0.0))
    assert hass.services.calls[-1][2]["value"] == 2.0

    now = 10.0
    while rt[INVERTER_RT_APPLIED] is not None:
        hass.states.set(TARGET, hass.services.calls[-1][2]["value"], max=100.0)
        asyncio.run(_send(hass, entry, None, now))
        now += 10.0

    released = [call[2]["value"] for call in hass.services.calls[1:]]
    assert released[0] == 11.8  # 2 A + one 9.8 A step, not 100 A
    assert all(value > 2.0 for value in released)
    assert released[-1] == 100.0  # the normal, reached in ten steps from the floor
    assert rt[INVERTER_RT_APPLIED] is None


def test_a_floor_above_the_normal_value_is_clamped_to_it():
    """Configured, not rejected: the normal may be the register's own live
    maximum, so this is resolved at apply time rather than at config time."""
    hass = _hass_with_target(current=100.0, maximum=100.0)
    entry = _entry({
        CONF_BATTERY_NOMINAL_VOLTAGE: 51.2,
        CONF_CHARGE_LIMIT_NORMAL: 60,
        CONF_CHARGE_LIMIT_MINIMUM: 90,
    })
    _arm(hass, entry)

    asyncio.run(_send(hass, entry, 0.0, 0.0))

    # 60, the normal — never the 90 the user asked for, which would hold the
    # battery HIGHER than a release would.
    assert hass.services.calls[-1][2]["value"] == 60.0


def test_the_floor_is_in_the_registers_own_units_on_a_watt_register():
    """Watts on a watt register, amps on an amp register — the same convention
    as the normal value, and the reason the clamp is after the conversion."""
    hass = _hass_with_target(current=6000.0, maximum=6000.0)
    entry = _entry({
        CONF_CHARGE_LIMIT_UNIT: "W",
        CONF_CHARGE_LIMIT_MINIMUM: 100,
        CONF_CHARGE_CONTROL_INTERVAL: 1,
    })
    _arm(hass, entry)

    asyncio.run(_send(hass, entry, 0.0, 0.0))
    assert hass.services.calls[-1][2]["value"] == 100.0

    # And a real advice above it still passes through in watts. A 2 kW margin
    # keeps the upward slew out of the way — this test is about the units, and
    # the ramp has its own section.
    hass.states.set(TARGET, 100.0, max=6000.0)
    asyncio.run(_send(hass, entry, 2000.0, 10.0, hub_entry=_hub(margin=2000)))
    assert hass.services.calls[-1][2]["value"] == 2000.0


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

    asyncio.run(_send(hass, entry, 2560.0, 0.0))

    # Read BEFORE the write, so it is the register's value as the inverter last
    # reported it — not an optimistic echo of what we just asked for.
    assert rt[INVERTER_RT_REGISTER] == 100.0
    assert rt[INVERTER_RT_NORMAL] == 100.0

    # The inverter takes the write; a paced-out call still refreshes the read-back
    # even though it writes nothing.
    hass.states.set(TARGET, 50.0, max=100.0)
    asyncio.run(_send(hass, entry, 2560.0, 1.0))

    assert len(hass.services.calls) == 1
    assert rt[INVERTER_RT_REGISTER] == 50.0


def test_the_read_back_is_refreshed_while_the_switch_is_off():
    """The register still holds a real value when our control is off — that is
    the half of the graph the sensor would otherwise lose."""
    hass = _hass_with_target(current=80.0, maximum=100.0)
    entry = _entry()
    rt = _arm(hass, entry, enabled=False)

    asyncio.run(_send(hass, entry, 2560.0, 0.0))

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

    asyncio.run(_send(hass, entry, 2560.0, 0.0))

    assert rt[INVERTER_RT_REGISTER] is None


# --- The enforced rate, in watts, for the calculation engine -------------------
#
# The engine's Excess verdict asks what the battery is PERMITTED to take, not what
# it is rated for: while this control holds the register down, the two are
# different numbers and counting the rating makes a clipping window look like a
# site with somewhere left to put its production. The control publishes the
# permitted rate in watts (INVERTER_RT_ENFORCED_CHARGE_W); None means nothing is
# being held back, so the nameplate rate stands.


def test_watts_from_amps_is_the_inverse_conversion():
    assert from_target_units(100.0, "A", 51.2) == 5120.0
    assert from_target_units(5120.0, "W", 51.2) == 5120.0


def test_the_enforced_rate_is_the_register_in_watts():
    hass = _hass_with_target(current=100.0, maximum=100.0)
    entry = _entry({CONF_BATTERY_NOMINAL_VOLTAGE: 51.2})
    rt = _arm(hass, entry)

    # First engaged cycle: the write is issued, but the register still reports the
    # full 100 A, so the battery may still take its full rate. Nothing narrows yet.
    asyncio.run(_send(hass, entry, 2560.0, 0.0))
    assert rt[INVERTER_RT_ENFORCED_CHARGE_W] == 5120.0

    # The inverter has taken the write: 50 A at 51.2 V is the 2560 W the forecast
    # asked for, and that is now the rate the battery is permitted.
    hass.states.set(TARGET, 50.0, max=100.0)
    asyncio.run(_send(hass, entry, 2560.0, 1000.0))
    assert rt[INVERTER_RT_ENFORCED_CHARGE_W] == 2560.0


def test_a_watt_register_needs_no_conversion():
    hass = _hass_with_target(current=6500.0, maximum=10000.0)
    entry = _entry({CONF_CHARGE_LIMIT_UNIT: "W"})
    rt = _arm(hass, entry)

    asyncio.run(_send(hass, entry, 6500.0, 0.0))

    assert rt[INVERTER_RT_ENFORCED_CHARGE_W] == 6500.0


def test_an_unreadable_register_falls_back_to_the_driven_value():
    """No read-back to trust, so the value we are driving it to stands in."""
    hass = _Hass()
    hass.states.set(TARGET, "unavailable")
    entry = _entry({CONF_BATTERY_NOMINAL_VOLTAGE: 51.2, CONF_CHARGE_LIMIT_NORMAL: 100})
    rt = _arm(hass, entry)

    asyncio.run(_send(hass, entry, 2560.0, 0.0))

    assert rt[INVERTER_RT_ENFORCED_CHARGE_W] == 2560.0


def test_the_floor_is_part_of_the_enforced_rate():
    """The minimum charge limit raises what is actually written, so the battery is
    permitted more than the advice asked for — and the engine must hear that."""
    hass = _hass_with_target(current=100.0, maximum=100.0)
    entry = _entry({
        CONF_BATTERY_NOMINAL_VOLTAGE: 51.2,
        CONF_CHARGE_LIMIT_MINIMUM: 10,  # 10 A = 512 W
    })
    rt = _arm(hass, entry)

    # A 0 W advice writes the 10 A floor instead; once the register holds it, the
    # enforced rate is that floor, not zero.
    asyncio.run(_send(hass, entry, 0.0, 0.0))
    hass.states.set(TARGET, 10.0, max=100.0)
    asyncio.run(_send(hass, entry, 0.0, 1000.0))

    assert rt[INVERTER_RT_ENFORCED_CHARGE_W] == 512.0


def test_nothing_is_enforced_while_the_switch_is_off():
    hass = _hass_with_target(current=50.0, maximum=100.0)
    entry = _entry({CONF_BATTERY_NOMINAL_VOLTAGE: 51.2})
    rt = _arm(hass, entry, enabled=False)

    asyncio.run(_send(hass, entry, 2560.0, 0.0))

    # The register may well sit at 50 A — but it is the user's own setting, not a
    # limit we are holding, so the nameplate rate is what the engine should count.
    assert rt[INVERTER_RT_ENFORCED_CHARGE_W] is None


def test_the_release_clears_the_enforced_rate():
    hass = _hass_with_target(current=100.0, maximum=100.0)
    entry = _entry({CONF_BATTERY_NOMINAL_VOLTAGE: 51.2})
    rt = _arm(hass, entry)

    asyncio.run(_send(hass, entry, 2560.0, 0.0))
    hass.states.set(TARGET, 50.0, max=100.0)
    asyncio.run(_send(hass, entry, 2560.0, 1000.0))
    assert rt[INVERTER_RT_ENFORCED_CHARGE_W] == 2560.0

    # Evening: the advice releases. The restore write happens once, but the
    # cleared enforcement must hold for every cycle after it.
    asyncio.run(_send(hass, entry, None, 2000.0))
    assert rt[INVERTER_RT_ENFORCED_CHARGE_W] is None
    asyncio.run(_send(hass, entry, None, 3000.0))
    assert rt[INVERTER_RT_ENFORCED_CHARGE_W] is None


# --- The asymmetric slew limit (the ramp) -------------------------------------
#
# Why it exists, from the maintainer's live site on 2026-08-24: every time the
# forecast ceiling self-healed upward (98 → 99 → 100 % as the clip burned down)
# the latch disarmed, the release wrote the full normal value in ONE step, and
# the battery drank ~10 kW out of exportable power for ten minutes — the
# clipping reserve spent on exactly the energy it was being kept for, ahead of
# the peak it was kept for.
#
# The release SEMANTICS were right ("less reserve needed, you may refill"); the
# step response was the defect. So the written value may climb by at most one
# Excess trigger margin's worth of watts per write, converted into the target
# register's own units, while a DOWNWARD move — engaging the limit, the
# protection direction — still lands in a single write.
#
# The bound is the trigger margin and not a knob of its own by construction: the
# engaged advice is anchored one margin below the export limit, so a masked site
# self-creeps upward by about one margin per write (commit 91aa5ed). A ramp
# bounded at exactly that lets the creep through untouched — proved below rather
# than argued.

SITE_NORMAL = 187.0  # A — the maintainer's register at full rate, ~9.6 kW
SITE_VOLTAGE = 51.2
SITE_INTERVAL = 60.0  # s between writes, as that site is configured
MARGIN_AMPS = DEFAULT_EXCESS_TRIGGER_MARGIN / SITE_VOLTAGE  # 9.77 A per write


def _site_entry(options=None):
    """The maintainer's inverter: 187 A normal, 2 A floor, one write a minute."""
    return _entry({
        CONF_BATTERY_NOMINAL_VOLTAGE: SITE_VOLTAGE,
        CONF_CHARGE_LIMIT_NORMAL: SITE_NORMAL,
        CONF_CHARGE_LIMIT_MINIMUM: 2,
        CONF_CHARGE_CONTROL_INTERVAL: SITE_INTERVAL,
        **(options or {}),
    })


def _accepting_site():
    """A hass whose register starts at full rate, plus that entry."""
    hass = _hass_with_target(current=SITE_NORMAL, maximum=SITE_NORMAL)
    entry = _site_entry()
    return hass, entry, _arm(hass, entry)


def _cycle(hass, entry, advice_w, now, hub_entry=None):
    """One cycle with the inverter APPLYING whatever we last wrote.

    The register following our writes is what the ramp is measured against in
    real life — the read-back feeds the deadband — so these tests apply it
    rather than leaving the register frozen.
    """
    if hass.services.calls:
        hass.states.set(
            TARGET, hass.services.calls[-1][2]["value"], max=SITE_NORMAL
        )
    asyncio.run(_send(hass, entry, advice_w, now, hub_entry=hub_entry))


def _written(hass):
    return [call[2]["value"] for call in hass.services.calls]


# --- The step, as arithmetic ---------------------------------------------------


def test_the_slew_step_is_the_trigger_margin_in_the_registers_units():
    """One margin of WATTS per write, converted like everything else here."""
    assert slew_margin_w(_hub()) == DEFAULT_EXCESS_TRIGGER_MARGIN == 500
    assert slew_margin_w(_hub(margin=800)) == 800.0
    # An amp register divides by the battery voltage; a watt register does not.
    assert slew_step(500.0, "A", 51.2, deadband=0) == 500.0 / 51.2
    assert slew_step(500.0, "W", 51.2, deadband=0) == 500.0


def test_an_unresolvable_hub_falls_back_to_the_default_margin():
    """A hub mid-reload must not read as a site with no rate limit at all."""
    assert slew_margin_w(None) == DEFAULT_EXCESS_TRIGGER_MARGIN


def test_no_trigger_margin_at_all_means_no_rate_limit():
    """A site with no margin has no natural step to borrow, and this module does
    not get to invent one — None is 'the value stands'."""
    assert slew_step(0.0, "A", 51.2, deadband=5) is None
    assert slew_step(-100.0, "W", 51.2, deadband=5) is None
    assert slew_limited(100.0, 10.0, None) == 100.0


def test_the_step_is_never_smaller_than_the_deadband():
    """A step the deadband would swallow is not a slower ramp — it is no ramp,
    every write suppressed and the register left held down for the day."""
    assert slew_step(500.0, "W", 51.2, deadband=800) == 800.0
    assert slew_step(500.0, "W", 51.2, deadband=100) == 500.0


def test_only_upward_moves_are_limited():
    """The asymmetry itself, in one place."""
    assert slew_limited(desired=100.0, baseline=50.0, step=10.0) == 60.0
    assert slew_limited(desired=55.0, baseline=50.0, step=10.0) == 55.0  # short hop
    # Downward is the protection direction: any distance, one write.
    assert slew_limited(desired=2.0, baseline=187.0, step=10.0) == 2.0
    # Nothing to measure a rise against.
    assert slew_limited(desired=100.0, baseline=None, step=10.0) == 100.0


def test_the_ramp_is_measured_from_the_last_value_we_wrote():
    """Our own write, not the read-back: a ramp that waited for the register to
    catch up would stall on a slow poll and leave the battery limited."""
    assert ramp_baseline(applied=50.0, current=187.0) == 50.0
    # Except when there is no memory to use — the first write after a reload.
    assert ramp_baseline(applied=None, current=187.0) == 187.0
    assert ramp_baseline(applied=None, current=None) is None


# --- The release ramp ---------------------------------------------------------


def test_a_release_ramps_to_full_rate_one_margin_at_a_time():
    """The bug this exists for, as the fix: 2 A → 187 A takes ~19 minutes of
    one-a-minute writes instead of one step."""
    hass, entry, rt = _accepting_site()

    # Engaged: a 0 W advice under the 2 A floor, one instant write down.
    _cycle(hass, entry, 0.0, 0.0)
    assert _written(hass) == [2.0]

    now = SITE_INTERVAL
    while rt[INVERTER_RT_APPLIED] is not None and now < 3600:
        _cycle(hass, entry, None, now)
        now += SITE_INTERVAL

    ramp = _written(hass)[1:]
    # Eighteen writes, one a minute, and the nineteenth minute lands: the last
    # 8.6 A of the climb is inside the 9.35 A deadband (5 % of 187), so the ramp
    # ends by clearing the marker rather than by spending a write on it.
    assert len(ramp) == 18
    assert now == 19 * SITE_INTERVAL + SITE_INTERVAL
    assert ramp[0] == 11.8  # 2 A + one 9.77 A margin step
    assert ramp[-1] == 178.4
    # Every step is one margin, never more, and always upward.
    steps = [b - a for a, b in zip([2.0] + ramp, ramp)]
    assert all(0 < step <= MARGIN_AMPS + 0.05 for step in steps), steps
    # Restored exactly once still holds — at the END of the ramp.
    assert rt[INVERTER_RT_APPLIED] is None
    assert rt[INVERTER_RT_STATUS] == CONTROL_STATE_IDLE


def test_the_release_ramp_respects_the_write_interval():
    """The pacing is not spent faster by the ramp: cycles inside the window
    write nothing, however many of them there are."""
    hass, entry, _rt = _accepting_site()

    _cycle(hass, entry, 0.0, 0.0)
    # 30 site cycles at 2 s — a whole minute of them, inside the 60 s window.
    now = 2.0
    while now < SITE_INTERVAL:
        _cycle(hass, entry, None, now)
        now += 2.0
    assert _written(hass) == [2.0]

    _cycle(hass, entry, None, SITE_INTERVAL)
    assert _written(hass) == [2.0, 11.8]


def test_a_release_interrupted_by_re_engagement_writes_down_instantly():
    """The maintainer's exact day: release at 13:43, SOC hits the new gate at
    13:53 and the limit re-engages. The ramp had climbed ten minutes' worth, and
    the downward write is one step from wherever it had reached."""
    hass, entry, rt = _accepting_site()

    _cycle(hass, entry, 0.0, 0.0)  # engaged at the 2 A floor
    for minute in range(1, 11):  # ten minutes of release
        _cycle(hass, entry, None, minute * SITE_INTERVAL)

    ramp = _written(hass)[1:]
    assert len(ramp) == 10
    assert ramp[-1] == 100.0  # ten margin steps up from 2 A, not 187 A
    assert rt[INVERTER_RT_APPLIED] == 100.0  # still ramping, marker held

    # Re-engaged: straight back down to the floor, in ONE write.
    _cycle(hass, entry, 0.0, 11 * SITE_INTERVAL)
    assert _written(hass)[-1] == 2.0
    assert rt[INVERTER_RT_STATUS] == CONTROL_STATE_LIMITING

    # What the reserve actually paid for those ten minutes: the mean of the ramp
    # rather than the full rate — under a third of the old one-step burst.
    burst = sum(ramp) / len(ramp)
    assert burst < SITE_NORMAL / 3


def test_engaging_deeper_is_never_rate_limited():
    """Downward from anywhere, at any depth, in one write — the slew must not be
    able to delay protection."""
    hass, entry, _rt = _accepting_site()

    _cycle(hass, entry, 9574.0, 0.0)  # 187 A: full rate, engaged
    _cycle(hass, entry, 0.0, SITE_INTERVAL)  # straight to the floor

    assert _written(hass)[-1] == 2.0


def test_the_final_approach_inside_the_deadband_ends_the_release():
    """Otherwise the marker would never clear and the release would never be
    finished: the last sliver of the climb is not worth a Modbus write, but it
    still has to end the ramp."""
    hass = _hass_with_target(current=96.0, maximum=100.0)
    entry = _entry({
        CONF_BATTERY_NOMINAL_VOLTAGE: SITE_VOLTAGE,
        CONF_CHARGE_CONTROL_INTERVAL: 1,
    })
    rt = _arm(hass, entry)
    # Pretend we wrote the 96 A the register holds — 4 A short of the normal,
    # inside the 5 A deadband (5 % of 100).
    rt[INVERTER_RT_APPLIED] = 96.0

    asyncio.run(_send(hass, entry, None, 100.0))

    assert hass.services.calls == []  # nothing worth writing
    assert rt[INVERTER_RT_APPLIED] is None  # and the release is over


# --- The masked-site self-creep passes through untouched ----------------------


def test_the_engaged_self_creep_passes_the_slew_without_delay():
    """The construction argument, as a replay.

    The masked-site trajectory from commit 91aa5ed's own tests — the advice
    climbing by exactly one Excess trigger margin per write while export is
    pinned at the limit — driven through the control layer. Every advice is
    written in full on the cycle it arrives: the ramp bound IS that step, so the
    escape from masking is not slowed by a single write.
    """
    hass = _hass_with_target(current=187.0, maximum=SITE_NORMAL)
    entry = _site_entry()
    _arm(hass, entry)

    # 1000 W → 6500 W in 500 W steps: test_masked_site_replay_self_creeps_off_
    # the_hard_limit's trajectory, in watts.
    trajectory = [1000.0 + 500.0 * n for n in range(12)]
    for minute, advice_w in enumerate(trajectory):
        _cycle(hass, entry, advice_w, minute * SITE_INTERVAL)

    written = _written(hass)
    assert len(written) == len(trajectory)
    # Each write is the advice itself, converted — never a shaved-down step.
    assert written == [round(w / SITE_VOLTAGE, 1) for w in trajectory]


def test_a_creep_faster_than_one_margin_is_the_one_that_gets_held():
    """The bound bites exactly where it should: double the margin per cycle and
    half of it is deferred to the next write."""
    hass = _hass_with_target(current=187.0, maximum=SITE_NORMAL)
    entry = _site_entry()
    _arm(hass, entry)

    _cycle(hass, entry, 1000.0, 0.0)
    _cycle(hass, entry, 2000.0, SITE_INTERVAL)  # +1000 W, two margins

    written = _written(hass)
    assert written[0] == round(1000.0 / SITE_VOLTAGE, 1)  # 19.5 A
    assert written[1] == round(written[0] + MARGIN_AMPS, 1)  # 29.3, not 39.1


# --- The standing destination hold, at the register --------------------------
#
# Since the destination became a standing ceiling, an advice of "the floor" is
# reached on days that reserve nothing at all: the pack parks at its destination
# and waits. This is the regime that hold operates in, and none of the mechanics
# below are new — the point is that the parked case is served by exactly the same
# LIMITING branch, ramp and enforcement publication as a reserved one.


def test_a_parked_battery_ramps_up_when_a_better_day_appears():
    """Parked on the floor, then production beats the forecast's anchor.

    Upward is still a permission to refill, so the overshoot arrives over
    several writes rather than in one burst — and the moment it goes (a cloud)
    the register is back on the floor in a single write.
    """
    hass, entry, rt = _accepting_site()

    # The hold: nothing to clip, no overshoot, the pack sitting at its
    # destination. One instant write down to the 2 A floor.
    _cycle(hass, entry, 0.0, 0.0)
    assert _written(hass) == [2.0]
    assert rt[INVERTER_RT_STATUS] == CONTROL_STATE_LIMITING
    # The enforcement the Excess verdict reads is the register's own value, so it
    # is one cycle behind the write by design — the battery really may still take
    # what the register still holds. It lands on the floor once the register has
    # followed us there.
    assert rt[INVERTER_RT_ENFORCED_CHARGE_W] == SITE_NORMAL * SITE_VOLTAGE
    _cycle(hass, entry, 0.0, SITE_INTERVAL)
    assert rt[INVERTER_RT_ENFORCED_CHARGE_W] == 2.0 * SITE_VOLTAGE
    assert _written(hass) == [2.0]  # nothing more to write while parked

    # The day turns out better than forecast: 4 kW the site cannot export.
    for minute in range(2, 6):
        _cycle(hass, entry, 4000.0, minute * SITE_INTERVAL)

    written = _written(hass)
    assert written[1] == round(2.0 + MARGIN_AMPS, 1)
    for previous, nxt in zip(written[1:], written[2:]):
        assert round(nxt - previous, 1) == round(MARGIN_AMPS, 1)
    # Still climbing — 4 kW is 78 A, and one margin is 9.8 A a minute.
    assert written[-1] < 4000.0 / SITE_VOLTAGE

    # The cloud: the overshoot is gone and the floor lands in one write.
    _cycle(hass, entry, 0.0, 6 * SITE_INTERVAL)
    assert _written(hass)[-1] == 2.0


# --- Directional pacing: the downward persistence window ----------------------
#
# The advice is memoryless direct feedback now (see calculations/forecast.py), so
# it moves with the plant on every site cycle and this layer is where the
# volatility is absorbed. Asymmetrically:
#
#   * upward — eligible every cycle, already bounded to one margin per write;
#   * downward — written only once EVERY sample in a full window agrees, and
#     then only by the amount they all agree on (the window's maximum);
#   * the gate ENGAGING — written at once, because that is a protective regime
#     transition and not a steady-state correction.
#
# The window is CONF_CHARGE_CONTROL_INTERVAL, whose meaning is exactly that:
# how long a reduction must hold. At the 300 s default a kettle, a passing cloud
# and a car plugging in all cost nothing at the register.

DEEP_W = 5000.0        # 97.7 A — a reduction well past the 9.35 A deadband
SHALLOW_W = 2000.0     # 39.1 A — a deeper dip, to be swallowed by the maximum
FULL_W = SITE_NORMAL * SITE_VOLTAGE  # the advice that asks for full rate


def _gated(hass, entry, advice_w, now, limiting=True):
    """One cycle with the gate state the forecast would have published."""
    if hass.services.calls:
        hass.states.set(
            TARGET, hass.services.calls[-1][2]["value"], max=SITE_NORMAL
        )
    asyncio.run(
        send_inverter_charge_limit(hass, entry, _hub(), advice_w, now, limiting)
    )


def _engaged_site():
    """An armed site whose gate is already engaged and settled at full rate.

    The first cycle is the engagement, and it is deliberately handed an advice
    that asks for full rate so the exemption has nothing to write — what it
    leaves behind is the gate marker, which is the state the window rules need.
    """
    hass, entry, rt = _accepting_site()
    _gated(hass, entry, FULL_W, 0.0)
    assert _written(hass) == []
    return hass, entry, rt


# --- The window, as arithmetic ------------------------------------------------


def test_the_window_is_full_only_after_a_whole_interval_of_samples():
    samples = []
    for n in range(6):  # 10 s apart, a 60 s window
        samples = note_reduction(samples, n * 10.0, 100.0, 60.0)
        assert down_window_value(samples, n * 10.0, 60.0) is None
    samples = note_reduction(samples, 60.0, 100.0, 60.0)
    assert down_window_value(samples, 60.0, 60.0) == 100.0


def test_the_window_hands_back_the_maximum_its_samples_agreed_on():
    """The least reduction all of them agree on — never a momentary deep dip."""
    samples = []
    for stamp, value in ((0.0, 100.0), (20.0, 40.0), (40.0, 90.0), (60.0, 50.0)):
        samples = note_reduction(samples, stamp, value, 60.0)
    assert down_window_value(samples, 60.0, 60.0) == 100.0


def test_the_sample_list_is_bounded_by_the_window():
    """A 2 s site cycle against a 300 s window keeps ~150 samples, not an hour's.

    Pruning keeps the shortest run that still spans the window, so the oldest
    retained stamp is still what the window is measured against.
    """
    samples = []
    for n in range(3000):  # 100 minutes of a 2 s cycle
        samples = note_reduction(samples, n * 2.0, 100.0, 300.0)
    assert len(samples) <= 300 / 2 + 2
    assert down_window_value(samples, 3000 * 2.0, 300.0) == 100.0
    # And it never prunes away the coverage it needs.
    assert (3000 * 2.0 - samples[0][0]) >= 300.0


# --- Downward: a reduction has to persist ------------------------------------


def test_a_reduction_is_not_written_until_it_has_held_for_the_window():
    hass, entry, _rt = _engaged_site()

    now = 10.0
    while now < SITE_INTERVAL + 10.0:
        _gated(hass, entry, DEEP_W, now)
        assert _written(hass) == [], f"wrote at {now}s"
        now += 10.0

    # The window filled: one write, and it is the reduction itself.
    _gated(hass, entry, DEEP_W, now)
    assert _written(hass) == [round(DEEP_W / SITE_VOLTAGE, 1)]


def test_the_written_reduction_is_the_windows_maximum_not_its_dip():
    """A deep dip inside the window cannot drag the register down with it."""
    hass, entry, _rt = _engaged_site()

    _gated(hass, entry, DEEP_W, 10.0)
    _gated(hass, entry, SHALLOW_W, 20.0)   # the dip
    now = 30.0
    while now <= SITE_INTERVAL + 10.0:
        _gated(hass, entry, DEEP_W, now)
        now += 10.0

    # 97.7 A (the 5 kW samples), never 39.1 A (the 2 kW dip).
    assert _written(hass) == [round(DEEP_W / SITE_VOLTAGE, 1)]


def test_a_sample_back_at_the_register_clears_the_window():
    """The kettle case at the register: a 40 s dip inside a 60 s window is not a
    reduction that persisted, and it costs no write at all."""
    hass, entry, rt = _engaged_site()

    for n in range(1, 5):  # 40 s of the dip
        _gated(hass, entry, DEEP_W, n * 10.0)
    assert _written(hass) == []

    # The plant recovers: the advice is back at full rate, so the window clears.
    _gated(hass, entry, FULL_W, 50.0)
    assert rt[INVERTER_RT_DOWN_SAMPLES] == []

    # A whole interval later there is still nothing written — the old samples
    # cannot combine with new ones to reach a window's worth.
    for n in range(6, 12):
        _gated(hass, entry, DEEP_W, n * 10.0)
    assert _written(hass) == []


def test_the_window_starts_again_after_a_write():
    hass, entry, rt = _engaged_site()

    now = 10.0
    while not _written(hass):
        _gated(hass, entry, DEEP_W, now)
        now += 10.0
    assert rt[INVERTER_RT_DOWN_SAMPLES] == []

    # A second, deeper reduction has to earn its own window.
    deeper = 2000.0
    for _ in range(5):
        _gated(hass, entry, deeper, now)
        now += 10.0
    assert len(_written(hass)) == 1
    _gated(hass, entry, deeper, now + SITE_INTERVAL)
    assert _written(hass)[-1] == round(deeper / SITE_VOLTAGE, 1)


# --- The engagement exemption -------------------------------------------------


def test_the_gate_engaging_writes_the_protective_reduction_at_once():
    """The destination hold or the reservation taking hold is a regime
    transition, and it must not wait out a persistence window with the pack
    already above where it was sent."""
    hass, entry, rt = _accepting_site()

    # Released: full rate, gate False, nothing to write.
    _gated(hass, entry, FULL_W, 0.0, limiting=False)
    assert _written(hass) == []

    # Engaged on the next cycle, and the write lands on that cycle.
    _gated(hass, entry, 0.0, 10.0, limiting=True)
    assert _written(hass) == [2.0]  # the configured floor
    assert rt[INVERTER_RT_STATUS] == CONTROL_STATE_LIMITING


def test_only_the_engaging_cycle_is_exempt():
    """The bite: the very same reduction, one cycle later, waits for its window.

    Without this the exemption would be a hole in the pacing rather than a
    regime-transition rule.
    """
    hass, entry, _rt = _accepting_site()

    _gated(hass, entry, FULL_W, 0.0, limiting=False)
    _gated(hass, entry, FULL_W, 10.0, limiting=True)   # engages, nothing to write
    _gated(hass, entry, 0.0, 20.0, limiting=True)      # a reduction, not exempt
    assert _written(hass) == []
    _gated(hass, entry, 0.0, 20.0 + SITE_INTERVAL, limiting=True)
    assert _written(hass) == [2.0]


def test_a_reload_treats_the_first_engaged_cycle_as_an_engagement():
    """Fresh runtime: the gate marker is gone, so the first engaged cycle is an
    edge and the protective write lands rather than waiting out a window on a
    register whose standing we no longer know."""
    hass, entry, _rt = _accepting_site()

    _gated(hass, entry, 0.0, 1000.0, limiting=True)
    assert _written(hass) == [2.0]


# --- Upward: eligible every cycle --------------------------------------------


def test_a_rise_is_written_on_the_cycle_it_arrives():
    """Three consecutive rises inside ONE interval, each one written.

    The rise is already bounded to a margin per write, and this is the direction
    the masked-site self-creep escapes in — one margin per CYCLE, not per
    interval.
    """
    hass, entry, _rt = _accepting_site()

    _gated(hass, entry, 0.0, 0.0)          # engaging: down to the floor
    assert _written(hass) == [2.0]
    for n in range(1, 4):
        _gated(hass, entry, 4000.0, n * 10.0)

    written = _written(hass)
    assert len(written) == 4
    steps = [round(b - a, 1) for a, b in zip(written, written[1:])]
    assert steps == [round(MARGIN_AMPS, 1)] * 3


def test_a_steady_plant_writes_once_and_never_reverses():
    """No deadband limit cycle: the reduction lands once and the register sits.

    A controller that moved with every sample would chatter around the deadband;
    with the window on the way down and the deadband on the way up, a plant that
    holds still produces exactly one write and no reversal at all.
    """
    hass, entry, _rt = _accepting_site()

    _gated(hass, entry, DEEP_W, 0.0)   # the engagement writes the reduction
    for n in range(1, 200):            # half an hour of the same plant
        _gated(hass, entry, DEEP_W, n * 10.0)

    written = _written(hass)
    assert written == [round(DEEP_W / SITE_VOLTAGE, 1)]
    moves = [b - a for a, b in zip(written, written[1:])]
    assert not [1 for a, b in zip(moves, moves[1:]) if a * b < 0]


def test_no_gate_state_keeps_the_pre_window_contract():
    """A caller with no gate to offer gets the old rules: reductions instant,
    one write per interval. Degraded, and on the side of writing."""
    hass, entry, rt = _accepting_site()

    _gated(hass, entry, DEEP_W, 0.0, limiting=None)
    assert _written(hass) == [round(DEEP_W / SITE_VOLTAGE, 1)]
    assert rt[INVERTER_RT_DOWN_SAMPLES] == []
    # And still paced by the interval, in both directions.
    _gated(hass, entry, 2000.0, 10.0, limiting=None)
    assert len(_written(hass)) == 1
    _gated(hass, entry, 2000.0, SITE_INTERVAL, limiting=None)
    assert len(_written(hass)) == 2


def test_the_enforced_rate_is_published_on_every_deferred_cycle():
    """The Excess verdict must not go blind while a reduction is pending.

    The publication is the register's own read-back and happens before any of
    the pacing rules, so a cycle that writes nothing still reports what the
    battery is really permitted.
    """
    hass, entry, rt = _engaged_site()

    for n in range(1, 5):
        _gated(hass, entry, DEEP_W, n * 10.0)
        assert rt[INVERTER_RT_ENFORCED_CHARGE_W] == SITE_NORMAL * SITE_VOLTAGE
        assert rt[INVERTER_RT_RECOMMENDED] == round(DEEP_W / SITE_VOLTAGE, 1)


# --- Register units and the reload baseline ----------------------------------


def test_a_watt_register_ramps_in_watts_with_no_conversion():
    """Step = the margin itself, straight from the setting."""
    hass = _hass_with_target(current=200.0, maximum=10000.0)
    entry = _entry({
        CONF_CHARGE_LIMIT_UNIT: "W",
        CONF_CHARGE_LIMIT_NORMAL: 10000,
        CONF_CHARGE_CONTROL_INTERVAL: 60,
    })
    rt = _arm(hass, entry)
    rt[INVERTER_RT_APPLIED] = 200.0

    # The deadband is 5 % of 10 kW = 500 W, the same as the margin here, so the
    # step is exactly one margin.
    asyncio.run(_send(hass, entry, 9000.0, 100.0))
    assert hass.services.calls[-1][2]["value"] == 700.0

    hass.states.set(TARGET, 700.0, max=10000.0)
    asyncio.run(_send(hass, entry, 9000.0, 160.0))
    assert hass.services.calls[-1][2]["value"] == 1200.0


def test_the_first_write_after_a_reload_ramps_from_the_register():
    """No applied marker survives a reload, so the register's own read-back is
    the baseline — otherwise the very cycle after a restart would be the
    full-rate step this whole thing exists to prevent."""
    hass = _hass_with_target(current=20.0, maximum=SITE_NORMAL)
    entry = _site_entry()
    rt = _arm(hass, entry)
    assert rt.get(INVERTER_RT_APPLIED) is None

    asyncio.run(_send(hass, entry, 9574.0, 0.0))  # advice at full rate

    assert hass.services.calls[-1][2]["value"] == round(20.0 + MARGIN_AMPS, 1)


def test_an_unreadable_register_and_no_marker_writes_the_advice():
    """Nothing to ramp from at all — a guessed baseline would be worse than
    none, so the value stands (and the deadband has nothing to compare either)."""
    hass = _Hass()
    hass.states.set(TARGET, "unavailable")
    entry = _site_entry()
    _arm(hass, entry)

    asyncio.run(_send(hass, entry, 9574.0, 0.0))

    assert hass.services.calls[-1][2]["value"] == SITE_NORMAL


# --- Where the step comes from ------------------------------------------------


def test_the_margin_is_read_from_the_hub_not_the_inverter_entry():
    """The slew step is a SITE-level number, so it is threaded in from the hub
    beside the inverter entry — the same way the site voltage reaches
    control/ocpp.py. A margin stored on the inverter entry is not a setting at
    all and must not be picked up as one.
    """
    hass = _hass_with_target(current=20.0, maximum=SITE_NORMAL)
    entry = _site_entry({CONF_EXCESS_TRIGGER_MARGIN: 5000})  # not a real setting
    _arm(hass, entry)

    asyncio.run(_send(hass, entry, 9574.0, 0.0, hub_entry=_hub(margin=1024)))

    # 1024 W at 51.2 V is a 20 A step from the register's 20 A — the hub's
    # number, not the inverter's 5000 W and not the 500 W default.
    assert hass.services.calls[-1][2]["value"] == 40.0


def test_a_hub_with_the_margin_switched_off_writes_in_one_step():
    """No margin, no ramp: the site has no natural step, and this module does not
    invent one behind the user's back."""
    hass = _hass_with_target(current=20.0, maximum=SITE_NORMAL)
    entry = _site_entry()
    _arm(hass, entry)

    asyncio.run(_send(hass, entry, 9574.0, 0.0, hub_entry=_hub(margin=0)))

    assert hass.services.calls[-1][2]["value"] == SITE_NORMAL


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
        asyncio.run(_send(hass, entry, advice_w, now))
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
