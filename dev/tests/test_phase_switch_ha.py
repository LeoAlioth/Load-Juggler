"""Phase switching through the real site cycle, the profile and the settling.

Machine-authored tests - not yet human-reviewed.

Design: docs/plans/2026-11-01/phase-switching.md. The site is a 20 A breaker
whose phase B carries a 16 A house: a three-phase charger's 6 A minimum does
not fit on B, so three phases would pause it, while phase A has room for 16 A.
"""

import time
from types import SimpleNamespace
from unittest.mock import patch

from homeassistant.core import Context, State, SupportsResponse
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.dynamic_ocpp_evse.calculations.variable_load import (
    MULTI,
    SINGLE,
    phase_value,
)
from custom_components.dynamic_ocpp_evse.const import (
    CONF_CHARGER_ID,
    CONF_CHARGER_L1_PHASE,
    CONF_CHARGER_L2_PHASE,
    CONF_CHARGER_L3_PHASE,
    CONF_ENTITY_ID,
    CONF_EVSE_CURRENT_IMPORT_ENTITY_ID,
    CONF_EVSE_MAXIMUM_CHARGE_CURRENT,
    CONF_EVSE_MINIMUM_CHARGE_CURRENT,
    CONF_HUB_ENTRY_ID,
    CONF_LOAD_PRIORITY,
    CONF_MAIN_BREAKER_RATING,
    CONF_NAME,
    CONF_OCPP_DEVICE_ID,
    CONF_PHASE_A_CURRENT_ENTITY_ID,
    CONF_PHASE_B_CURRENT_ENTITY_ID,
    CONF_PHASE_C_CURRENT_ENTITY_ID,
    CONF_PHASE_SWITCHING,
    CONF_PHASE_VOLTAGE,
    CONF_VL_PHASE_ENTITY_ID,
    DOMAIN,
    ENTRY_TYPE,
    ENTRY_TYPE_HUB,
    ENTRY_TYPE_LOAD,
    EVSE_RT_COMMANDED_PHASES,
    EVSE_RT_PHASE_PAUSE_AT,
    EVSE_RT_PHASE_SUPPORTED,
    EVSE_RT_PHASES,
    PHASE_SWITCH_SETTLE_S,
    RT_PHASE_SWITCH_AT,
)
from custom_components.dynamic_ocpp_evse.control import manual_change
from custom_components.dynamic_ocpp_evse.control.ocpp import (
    phase_switch_pause,
    probe_phase_switching,
    send_ocpp_command,
)
from custom_components.dynamic_ocpp_evse.control.variable_load import _write_phase

from .closed_loop import AMPS, close_loop

SLUG = "psw"
STATUS = f"sensor.{SLUG}_status_connector"
HOUSE = {"a": 2.0, "b": 16.0, "c": 1.0}


def _hub(name):
    return MockConfigEntry(
        domain=DOMAIN, version=2, minor_version=4, title=name,
        data={CONF_NAME: name, CONF_ENTITY_ID: name, ENTRY_TYPE: ENTRY_TYPE_HUB},
        options={
            CONF_PHASE_A_CURRENT_ENTITY_ID: f"sensor.{name}_a",
            CONF_PHASE_B_CURRENT_ENTITY_ID: f"sensor.{name}_b",
            CONF_PHASE_C_CURRENT_ENTITY_ID: f"sensor.{name}_c",
            CONF_MAIN_BREAKER_RATING: 20,
            CONF_PHASE_VOLTAGE: 230,
        },
    )


def _charger(hub, **options):
    return MockConfigEntry(
        domain=DOMAIN, version=2, minor_version=4, title=SLUG,
        data={CONF_NAME: SLUG, CONF_ENTITY_ID: SLUG, ENTRY_TYPE: ENTRY_TYPE_LOAD,
              CONF_CHARGER_ID: SLUG, CONF_OCPP_DEVICE_ID: SLUG,
              CONF_EVSE_CURRENT_IMPORT_ENTITY_ID: f"sensor.{SLUG}_current",
              CONF_HUB_ENTRY_ID: hub.entry_id},
        options={CONF_LOAD_PRIORITY: 1, CONF_EVSE_MINIMUM_CHARGE_CURRENT: 6,
                 CONF_EVSE_MAXIMUM_CHARGE_CURRENT: 16,
                 CONF_CHARGER_L1_PHASE: "A", CONF_CHARGER_L2_PHASE: "B",
                 CONF_CHARGER_L3_PHASE: "C", **options},
    )


async def _run(hass, name, cycles, *, supported=True, house_b=None, status=None, mode=None, **options):
    """The charger drawing what it was told on the phases the engine put it
    on; ``house_b(i)`` / ``status(i)`` vary phase B's house and the connector."""
    hub = _hub(name)
    entry = _charger(hub, **options)
    hass.states.async_set(STATUS, "Charging")

    def plant(i, command):
        rt = hass.data[DOMAIN]["loads"][entry.entry_id]
        hass.states.async_set(STATUS, status(i) if status else "Charging")
        draw = command if hass.states.get(STATUS).state == "Charging" else 0.0
        one = rt.get(EVSE_RT_PHASES, 3) == 1
        house = dict(HOUSE, b=house_b(i) if house_b else HOUSE["b"])
        for phase in "abc":
            on = phase == "a" or not one
            hass.states.async_set(f"sensor.{name}_{phase}", f"{house[phase] + (draw if on else 0):.2f}", AMPS)
        hass.states.async_set(f"sensor.{SLUG}_current", f"{draw:.2f}", AMPS)
        return {"phases": rt.get(EVSE_RT_PHASES, 3), "draw": draw}

    return await close_loop(
        hass, hub, entry, cycles, plant, at="2026-11-01 10:00:00+00:00",
        load_data={EVSE_RT_PHASE_SUPPORTED: supported, **({"operating_mode": mode} if mode else {})},
    ), entry


async def test_one_phase_instead_of_a_pause_when_phase_b_is_full(hass):
    trace, _ = await _run(hass, "pswa", 40)
    assert trace[-1]["phases"] == 1
    assert trace[-1]["permit"] >= 6
    # the switch is taken on the first cycle three phases would pause it
    assert min(r["i"] for r in trace if r["phases"] == 1) < 5


async def test_back_to_three_only_after_ten_minutes(hass):
    # phase B frees at cycle 30 (60 s): three would fit, but the switch to one
    # was made at the start, so three waits for ten minutes after it
    trace, _ = await _run(hass, "pswb", 340, house_b=lambda i: 16.0 if i < 30 else 2.0)
    first_one = min(r["i"] for r in trace if r["phases"] == 1)
    back = min(r["i"] for r in trace if r["i"] > first_one and r["phases"] == 3)
    assert (back - first_one) * 2 >= 600
    assert trace[-1]["permit"] >= 6


async def test_a_charger_that_cannot_switch_stays_on_three(hass):
    trace, _ = await _run(hass, "pswc", 20, supported=False)
    assert {r["phases"] for r in trace} == {3}
    assert trace[-1]["permit"] < 6


async def test_the_option_off_keeps_three(hass):
    trace, _ = await _run(hass, "pswd", 20, **{CONF_PHASE_SWITCHING: False})
    assert {r["phases"] for r in trace} == {3}


async def test_no_car_puts_it_back_on_three(hass):
    trace, _ = await _run(hass, "pswe", 30, status=lambda i: "Charging" if i < 15 else "Available")
    assert trace[10]["phases"] == 1
    assert trace[-1]["phases"] == 3


async def test_hold_gives_the_charger_nothing_and_does_not_switch_it_to_one_phase(hass):
    """Hold on an OCPP charger: its limit is 0 and it claims nothing, with phase
    B free enough for three phases the whole time - and the 0 A is not read as
    a reason to go to one phase (GitHub issue 21)."""
    trace, _ = await _run(hass, "pswh", 60, house_b=lambda i: 2.0, mode="Hold")
    assert all(r["permit"] == 0 for r in trace), [r["permit"] for r in trace][:10]
    assert {r["phases"] for r in trace} == {3}


def test_hold_is_a_mode_of_the_charger_and_the_variable_load_but_not_the_power_station():
    from custom_components.dynamic_ocpp_evse.const import (
        DEVICE_TYPE_EVSE, DEVICE_TYPE_POWER_STATION, DEVICE_TYPE_VARIABLE_LOAD,
    )
    from custom_components.dynamic_ocpp_evse.const.modes import modes_for

    held = lambda device: "Hold" in [m.key for m in modes_for(device)[0]]  # noqa: E731
    assert held(DEVICE_TYPE_EVSE) and held(DEVICE_TYPE_VARIABLE_LOAD)
    assert not held(DEVICE_TYPE_POWER_STATION)


def _sensor(hass, entry, rt):
    return SimpleNamespace(
        hass=hass, config_entry=entry, _runtime=lambda: rt, _attr_name=SLUG,
        _connector_status_entity=STATUS, _charge_control_entity="switch.none",
        _car_active_phases=3, _phases=3, _last_command_time=0.0,
        _last_commanded_limit=None, _last_set_current=None, _last_set_power=None,
        _last_update=None,
    )


async def test_the_profile_carries_the_phase_count_and_marks_the_switch(hass):
    hub = _hub("pswf")
    entry = _charger(hub, charge_rate_unit="A")
    calls = []

    async def set_charge_rate(call):
        calls.append(call.data["custom_profile"]["chargingSchedule"]["chargingSchedulePeriod"][0])

    hass.services.async_register("ocpp", "set_charge_rate", set_charge_rate)
    hass.states.async_set(STATUS, "Charging")
    rt = {EVSE_RT_PHASE_SUPPORTED: True, EVSE_RT_PHASES: 1}
    sensor = _sensor(hass, entry, rt)

    # a drawing charger is first paused at 0 A on its old count ...
    assert await send_ocpp_command(sensor, 10.0, hub, 50.0, effective_status="Charging", draw=30.0)
    assert (calls[-1]["limit"], calls[-1]["numberPhases"]) == (0.0, 3)
    assert rt[EVSE_RT_COMMANDED_PHASES] == 3 and rt[RT_PHASE_SWITCH_AT] == 50.0
    # valid for the longest pause on top of the usual validity, so sent once
    # ... held for at least 60 s, even with the draw gone, sending nothing ...
    assert not await send_ocpp_command(sensor, 10.0, hub, 100.0, effective_status="Charging", draw=0.0)
    # ... and while it still draws (a reading not yet in) ...
    assert not await send_ocpp_command(sensor, 10.0, hub, 120.0, effective_status="Charging", draw=29.0)
    assert len(calls) == 1
    # ... then the new count with the limit
    assert await send_ocpp_command(sensor, 10.0, hub, 130.0, effective_status="Charging", draw=0.0)
    assert (calls[-1]["limit"], calls[-1]["numberPhases"]) == (10.0, 1)
    assert rt[EVSE_RT_COMMANDED_PHASES] == 1 and rt[RT_PHASE_SWITCH_AT] == 130.0
    assert EVSE_RT_PHASE_PAUSE_AT not in rt

    rt[EVSE_RT_PHASE_SUPPORTED] = None   # not answered yet: no numberPhases
    assert await send_ocpp_command(sensor, 10.0, hub, 140.0, effective_status="Charging")
    assert "numberPhases" not in calls[-1]


def test_the_pause_before_a_switch():
    rt = {EVSE_RT_COMMANDED_PHASES: 3}
    # nothing due, or a 0 A command anyway: no pause
    assert phase_switch_pause(rt, 3, 10.0, 30.0, 0.0) == (10.0, 3)
    assert phase_switch_pause(rt, 1, 0.0, 0.0, 0.0) == (0.0, 1)
    assert EVSE_RT_PHASE_PAUSE_AT not in rt
    # a reading that never drops: the new count after 180 s at the most
    assert phase_switch_pause(rt, 1, 10.0, 30.0, 0.0) == (0.0, 3)
    assert phase_switch_pause(rt, 1, 10.0, 30.0, 179.0) == (0.0, 3)
    assert phase_switch_pause(rt, 1, 10.0, 30.0, 180.0) == (10.0, 1)
    # the engine changes its mind during the pause: the pause ends, nothing switches
    rt = {EVSE_RT_COMMANDED_PHASES: 3}
    phase_switch_pause(rt, 1, 10.0, 30.0, 0.0)
    assert phase_switch_pause(rt, 3, 10.0, 30.0, 10.0) == (10.0, 3)
    assert EVSE_RT_PHASE_PAUSE_AT not in rt


async def test_the_capability_is_read_once_in_the_background(hass):
    hub = _hub("pswg")
    entry = _charger(hub)
    asked = []

    async def get_configuration(call):
        asked.append(call.data["ocpp_key"])
        return {"value": "true"}

    hass.services.async_register(
        "ocpp", "get_configuration", get_configuration, supports_response=SupportsResponse.ONLY
    )
    rt = {}
    sensor = _sensor(hass, entry, rt)
    probe_phase_switching(sensor, 100.0)
    await hass.async_block_till_done()
    assert rt[EVSE_RT_PHASE_SUPPORTED] is True
    probe_phase_switching(sensor, 200.0)
    await hass.async_block_till_done()
    assert asked == ["ConnectorSwitch3to1PhaseSupported"]


def test_a_setpoint_dip_while_switching_is_not_a_manual_change():
    old = State("number.socket_limit", "16")
    new = State("number.socket_limit", "0", context=Context())
    rt = {"_commanded": {("number.socket_limit", "state"): {16.0: time.monotonic() - 100}}}
    assert manual_change(rt, old, new) is not None
    rt[RT_PHASE_SWITCH_AT] = time.monotonic()
    assert manual_change(rt, old, new) is None
    rt[RT_PHASE_SWITCH_AT] = time.monotonic() - PHASE_SWITCH_SETTLE_S - 1
    assert manual_change(rt, old, new) is not None


def test_phase_value():
    assert phase_value(SINGLE) == 1 and phase_value(MULTI) == 3
    assert phase_value(SINGLE, ["1", "3"]) == "1"
    assert phase_value(MULTI, ["1 phase", "3 phases"]) == "3 phases"
    assert phase_value(MULTI, ["single", "multi"]) is None


async def test_the_variable_load_writes_its_phase_entity(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={CONF_VL_PHASE_ENTITY_ID: "select.socket_phases"})
    hass.states.async_set("select.socket_phases", "3", {"options": ["1", "3"]})
    written = []

    async def select_option(call):
        written.append(call.data["option"])
        hass.states.async_set("select.socket_phases", call.data["option"], {"options": ["1", "3"]})

    hass.services.async_register("select", "select_option", select_option)
    rt = {}
    sensor = SimpleNamespace(hass=hass, config_entry=entry, _attr_name="socket")
    await _write_phase(sensor, rt, SINGLE, 70.0)
    await hass.async_block_till_done()
    await _write_phase(sensor, rt, SINGLE, 80.0)   # already holds it: no write
    await hass.async_block_till_done()
    assert written == ["1"]
    assert rt[RT_PHASE_SWITCH_AT] == 70.0


async def test_a_phase_change_pauses_first_and_three_restarts_at_the_minimum(hass):
    """The whole hub cycle - engine, load processor, OCPP call - with a 45 s
    command slot: a phase change first holds the charger at 0 A on its old
    count (at once, not at the slot) and sends the new count 60 s later - not
    at the slot, and with no profile in between; the way back to three, ten
    minutes after the change, pauses the same way and ramps up from the 6 A
    minimum, never starting at the 16 A one phase was allowed."""
    from custom_components.dynamic_ocpp_evse.const import CONF_UPDATE_FREQUENCY, EVSE_RT_COMMANDED_LIMIT
    from custom_components.dynamic_ocpp_evse.sensor import LoadJugglerDeviceSensor, async_run_hub_cycle

    from .closed_loop import clocked

    hub = _hub("pswh")
    entry = _charger(hub, charge_rate_unit="A", **{CONF_UPDATE_FREQUENCY: 45})
    hub.add_to_hass(hass)
    entry.add_to_hass(hass)
    hass.data[DOMAIN] = {
        "hubs": {hub.entry_id: {"entry": hub, "loads": [entry.entry_id]}},
        "loads": {entry.entry_id: {"entry": entry, "hub_entry_id": hub.entry_id,
                                   "dynamic_control": True, EVSE_RT_PHASE_SUPPORTED: True}},
    }
    sensor = LoadJugglerDeviceSensor(hass, entry, hub, SLUG, SLUG)
    hass.data[DOMAIN].setdefault("load_processors", {})[hub.entry_id] = {entry.entry_id: sensor}
    rt = hass.data[DOMAIN]["loads"][entry.entry_id]
    sent = []

    async def accept(domain, service, data, *args, **kwargs):
        if service == "set_charge_rate":
            period = data["custom_profile"]["chargingSchedule"]["chargingSchedulePeriod"][0]
            sent.append((clock.now, period["limit"], period.get("numberPhases")))

    def world(house_b):
        draw = rt.get(EVSE_RT_COMMANDED_LIMIT) or 0.0
        one = rt.get(EVSE_RT_COMMANDED_PHASES, 3) == 1
        house = dict(HOUSE, b=house_b)
        for phase in "abc":
            on = phase == "a" or not one
            hass.states.async_set(f"sensor.pswh_{phase}", f"{house[phase] + (draw if on else 0):.2f}", AMPS)
        hass.states.async_set(STATUS, "Charging")
        hass.states.async_set(f"sensor.{SLUG}_current", f"{draw:.2f}", AMPS)

    with clocked(accept) as clock:
        for i in range(400):
            world(16.0 if i < 20 else 2.0)
            await async_run_hub_cycle(hass, hub)
            clock.now += 2

    # the first cycle: 0 A on three phases, nothing more until the pause is over
    assert sent[0] == (10_000.0, 0.0, 3)
    ones = [s for s in sent if s[2] == 1]
    assert ones and ones[0][0] == 10_060.0 and ones[0][1] > 0
    assert [s for s in sent if 10_000.0 < s[0] < 10_060.0] == []
    # back to three: the engine decides ten minutes after its change (10 600 s,
    # no multiple of the 45 s slot), pauses on one phase, sends three 60 s later
    assert (10_600.0, 0.0, 1) in sent
    back = next(s for s in sent if s[2] == 3 and s[0] > ones[0][0])
    assert back[0] == 10_660.0
    # one ramp step above the minimum at most, nowhere near one phase's 16 A
    assert 6.0 <= back[1] <= 9.0
