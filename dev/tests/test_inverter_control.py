"""Tests for the inverter battery charge write-control — control.inverter.

Machine-authored tests — not yet human-reviewed.

The forecast's recommended charge limit only reaches the inverter when the
user armed the control switch, the value moved by more than the deadband, and
the write interval has elapsed. Releasing puts the normal value back exactly
once. These use a hand-rolled fake hass rather than the HA fixtures so they
also run in the pure-python runner.
"""

import asyncio
from types import SimpleNamespace

from custom_components.dynamic_ocpp_evse.const import (
    DOMAIN,
    CONF_CHARGE_LIMIT_ENTITY_ID,
    CONF_CHARGE_LIMIT_UNIT,
    CONF_CHARGE_LIMIT_NORMAL,
    CONF_CHARGE_CONTROL_INTERVAL,
    CONF_CHARGE_CONTROL_DEADBAND,
    CONF_BATTERY_NOMINAL_VOLTAGE,
    CONF_BATTERY_VOLTAGE_ENTITY_ID,
    INVERTER_RT_APPLIED,
    INVERTER_RT_CONTROL_ENABLED,
    INVERTER_RT_STATUS,
)
from custom_components.dynamic_ocpp_evse.control.inverter import (
    battery_voltage,
    resolve_normal_value,
    send_inverter_charge_limit,
    should_write,
    to_target_units,
)

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
    assert rt[INVERTER_RT_STATUS] == "Off"


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
    assert rt[INVERTER_RT_STATUS] == "Limiting to 50.0A"


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
    assert rt[INVERTER_RT_STATUS] == "Not limiting"


def test_release_without_a_prior_write_leaves_the_register_alone():
    """A register we never touched is the user's — never 'restore' it."""
    hass = _hass_with_target(current=42.0, maximum=100.0)
    entry = _entry()
    rt = _arm(hass, entry)

    asyncio.run(send_inverter_charge_limit(hass, entry, None, 0.0))

    assert hass.services.calls == []
    assert rt[INVERTER_RT_STATUS] == "Not limiting"


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
