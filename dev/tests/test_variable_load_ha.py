"""JUG-30: a Variable Load through the REAL hub cycle - engine, load processor
(smoothing, command gate, pause) and the setpoint write - against a small
single-phase site.

Machine-authored tests - not yet human-reviewed.

The site: a 25 A breaker with a grid CT on phase A, a house, optionally the sun
(exporting through the CT), and a variable load on phase A behind a
``number`` setpoint in amps that draws, one cycle later, what it was last set to.
"""

from types import SimpleNamespace

from pytest_homeassistant_custom_component.common import MockConfigEntry

from .translation_steps import helps, labels
from custom_components.dynamic_ocpp_evse.config_flow.sections import flat_fields
from custom_components.dynamic_ocpp_evse import sensor as sensor_platform
from custom_components.dynamic_ocpp_evse.const import (
    CONF_CHARGER_L1_PHASE,
    CONF_CHARGER_L2_PHASE,
    CONF_CHARGER_L3_PHASE,
    CONF_DEVICE_TYPE,
    CONF_ENTITY_ID,
    CONF_HUB_ENTRY_ID,
    CONF_LOAD_PRIORITY,
    CONF_MAIN_BREAKER_RATING,
    CONF_NAME,
    CONF_PHASE_A_CURRENT_ENTITY_ID,
    CONF_PHASE_VOLTAGE,
    CONF_SOLAR_GRACE_PERIOD,
    CONF_UPDATE_FREQUENCY,
    CONF_VL_CONNECTED_ENTITY_ID,
    CONF_VL_CURRENT_L1_ENTITY_ID,
    CONF_VL_MAX,
    CONF_VL_MIN,
    CONF_VL_SETPOINT_ENTITY_ID,
    DEFAULT_SITE_UPDATE_FREQUENCY,
    DEVICE_TYPE_VARIABLE_LOAD,
    DOMAIN,
    ENTRY_TYPE,
    ENTRY_TYPE_HUB,
    ENTRY_TYPE_LOAD,
    EVSE_MODE_SOLAR_ONLY,
    EVSE_MODE_STANDARD,
    VL_MODE_HOLD,
    VL_RT_OUTPUTS,
)

from .closed_loop import clocked

V = 230.0
BREAKER_A = 25.0
CYCLE_S = float(DEFAULT_SITE_UPDATE_FREQUENCY)
AMPS = {"device_class": "current", "unit_of_measurement": "A"}
SETPOINT = "number.vl_setpoint"


class World:
    """The house, the sun and the device. Grid = house + device - sun (A)."""

    def __init__(self, hass, *, house_a, sun_a=0.0, entity_min=0.0, setpoint=True, connected=None):
        self.hass, self.house_a, self.sun_a = hass, house_a, sun_a
        self.entity_min, self.has_setpoint, self.connected = entity_min, setpoint, connected
        self.value = 0.0        # what the setpoint entity holds
        self.draw = 0.0         # what the device draws (follows the setpoint a cycle late)
        self.calls = []

    @property
    def grid(self):
        return self.house_a + self.draw - self.sun_a

    def publish(self):
        set_state = self.hass.states.async_set
        set_state("sensor.grid_a", str(round(self.grid, 2)), AMPS)
        set_state("sensor.vl_current", str(round(self.draw, 2)), AMPS)
        if self.has_setpoint:
            set_state(SETPOINT, str(self.value), {"min": self.entity_min, "max": 32, "step": 1})
        if self.connected is not None:
            set_state("binary_sensor.vl_connected", "on" if self.connected else "off")

    def step(self):
        if self.connected is False:
            self.draw = 0.0
        else:
            self.draw = self.value if self.value >= 6 else 0.0

    async def accept(self, domain, service, data=None, *args, **kwargs):
        self.calls.append((domain, service, dict(data or {})))
        if domain == "number" and service == "set_value":
            self.value = float(data["value"])


def _site(hass, *, mode, setpoint=True, connected=False, hub_options=None, **options):
    hub = MockConfigEntry(
        domain=DOMAIN, version=2, minor_version=10, title="Hub",
        data={CONF_NAME: "Hub", CONF_ENTITY_ID: "hub", ENTRY_TYPE: ENTRY_TYPE_HUB},
        options={
            CONF_PHASE_A_CURRENT_ENTITY_ID: "sensor.grid_a",
            CONF_MAIN_BREAKER_RATING: BREAKER_A,
            CONF_PHASE_VOLTAGE: V,
            **(hub_options or {}),
        },
    )
    load = MockConfigEntry(
        domain=DOMAIN, version=2, minor_version=10, title="Variable",
        data={
            CONF_ENTITY_ID: "vl", CONF_NAME: "Variable", ENTRY_TYPE: ENTRY_TYPE_LOAD,
            CONF_DEVICE_TYPE: DEVICE_TYPE_VARIABLE_LOAD, CONF_HUB_ENTRY_ID: hub.entry_id,
        },
        options={
            CONF_VL_MIN: 6, CONF_VL_MAX: 16,
            CONF_CHARGER_L1_PHASE: "A", CONF_CHARGER_L2_PHASE: None, CONF_CHARGER_L3_PHASE: None,
            CONF_LOAD_PRIORITY: 1,
            CONF_UPDATE_FREQUENCY: 5,
            CONF_SOLAR_GRACE_PERIOD: 0,
            CONF_VL_CURRENT_L1_ENTITY_ID: "sensor.vl_current",
            **({CONF_VL_SETPOINT_ENTITY_ID: SETPOINT} if setpoint else {}),
            **({CONF_VL_CONNECTED_ENTITY_ID: "binary_sensor.vl_connected"} if connected else {}),
            **options,
        },
    )
    hass.data[DOMAIN] = {
        "hubs": {hub.entry_id: {
            "entry": hub, "loads": [load.entry_id], "distribution_mode": "Priority",
            "allow_grid_charging": True, "power_buffer": 0,
            "battery_soc_min": 20, "battery_soc_target": 50,
        }},
        "loads": {load.entry_id: {
            "entry": load, "hub_entry_id": hub.entry_id, "dynamic_control": True,
            "operating_mode": mode,
        }},
    }
    hass.data[DOMAIN].setdefault("load_processors", {}).setdefault(hub.entry_id, {})[
        load.entry_id
    ] = sensor_platform.LoadJugglerDeviceSensor(hass, load, hub, "Variable", "vl")
    return SimpleNamespace(hub=hub, load=load, rt=hass.data[DOMAIN]["loads"][load.entry_id])


async def _run(hass, site, world, seconds, script=None):
    """Run the site for ``seconds``; ``script(world, t)`` moves the world first.
    One row per cycle: time, grid, draw, setpoint, published outputs."""
    log = []
    with clocked(world.accept) as clock:
        start = clock.now
        while clock.now - start < seconds:
            t = clock.now - start
            if script:
                script(world, t)
            world.publish()
            await sensor_platform.async_run_hub_cycle(hass, site.hub)
            log.append(SimpleNamespace(
                t=t, grid=world.grid, draw=world.draw, value=world.value,
                out=dict(site.rt.get(VL_RT_OUTPUTS) or {}),
            ))
            world.step()
            clock.now += CYCLE_S
    return log


async def test_standard_stays_under_the_fuse_shared_with_the_house(hass):
    """A 15 A house on a 25 A breaker leaves the device 10 A; the house going
    to 21 A leaves 4 A, under the device's 6 A minimum, so it stops. Outside
    the cut itself (the input filter and the ramp take some 12 s, as for a
    charger) the breaker is never over."""
    site = _site(hass, mode=EVSE_MODE_STANDARD.key)
    world = World(hass, house_a=15.0)

    def script(w, t):
        w.house_a = 15.0 if t < 120 else 21.0

    log = await _run(hass, site, world, 300, script)
    settled = [r for r in log if 30 <= r.t < 120]
    assert max(r.draw for r in settled) <= 10.0 + 0.05
    assert max(r.draw for r in settled) >= 9.0  # and it uses the room
    after = [r for r in log if r.t >= 140]
    assert all(r.draw == 0.0 for r in after), [(r.t, r.draw) for r in after][:5]
    steady = [r for r in log if r.t >= 30 and not 120 <= r.t < 135]
    assert max(r.grid for r in steady) <= BREAKER_A + 0.05, max(r.grid for r in steady)
    assert log[-1].out["allowed_lowest"] == 0.0


async def test_solar_only_follows_the_surplus_and_stops_without_it(hass):
    """Solar Only: 12 A of sun on a 2 A house - the device takes about the
    10 A surplus and the site does not import for it; at 4 A of sun (2 A spare,
    under its minimum) it stops."""
    site = _site(hass, mode=EVSE_MODE_SOLAR_ONLY.key)
    world = World(hass, house_a=2.0, sun_a=12.0)

    def script(w, t):
        w.sun_a = 12.0 if t < 200 else 4.0

    log = await _run(hass, site, world, 400, script)
    sunny = [r for r in log if 100 <= r.t < 200]
    assert 8.0 <= min(r.draw for r in sunny) and max(r.draw for r in sunny) <= 10.5, [r.draw for r in sunny]
    assert max(r.grid for r in sunny) <= 0.6  # no import beyond a dead band
    dull = [r for r in log if r.t >= 260]
    assert all(r.draw == 0.0 for r in dull), [(r.t, r.draw) for r in dull][:5]


async def test_output_only_publishes_the_allowance_and_writes_nothing(hass):
    """No setpoint entity: the allowance is published, no service is called."""
    site = _site(hass, mode=EVSE_MODE_STANDARD.key, setpoint=False)
    world = World(hass, house_a=15.0, setpoint=False)
    log = await _run(hass, site, world, 60)
    assert [c for c in world.calls if c[0] == "number"] == []
    out = log[-1].out
    assert out["allowed_lowest"] == 10.0
    assert out["allowed_power"] == 2300
    assert out["allowed_percent"] == 62
    assert "phase_advice" not in out  # one leg: nothing to advise


async def test_hold_writes_zero_or_the_entity_minimum(hass):
    """Hold claims nothing: the setpoint goes to 0, or to the entity's own
    minimum when it cannot take 0."""
    for entity_min, expected in ((0.0, 0.0), (6.0, 6.0)):
        site = _site(hass, mode=VL_MODE_HOLD.key)
        world = World(hass, house_a=5.0, entity_min=entity_min)
        world.value = 12.0
        log = await _run(hass, site, world, 60)
        assert log[-1].value == expected, (entity_min, [r.value for r in log])
        assert log[-1].out["allowed_lowest"] == 0.0
        status = hass.data[DOMAIN]["load_status"][site.load.entry_id]
        assert status == "Hold"


async def test_not_connected_is_no_demand(hass):
    """The connected sensor off: the device claims nothing, so the room it
    would take stays with the site; on, it runs."""
    site = _site(hass, mode=EVSE_MODE_STANDARD.key, connected=True)
    world = World(hass, house_a=5.0, connected=False)

    def script(w, t):
        w.connected = t >= 60

    log = await _run(hass, site, world, 150, script)
    assert all(r.draw == 0.0 for r in log if r.t < 60)
    assert max(r.draw for r in log if r.t >= 100) == 16.0


# ── Setup: the config flow, the options page, the entities ─────────────


import json  # noqa: E402
from pathlib import Path  # noqa: E402

import voluptuous as vol  # noqa: E402
from homeassistant.config_entries import ConfigEntryState  # noqa: E402
from homeassistant.data_entry_flow import FlowResultType  # noqa: E402

from custom_components.dynamic_ocpp_evse.config_flow.schemas import (  # noqa: E402
    _variable_load_schema,
)
from custom_components.dynamic_ocpp_evse.const import (  # noqa: E402
    CONF_VL_PHASE_MIN_INTERVAL,
    CONF_VL_PHASE_MULTI_ABOVE,
    CONF_VL_PHASE_SINGLE_BELOW,
    CONF_VL_POWER_ENTITY_ID,
    CONF_VL_RESEND_INTERVAL,
    CONF_VL_SETPOINT_UNIT,
)


async def _to_variable_load_page(hass, mock_hub_entry):
    mock_hub_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_hub_entry.entry_id)
    await hass.async_block_till_done()
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    options = flat_fields(result["data_schema"])["setup_type"].config["options"]
    assert "variable_load" in {o["value"] for o in options}
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={"setup_type": "variable_load"}
    )


_FORM = {
    CONF_NAME: "Alfen",
    CONF_ENTITY_ID: "lj_alfen",
    CONF_VL_SETPOINT_UNIT: "W",
    CONF_VL_MIN: 1380,
    CONF_VL_MAX: 11040,
    CONF_CHARGER_L1_PHASE: "A",
    CONF_CHARGER_L2_PHASE: "B",
    CONF_CHARGER_L3_PHASE: "C",
    CONF_LOAD_PRIORITY: 1,
    CONF_VL_POWER_ENTITY_ID: "sensor.alfen_power",
    CONF_UPDATE_FREQUENCY: 10,
    CONF_VL_RESEND_INTERVAL: 0,
    "vl_bias": 0,
    CONF_VL_PHASE_MULTI_ABOVE: 4000,
    CONF_VL_PHASE_SINGLE_BELOW: 2000,
    CONF_VL_PHASE_MIN_INTERVAL: 10,
    CONF_SOLAR_GRACE_PERIOD: 2,
    "manual_override_timeout": 120,
    "pace_over_horizon": False,
    "catch_up_enabled": False,
}


async def test_variable_load_creation_flow_output_only(hass, mock_hub_entry, mock_setup):
    """user -> variable_load -> (auto-selected hub) -> one page, every entity
    optional: no setpoint is output-only. Everything lands in options."""
    hass.states.async_set("sensor.alfen_power", "0", {"unit_of_measurement": "W"})
    result = await _to_variable_load_page(hass, mock_hub_entry)
    assert result["step_id"] == "variable_load_config"
    schema = {str(k): k for k in flat_fields(result["data_schema"])}
    assert schema[CONF_VL_PHASE_MULTI_ABOVE].default() == 4000
    assert schema[CONF_VL_PHASE_SINGLE_BELOW].default() == 2000
    assert schema[CONF_VL_PHASE_MIN_INTERVAL].default() == 10
    assert schema[CONF_VL_SETPOINT_UNIT].default() == "A"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input=_FORM)
    assert result["type"] == FlowResultType.CREATE_ENTRY, result.get("errors")
    entry = result["result"]
    assert entry.data[CONF_DEVICE_TYPE] == DEVICE_TYPE_VARIABLE_LOAD
    assert entry.data[CONF_HUB_ENTRY_ID] == mock_hub_entry.entry_id
    assert entry.options[CONF_VL_SETPOINT_ENTITY_ID] is None
    assert entry.options[CONF_VL_POWER_ENTITY_ID] == "sensor.alfen_power"
    assert entry.options[CONF_VL_MAX] == 11040
    assert entry.options[CONF_CHARGER_L3_PHASE] == "C"


async def test_variable_load_form_refuses_bad_bounds_bands_and_legs(hass, mock_hub_entry, mock_setup):
    result = await _to_variable_load_page(hass, mock_hub_entry)
    for change, field, error in (  # an error is keyed by the field's section
        ({CONF_VL_MIN: 12000}, "setpoint", "vl_min_exceeds_max"),
        ({CONF_VL_PHASE_SINGLE_BELOW: 5000}, "phase_advice", "vl_advice_band"),
        ({CONF_CHARGER_L2_PHASE: "A"}, "site", "charger_phase_duplicate"),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={**_FORM, **change}
        )
        assert result["type"] == FlowResultType.FORM
        assert result["errors"] == {field: error}


async def test_variable_load_options_saves_and_clears_the_setpoint(hass, mock_hub_entry, mock_setup):
    mock_hub_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_hub_entry.entry_id)
    await hass.async_block_till_done()
    entry = MockConfigEntry(
        domain=DOMAIN, version=2, minor_version=10, title="Heater",
        data={CONF_NAME: "Heater", CONF_ENTITY_ID: "heater", ENTRY_TYPE: ENTRY_TYPE_LOAD,
              CONF_DEVICE_TYPE: DEVICE_TYPE_VARIABLE_LOAD, CONF_HUB_ENTRY_ID: mock_hub_entry.entry_id},
        options={CONF_LOAD_PRIORITY: 1, CONF_VL_SETPOINT_ENTITY_ID: "number.heater_power",
                 CONF_CHARGER_L1_PHASE: "B", CONF_CHARGER_L2_PHASE: None, CONF_CHARGER_L3_PHASE: None},
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "settings"}
    )
    assert result["step_id"] == "variable_load"
    user_input = {
        str(k): k.default() for k in flat_fields(result["data_schema"]) if k.default is not vol.UNDEFINED
    }
    assert user_input[CONF_CHARGER_L1_PHASE] == "B"
    user_input[CONF_VL_MAX] = 20
    result = await hass.config_entries.options.async_configure(result["flow_id"], user_input=user_input)
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_VL_MAX] == 20
    assert entry.options[CONF_VL_SETPOINT_ENTITY_ID] is None  # cleared in the form
    assert entry.options[CONF_CHARGER_L2_PHASE] is None


async def test_variable_load_sets_up_its_allowance_sensors_and_a_hold_mode(hass, mock_hub_entry):
    """Through the real setup: a three-leg device gets the allowance sensors
    (lowest, power, percent, per leg, phase advice), the mode select with Hold,
    and no sliders."""
    from homeassistant.helpers import entity_registry as er

    mock_hub_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_hub_entry.entry_id)
    await hass.async_block_till_done()
    entry = MockConfigEntry(
        domain=DOMAIN, version=2, minor_version=10, title="Alfen",
        data={CONF_NAME: "Alfen", CONF_ENTITY_ID: "alfen", ENTRY_TYPE: ENTRY_TYPE_LOAD,
              CONF_DEVICE_TYPE: DEVICE_TYPE_VARIABLE_LOAD, CONF_HUB_ENTRY_ID: mock_hub_entry.entry_id},
        options={CONF_LOAD_PRIORITY: 1, CONF_CHARGER_L1_PHASE: "A",
                 CONF_CHARGER_L2_PHASE: "B", CONF_CHARGER_L3_PHASE: "C"},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED

    entities = er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    unique = {e.unique_id for e in entities}
    for suffix in ("allowed_current", "allowed_power", "allowed_percent", "allowed_current_l1",
                   "allowed_current_l2", "allowed_current_l3", "phase_advice"):
        assert any(u.endswith(f"_{suffix}") for u in unique), suffix
    assert "number" not in {e.domain for e in entities}
    select = next(e.entity_id for e in entities if e.domain == "select")
    assert "Hold" in hass.states.get(select).attributes["options"]


def test_variable_load_strings_exist_in_both_languages():
    """Every field of both pages has a label and help text, and both errors
    exist, in en and sl alike."""
    fields = {str(k) for k in _variable_load_schema({}, 3).schema}
    base = Path("custom_components/dynamic_ocpp_evse")
    for name in ("translations/en.json", "translations/sl.json"):
        data = json.loads((base / name).read_text(encoding="utf-8"))
        for section, step, extra in (
            ("config", "variable_load_config", {CONF_NAME, CONF_ENTITY_ID}),
            ("options", "variable_load", set()),
        ):
            page = data[section]["step"][step]
            assert page["title"] and page["description"], (name, step)
            assert set(labels(page)) | set(page.get("data", {})) == fields | extra, (name, step, set(labels(page)) ^ (fields | extra))
            assert set(helps(page)) >= fields, (name, step, fields - set(helps(page)))
            for error in ("vl_min_exceeds_max", "vl_advice_band"):
                assert data[section]["error"][error], (name, section, error)


# ── Single-phase mode: sized on the legs it draws on (owner, 2026-10-21) ──


from custom_components.dynamic_ocpp_evse.const import (  # noqa: E402
    CONF_PHASE_B_CURRENT_ENTITY_ID,
    CONF_PHASE_C_CURRENT_ENTITY_ID,
    CONF_VL_CURRENT_L2_ENTITY_ID,
    CONF_VL_CURRENT_L3_ENTITY_ID,
)


class World3:
    """A three-phase site and a three-leg device that runs on one leg (L1 = A)
    or on all three, drawing what it was last set to on each leg it uses."""

    def __init__(self, hass, house, legs_on=1, value=0.0):
        self.hass, self.house, self.legs_on = hass, list(house), legs_on
        self.value, self.calls = value, []
        self.draw = [0.0, 0.0, 0.0]
        self.step()

    @property
    def grid(self):
        return [h + d for h, d in zip(self.house, self.draw)]

    def publish(self):
        set_state = self.hass.states.async_set
        for phase, amps in zip("abc", self.grid):
            set_state(f"sensor.grid_{phase}", str(round(amps, 2)), AMPS)
        for n, amps in enumerate(self.draw, 1):
            set_state(f"sensor.vl_current_l{n}", str(round(amps, 2)), AMPS)
        set_state(SETPOINT, str(self.value), {"min": 0, "max": 32, "step": 0.1})

    def step(self):
        on = self.value if self.value >= 6 else 0.0
        self.draw = [on if n < self.legs_on else 0.0 for n in range(3)]

    async def accept(self, domain, service, data=None, *args, **kwargs):
        self.calls.append((domain, service, dict(data or {})))
        if domain == "number" and service == "set_value":
            self.value = float(data["value"])


def _site3(hass, **options):
    return _site(
        hass, mode=EVSE_MODE_STANDARD.key, **options,
        hub_options={CONF_PHASE_B_CURRENT_ENTITY_ID: "sensor.grid_b",
                     CONF_PHASE_C_CURRENT_ENTITY_ID: "sensor.grid_c"},
        **{CONF_CHARGER_L2_PHASE: "B", CONF_CHARGER_L3_PHASE: "C",
           CONF_VL_CURRENT_L1_ENTITY_ID: "sensor.vl_current_l1",
           CONF_VL_CURRENT_L2_ENTITY_ID: "sensor.vl_current_l2",
           CONF_VL_CURRENT_L3_ENTITY_ID: "sensor.vl_current_l3"},
    )


async def _run3(hass, site, world, seconds, script=None):
    log = []
    with clocked(world.accept) as clock:
        start = clock.now
        while clock.now - start < seconds:
            t = clock.now - start
            if script:
                script(world, t)
            world.publish()
            await sensor_platform.async_run_hub_cycle(hass, site.hub)
            log.append(SimpleNamespace(t=t, grid=world.grid, draw=list(world.draw), value=world.value,
                                       out=dict(site.rt.get(VL_RT_OUTPUTS) or {})))
            world.step()
            clock.now += CYCLE_S
    return log


async def test_one_leg_in_use_runs_on_room_for_one_minimum(hass):
    """Phases B and C carry 22 A of house on a 25 A breaker - 3 A spare, under
    the 6 A minimum - and A has 8 A spare. Sized on its three configured legs
    the device could not run; running on L1 only it is sized on that leg and
    keeps going, inside A's room."""
    site = _site3(hass)
    world = World3(hass, house=[17.0, 22.0, 22.0], legs_on=1, value=6.0)
    log = await _run3(hass, site, world, 120)
    later = [r for r in log if r.t >= 30]
    assert all(6.0 <= r.draw[0] <= 8.05 for r in later), [r.draw for r in later][:5]
    assert max(max(r.grid) for r in later) <= BREAKER_A + 0.05
    assert later[-1].out["allowed_l2"] == 0.0 and later[-1].out["allowed_l3"] == 0.0


async def test_switching_to_three_legs_without_room_stops_at_once(hass):
    """The same device at 16 A on L1 (A has the room) is switched to three
    phases by its owner while B and C have 3 A spare. Load Juggler sees it on
    the next reading and sets 0 at once - not at its command slot, not after
    the one-cycle confirmation - and the fuse holds from then on. The device's
    own switch is the only overload: the cycle it draws 16 A on B and C before
    anyone can see it."""
    site = _site3(hass)
    world = World3(hass, house=[5.0, 22.0, 22.0], legs_on=1, value=16.0)

    def script(w, t):
        w.legs_on = 1 if t < 60 else 3

    log = await _run3(hass, site, world, 150, script)
    assert all(r.draw[0] == 16.0 for r in log if 20 <= r.t < 60)
    switched = [r for r in log if r.t >= 60]
    over = [r.t for r in switched if max(r.grid) > BREAKER_A + 0.05]
    assert over and max(over) <= 64.0, over  # the switch, and the reading of it
    assert all(r.value == 0.0 for r in switched if r.t >= 64), [(r.t, r.value) for r in switched][:6]


async def test_switching_to_three_legs_with_room_ramps_from_the_minimum(hass):
    """Room on every phase: after the switch the allowance restarts at the 6 A
    minimum and climbs through the normal ramp (one step: 8.5 A), never
    straight to the 16 A it had on one leg."""
    site = _site3(hass)
    world = World3(hass, house=[5.0, 5.0, 5.0], legs_on=1, value=16.0)

    def script(w, t):
        w.legs_on = 1 if t < 60 else 3

    log = await _run3(hass, site, world, 240, script)
    after = [r.value for r in log if r.t >= 62]
    assert after[0] < 10.0, after[:5]  # the minimum plus one ramp step
    assert after == sorted(after), after[:20]  # rising, not jumping back
    assert after[-1] == 16.0
    assert all(max(r.grid) <= BREAKER_A + 0.05 for r in log if r.t >= 64)


async def test_phase_advice_on_one_leg_reads_what_all_its_legs_could_take(hass):
    """Running on L1 with 20 A of room on every phase, 16 A on one leg is
    3680 W - under the 4000 W multi threshold - but on all three legs it could
    take 3 x 16 A = 11 kW, so the advice is multi. Room shrinking to 4 A on B
    and C (2760 W on three legs) is between the thresholds: multi holds. To 2 A
    (1380 W): single, but only once the minimum time since the last change
    (5 min here) has passed."""
    from custom_components.dynamic_ocpp_evse.const import CONF_VL_PHASE_MIN_INTERVAL

    site = _site3(hass, **{CONF_VL_PHASE_MIN_INTERVAL: 5})
    world = World3(hass, house=[5.0, 5.0, 5.0], legs_on=1, value=16.0)

    def script(w, t):
        w.house[1] = w.house[2] = 5.0 if t < 60 else 21.0 if t < 120 else 23.0

    log = await _run3(hass, site, world, 400, script)
    advice = [(r.t, r.out.get("phase_advice")) for r in log if r.out.get("phase_advice")]
    assert all(r.draw[0] == 16.0 for r in log if r.t >= 20)  # on one leg throughout
    first_single = min(t for t, a in advice if a == "single")
    assert all(a == "multi" for t, a in advice if t < first_single), advice[:5]
    assert advice[0][0] < 10 and first_single >= 300, (advice[:3], first_single)
    assert all(a == "single" for t, a in advice if t >= first_single)


# ── Stale grid CTs and the fallback setpoint (owner, 2026-10-21) ────────


class BlindWorld(World):
    """The grid CT goes unavailable from ``dead_from`` seconds on."""

    dead = False

    def publish(self):
        super().publish()
        if self.dead:
            self.hass.states.async_set("sensor.grid_a", "unavailable")


async def test_stale_grid_keeps_a_running_device_at_its_minimum_like_a_charger(hass):
    """The grid CT dies: past the 60 s stale timeout a running variable load is
    held at its minimum, exactly as a charging EVSE is."""
    site = _site(hass, mode=EVSE_MODE_STANDARD.key)
    world = BlindWorld(hass, house_a=5.0)

    def script(w, t):
        w.dead = t >= 40

    log = await _run(hass, site, world, 200, script)
    assert max(r.value for r in log if r.t < 40) == 16.0
    assert all(r.value == 6.0 for r in log if r.t >= 140), [(r.t, r.value) for r in log][60:70]


async def test_stale_grid_gives_an_idle_device_nothing(hass):
    """Active (no gate) but drawing nothing when the CT dies: past the timeout
    it is given nothing, as an EVSE that is not charging."""
    site = _site(hass, mode=VL_MODE_HOLD.key)
    world = BlindWorld(hass, house_a=5.0)

    def script(w, t):
        w.dead = t >= 20
        if t >= 100:  # past the 60 s timeout: wants power, blind
            site.rt["operating_mode"] = EVSE_MODE_STANDARD.key

    log = await _run(hass, site, world, 200, script)
    assert all(r.value == 0.0 for r in log), [(r.t, r.value) for r in log if r.value]


async def test_unload_writes_the_fallback_setpoint_only_when_set(hass, mock_hub_entry):
    from custom_components.dynamic_ocpp_evse.const import CONF_VL_FALLBACK_SETPOINT

    mock_hub_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_hub_entry.entry_id)
    await hass.async_block_till_done()
    calls = []

    async def record(call):
        calls.append(dict(call.data))

    hass.services.async_register("number", "set_value", record)
    hass.states.async_set("number.heater", "12", {"min": 0, "max": 32})
    for fallback, expected in ((None, []), (0, [{"entity_id": "number.heater", "value": 0.0}])):
        calls.clear()
        entry = MockConfigEntry(
            domain=DOMAIN, version=2, minor_version=10, title="Heater",
            data={CONF_NAME: "Heater", CONF_ENTITY_ID: f"heater_{fallback}", ENTRY_TYPE: ENTRY_TYPE_LOAD,
                  CONF_DEVICE_TYPE: DEVICE_TYPE_VARIABLE_LOAD, CONF_HUB_ENTRY_ID: mock_hub_entry.entry_id},
            options={CONF_LOAD_PRIORITY: 1, CONF_VL_SETPOINT_ENTITY_ID: "number.heater",
                     CONF_CHARGER_L1_PHASE: "A", CONF_CHARGER_L2_PHASE: None, CONF_CHARGER_L3_PHASE: None,
                     **({CONF_VL_FALLBACK_SETPOINT: fallback} if fallback is not None else {})},
        )
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        calls.clear()  # whatever the first cycle wrote
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
        assert calls == expected, (fallback, calls)
