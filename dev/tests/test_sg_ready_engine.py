"""JUG-24: the SG Ready device on the engine side - the load builder, the state hook
(the mapping onto the allocation) and the relay actuation. Fake hass, no HA fixtures.

Machine-authored tests - not yet human-reviewed.
"""
import asyncio

import pytest

from custom_components.dynamic_ocpp_evse.calculations import sg_ready as sg
from custom_components.dynamic_ocpp_evse.calculations.models import (
    LoadContext, PhaseValues, SiteContext,
)
from custom_components.dynamic_ocpp_evse.const import (
    CONF_CONNECTED_TO_PHASE,
    CONF_LOAD_PRIORITY,
    CONF_SG_NOMINAL_POWER,
    CONF_SG_POWER_ENTITY_ID,
    CONF_SG_RELAY_1_ENTITY_ID,
    CONF_SG_RELAY_2_ENTITY_ID,
    DOMAIN,
    SG_RT_MEMORY,
    SG_RT_NORMAL_DRAW,
    SG_RT_STATUS,
    SG_VERIFY_S,
)
from custom_components.dynamic_ocpp_evse.control.sg_ready import send_sg_ready_command
from custom_components.dynamic_ocpp_evse.engine import hub_calculation as hc
from custom_components.dynamic_ocpp_evse.engine.load_builders import _build_sg_ready_load

R1, R2, POWER = "switch.r1", "switch.r2", "sensor.hp_power"
MIN = 600.0


class State:
    def __init__(self, state, unit=None):
        self.state = state
        self.attributes = {"unit_of_measurement": unit} if unit else {}


class Services:
    def __init__(self, hass):
        self.hass, self.calls, self.fail_on, self.obey = hass, [], None, True

    async def async_call(self, domain, service, data, blocking=False, context=None):
        assert blocking, "relay steps must be awaited one after the other"
        if self.fail_on == len(self.calls):
            self.calls.append(None)
            raise RuntimeError("boom")
        self.calls.append((service, data["entity_id"]))
        if self.obey:
            self.hass.states.mapping[data["entity_id"]] = State("on" if service == "turn_on" else "off")


class States:
    def __init__(self):
        self.mapping = {}

    def get(self, entity_id):
        return self.mapping.get(entity_id)


class Hass:
    def __init__(self):
        self.states, self.services = States(), Services(self)
        self.states.mapping.update({R1: State("off"), R2: State("off")})
        self.data = {DOMAIN: {"loads": {}}}


class Entry:
    def __init__(self, entry_id="hp", **options):
        self.entry_id = entry_id
        self.data = {CONF_SG_RELAY_1_ENTITY_ID: R1, CONF_SG_RELAY_2_ENTITY_ID: R2}
        self.options = {CONF_SG_NOMINAL_POWER: 2300, CONF_CONNECTED_TO_PHASE: "A", **options}


def setup(hass, entry):
    rt = hass.data[DOMAIN]["loads"][entry.entry_id] = {"entry": entry, "dynamic_control": True}
    return rt


# --- the builder -------------------------------------------------------------

def test_the_extra_draw_counts_only_in_recommended_and_force_and_normal_is_household():
    hass, entry = Hass(), Entry()
    rt = setup(hass, entry)
    load = _build_sg_ready_load(hass, entry, 230, "hp", 1)
    assert load.l1_current == 0 and load.connector_status == "Charging"
    assert load.max_current == pytest.approx(10.0)  # 2300 W / 230 V
    rt[SG_RT_MEMORY] = sg.SgMemory(state=sg.RECOMMENDED)
    load = _build_sg_ready_load(hass, entry, 230, "hp", 1)
    assert load.l1_current == pytest.approx(10.0)  # no sensor: the nominal power


def test_the_power_sensor_learns_the_normal_draw_and_only_the_excess_over_it_is_managed():
    hass, entry = Hass(), Entry(**{CONF_SG_POWER_ENTITY_ID: POWER})
    rt = setup(hass, entry)
    hass.states.mapping[POWER] = State("800", "W")
    _build_sg_ready_load(hass, entry, 230, "hp", 1)  # in Normal: 800 W is Normal
    assert rt[SG_RT_NORMAL_DRAW] == 800
    rt[SG_RT_MEMORY] = sg.SgMemory(state=sg.FORCE)
    hass.states.mapping[POWER] = State("2.8", "kW")
    load = _build_sg_ready_load(hass, entry, 230, "hp", 1)
    assert load.l1_current * 230 == pytest.approx(2000)
    assert rt[SG_RT_NORMAL_DRAW] == 800  # not re-learned while it runs harder
    hass.states.mapping[POWER] = State("unavailable")  # held
    load = _build_sg_ready_load(hass, entry, 230, "hp", 1)
    assert load.l1_current * 230 == pytest.approx(2000)


# --- the hook: the mapping onto the allocation ---------------------------------

def site(consumption=(5.0, 0.0, 0.0), soc=None, target=None, full=None):
    return SiteContext(
        voltage=230, main_breaker_rating=25,
        consumption=PhaseValues(*consumption), export_current=PhaseValues(0.0, 0.0, 0.0),
        battery_soc=soc, battery_soc_target=target, battery_soc_full=full,
    )


def device(hass, entry_id="hp", priority=1, granted=True, phase="A", **options):
    entry = Entry(entry_id, **options)
    rt = setup(hass, entry)
    rt[SG_RT_MEMORY] = sg.SgMemory(entered_at=0.0)
    load = LoadContext(
        load_id=entry_id, entity_id=entry_id, min_current=10, max_current=10, phases=1,
        priority=priority, active_phases_mask=phase, device_type="sg_ready",
        mode_behavior="binary_excess", mode_priority=4,
    )
    load.available_current = 10.0 if granted else 0.0
    return load, rt


def decide(hass, s, loads, runtime=None, excess_on=False, now=MIN + 1):
    s.loads = loads
    return hc._apply_sg_ready(hass, runtime if runtime is not None else {}, s, excess_on, now)


def state_of(out, load):
    return out[load.load_id]["state"]


def test_force_needs_the_grant_and_actual_excess():
    hass = Hass()
    load, _ = device(hass)
    assert state_of(decide(hass, site(), [load], excess_on=True), load) == sg.FORCE
    load2, _ = device(hass, granted=False)
    assert state_of(decide(hass, site(), [load2], excess_on=True), load2) == sg.NORMAL
    # a grant that rests on the forecast alone is not actual excess
    load3, _ = device(hass)
    assert state_of(decide(hass, site(), [load3], excess_on=False), load3) == sg.NORMAL


def test_a_full_battery_counts_as_actual_excess_like_the_excess_behaviour_does():
    hass = Hass()
    load, _ = device(hass)
    out = decide(hass, site(soc=100, target=80, full=98), [load], excess_on=False)
    assert state_of(out, load) == sg.FORCE


def test_early_start_recommends_from_the_forecast_source_only_by_default():
    hass = Hass()
    load, _ = device(hass)
    assert state_of(decide(hass, site(), [load], {"_early_planned_on": True}), load) == sg.RECOMMENDED
    load2, _ = device(hass)
    out = decide(hass, site(), [load2], {"_early_clip_on": True})
    assert state_of(out, load2) == sg.NORMAL  # the clipping early start is not followed


def test_following_the_clipping_early_start_is_one_flag(monkeypatch):
    monkeypatch.setattr(sg, "FOLLOW_CLIP_EARLY", True)
    hass = Hass()
    load, _ = device(hass)
    assert state_of(decide(hass, site(), [load], {"_early_clip_on": True}), load) == sg.RECOMMENDED


def test_battery_above_target_recommends_and_a_battery_free_site_never_does():
    hass = Hass()
    load, _ = device(hass)
    out = decide(hass, site(soc=90, target=80, full=98), [load])
    assert (state_of(out, load), out[load.load_id]["reason"]) == (sg.RECOMMENDED, "battery above target")
    load2, _ = device(hass)
    assert state_of(decide(hass, site(soc=70, target=80, full=98), [load2]), load2) == sg.NORMAL
    load3, _ = device(hass)
    assert state_of(decide(hass, site(), [load3]), load3) == sg.NORMAL


def test_overload_blocks_and_it_is_published_for_the_actuator():
    hass = Hass()
    load, rt = device(hass)
    out = decide(hass, site(consumption=(27.0, 0.0, 0.0)), [load], excess_on=True)
    assert state_of(out, load) == sg.BLOCK
    assert rt[SG_RT_STATUS]["state"] == sg.BLOCK and rt[SG_RT_STATUS]["blocks_24h"] == 1


def test_overload_reaches_the_lowest_priority_device_first():
    hass = Hass()
    a, _ = device(hass, "a", priority=1)
    b, _ = device(hass, "b", priority=2)
    for load in (a, b):
        hass.data[DOMAIN]["loads"][load.load_id]["entry"].options[CONF_SG_NOMINAL_POWER] = 2300
    # 1.5 A over the breaker (345 W): the second device's 2300 W Normal draw covers it
    out = decide(hass, site(consumption=(26.5, 0.0, 0.0)), [a, b])
    assert state_of(out, b) == sg.BLOCK and state_of(out, a) == sg.NORMAL


def test_no_block_off_grid_and_the_release_has_no_headroom_to_wait_for():
    hass = Hass()
    load, _ = device(hass)
    s = site(consumption=(27.0, 0.0, 0.0))
    s.is_off_grid = True
    assert state_of(decide(hass, s, [load]), load) == sg.NORMAL


def test_hands_off_publishes_normal_and_forgets_the_history():
    hass = Hass()
    load, rt = device(hass)
    load.dynamic_control = False
    out = decide(hass, site(), [load])
    assert out[load.load_id]["hands_off"] and out[load.load_id]["state"] == sg.NORMAL


def test_unavailable_relay_goes_normal():
    hass = Hass()
    load, rt = device(hass)
    rt[SG_RT_MEMORY] = sg.SgMemory(state=sg.FORCE, entered_at=0.0)
    hass.states.mapping[R2] = State("unavailable")
    out = decide(hass, site(), [load], excess_on=True)
    assert state_of(out, load) == sg.NORMAL


# --- actuation ------------------------------------------------------------------

class Sensor:
    _attr_name = "Heat Pump"

    def __init__(self, hass, entry):
        self.hass, self.config_entry, self._last_command_time = hass, entry, -1e9

    def _runtime(self):
        return self.hass.data[DOMAIN]["loads"][self.config_entry.entry_id]


def send(sensor, state, now, **extra):
    asyncio.run(send_sg_ready_command(sensor, {"state": state, **extra}, now))


def rig(r1="off", r2="off"):
    hass, entry = Hass(), Entry()
    setup(hass, entry)
    hass.states.mapping[R1], hass.states.mapping[R2] = State(r1), State(r2)
    return hass, Sensor(hass, entry)


def test_block_to_recommended_passes_through_normal_one_relay_at_a_time():
    hass, sensor = rig(r1="on")
    send(sensor, sg.RECOMMENDED, 100)
    assert hass.services.calls == [("turn_off", R1), ("turn_on", R2)]


def test_normal_to_force_goes_via_recommended():
    hass, sensor = rig()
    send(sensor, sg.FORCE, 100)
    assert hass.services.calls == [("turn_on", R2), ("turn_on", R1)]


def test_a_failing_second_call_leaves_the_mild_state_and_flags_a_fault():
    hass, sensor = rig(r1="on")
    hass.services.fail_on = 1
    send(sensor, sg.RECOMMENDED, 100)
    assert hass.states.mapping[R1].state == "off" and hass.states.mapping[R2].state == "off"  # Normal
    assert sensor._runtime()["sg_relay_fault"] is True


def test_nothing_is_sent_when_the_relays_already_read_as_asked():
    hass, sensor = rig(r2="on")
    send(sensor, sg.RECOMMENDED, 100)
    assert hass.services.calls == []


def test_relays_that_do_not_obey_become_a_fault_after_the_verify_time():
    hass, sensor = rig()
    rt = sensor._runtime()
    hass.services.obey = False  # a Shelly that takes the call and does nothing
    send(sensor, sg.FORCE, 0)
    send(sensor, sg.FORCE, SG_VERIFY_S - 1)
    assert not rt.get("sg_relay_fault")
    send(sensor, sg.FORCE, SG_VERIFY_S + 1)
    assert rt["sg_relay_fault"]


def test_unreadable_relays_are_not_driven():
    hass, sensor = rig()
    hass.states.mapping[R1] = State("unavailable")
    send(sensor, sg.FORCE, 100)
    assert hass.services.calls == []


def test_hands_off_drops_to_normal_once_then_leaves_the_relays_alone():
    hass, sensor = rig(r2="on")
    send(sensor, sg.NORMAL, 100, hands_off=True)
    assert hass.services.calls == [("turn_off", R2)]
    hass.states.mapping[R2] = State("on")  # the user then operates it
    send(sensor, sg.NORMAL, 200, hands_off=True)
    assert hass.services.calls == [("turn_off", R2)]


def test_a_manual_override_is_not_overridden():
    hass, sensor = rig(r1="on")
    sensor._runtime()["manual_until"] = object()
    send(sensor, sg.NORMAL, 100, hands_off=True)
    assert hass.services.calls == []
