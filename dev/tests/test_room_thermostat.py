"""The room thermostat device type: the hot water tank's machinery with room labels and defaults."""

import asyncio

from custom_components.dynamic_ocpp_evse.const import (
    CONF_CLIMATE_ENTITY_ID,
    CONF_DEVICE_TYPE,
    DEVICE_TYPE_HOT_WATER_TANK,
    DEVICE_TYPE_ROOM_THERMOSTAT,
    DOMAIN,
    behaviour_type,
    thermostat_defaults,
)
from custom_components.dynamic_ocpp_evse.const.modes import modes_for, resolve_operating_mode
from custom_components.dynamic_ocpp_evse.control.hot_water_tank import send_hot_water_tank_command
from custom_components.dynamic_ocpp_evse.engine.load_builders import _build_hot_water_tank_load

from .test_water_heater_tank import FakeEntry, FakeHass, FakeSensor, FakeState

CLIMATE = "climate.bee_room"


def _room(temp, device_type=DEVICE_TYPE_ROOM_THERMOSTAT):
    device = FakeState("heat", current_temperature=temp, temperature=18.0, min_temp=5, max_temp=35)
    hass = FakeHass({CLIMATE: device})
    hass.data[DOMAIN]["loads"]["tank"]["operating_mode"] = "Normal"
    entry = FakeEntry({CLIMATE_KEY: CLIMATE, CONF_DEVICE_TYPE: device_type})
    return hass, device, FakeSensor(hass, entry)


CLIMATE_KEY = CONF_CLIMATE_ENTITY_ID


def test_a_room_thermostat_runs_on_the_tanks_machinery():
    assert behaviour_type(DEVICE_TYPE_ROOM_THERMOSTAT) == DEVICE_TYPE_HOT_WATER_TANK
    assert behaviour_type(DEVICE_TYPE_HOT_WATER_TANK) == DEVICE_TYPE_HOT_WATER_TANK
    assert modes_for(DEVICE_TYPE_ROOM_THERMOSTAT) == modes_for(DEVICE_TYPE_HOT_WATER_TANK)
    assert resolve_operating_mode(DEVICE_TYPE_ROOM_THERMOSTAT, "Solar Priority").key == "Solar Priority"


def test_the_defaults_are_a_rooms_not_a_tanks():
    assert thermostat_defaults(DEVICE_TYPE_ROOM_THERMOSTAT) == (7, 21, 24)
    assert thermostat_defaults(DEVICE_TYPE_HOT_WATER_TANK) == (30, 45, 65)


def test_a_room_with_no_stored_setpoints_is_sized_against_its_own_boost():
    for device_type, headroom in ((DEVICE_TYPE_ROOM_THERMOSTAT, 3.0), (DEVICE_TYPE_HOT_WATER_TANK, 44.0)):
        hass, _, sensor = _room(21.0, device_type)
        load = _build_hot_water_tank_load(hass, sensor.config_entry, 230.0, "tank_1", 1)
        assert load.device_type == DEVICE_TYPE_HOT_WATER_TANK      # the engine sees one kind of load
        assert load.thermal_headroom_k == headroom


def test_a_room_in_normal_mode_is_set_to_its_comfort_temperature():
    hass, device, sensor = _room(15.0)
    asyncio.run(send_hot_water_tank_command(sensor, 8.7, {"excess_available": False}, 0.0))
    targets = [d["temperature"] for _, s, d in hass.services.calls if s == "set_temperature"]
    assert targets and targets[-1] == 21.0, targets
