"""A hot water tank driven through a ``water_heater`` entity instead of a
``climate`` one.

Machine-authored tests - not yet human-reviewed.

A water heater has no hvac_mode and reports no hvac_action, so the tank reads
whether it is heating off its power sensor and gates it by its target
temperature alone. Two met in the field (2026-09-25): a Vaillant with
TARGET_TEMPERATURE | OPERATION_MODE, no ON_OFF, operation modes heating /
hot_water_only / stand_by; and a MELCloud heat pump with ON_OFF too, whose
turn_off powers down the whole unit.

Runnable two ways:
  python3 dev/tests/test_water_heater_tank.py   (standalone, no pytest needed)
  pytest dev/tests/test_water_heater_tank.py    (Docker / CI tier)
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from standalone_loader import load_pure_modules  # noqa: E402

load_pure_modules(
    engine_modules=("hub_calculation",), control_modules=("hot_water_tank",)
)

from custom_components.dynamic_ocpp_evse.const import (  # noqa: E402
    DOMAIN,
    CONF_CLIMATE_ENTITY_ID,
    CONF_CONNECTED_TO_PHASE,
    CONF_HEATING_ELEMENT_POWER,
    CONF_TANK_POWER_ENTITY_ID,
)
from custom_components.dynamic_ocpp_evse.control.hot_water_tank import (  # noqa: E402
    send_hot_water_tank_command,
)
from custom_components.dynamic_ocpp_evse.engine.load_builders import (  # noqa: E402
    _build_hot_water_tank_load,
)

V = 230.0
WH = "water_heater.tank"
VAILLANT = 3  # TARGET_TEMPERATURE | OPERATION_MODE, no ON_OFF
MELCLOUD = 11  # TARGET_TEMPERATURE | OPERATION_MODE | ON_OFF


class FakeState:
    def __init__(self, state, unit=None, **attrs):
        self.state = state
        self.attributes = {"unit_of_measurement": unit} if unit else {}
        self.attributes.update(attrs)


class FakeStates:
    def __init__(self, mapping):
        self._mapping = mapping

    def get(self, entity_id):
        return self._mapping.get(entity_id)


class FakeServices:
    def __init__(self):
        self.calls = []

    async def async_call(self, domain, service, data, blocking=False):
        self.calls.append((domain, service, data))


class FakeHass:
    def __init__(self, mapping):
        self.states = FakeStates(mapping)
        self.services = FakeServices()
        self.data = {DOMAIN: {"loads": {"tank": {}}}}


class FakeEntry:
    def __init__(self, data):
        self.entry_id = "tank"
        self.data = data
        self.options = {}


class FakeSensor:
    _attr_name = "Tank"

    def __init__(self, hass, entry):
        self.hass = hass
        self.config_entry = entry


def _entry(power_sensor=True):
    data = {
        CONF_CLIMATE_ENTITY_ID: WH,
        CONF_CONNECTED_TO_PHASE: "A",
        CONF_HEATING_ELEMENT_POWER: 2000,
    }
    if power_sensor:
        data[CONF_TANK_POWER_ENTITY_ID] = "sensor.tank_power"
    return FakeEntry(data)


def _heater(state="heating", features=VAILLANT):
    return FakeState(
        state, supported_features=features, current_temperature=50,
        temperature=50, min_temp=35, max_temp=70,
    )


# --- reading: the power sensor says whether a water heater is heating ---


def test_a_heating_water_heater_is_read_off_its_power_sensor():
    hass = FakeHass({WH: _heater(), "sensor.tank_power": FakeState("1900", "W")})
    load = _build_hot_water_tank_load(hass, _entry(), V, "tank_1", 1)
    rt = hass.data[DOMAIN]["loads"]["tank"]
    assert rt["tank_hvac_action"] == "heating"
    assert load.connector_status == "Charging"
    assert rt["device_power"] == 1900  # learned, as a heating climate's would be
    assert abs(load.l1_current - 1900 / V) < 0.01


def test_a_satisfied_water_heater_frees_its_power():
    # Standby electronics only: the water is hot, so the tank is inactive and
    # the engine reallocates its rating - what a climate's "idle" does.
    hass = FakeHass({WH: _heater(), "sensor.tank_power": FakeState("3", "W")})
    load = _build_hot_water_tank_load(hass, _entry(), V, "tank_1", 1)
    rt = hass.data[DOMAIN]["loads"]["tank"]
    assert rt["tank_hvac_action"] == "idle"
    assert load.connector_status == "Available"
    assert "device_power" not in rt  # nothing learned from standby watts


def test_without_a_power_sensor_a_water_heater_colder_than_its_target_draws_its_rating():
    # The configured rating stands in for the missing meter, as it does for a
    # heating climate - so a boost does not read the tank's own draw as the
    # house eating the surplus that started it.
    heater = _heater()
    heater.attributes.update(current_temperature=42.5, temperature=60)
    hass = FakeHass({WH: heater})
    load = _build_hot_water_tank_load(hass, _entry(power_sensor=False), V, "tank_1", 1)
    assert hass.data[DOMAIN]["loads"]["tank"]["tank_hvac_action"] == "heating"
    assert load.connector_status == "Charging"
    assert abs(load.l1_current - 2000 / V) < 0.01


def test_without_a_power_sensor_a_water_heater_at_its_target_frees_its_power():
    heater = _heater()
    heater.attributes.update(current_temperature=60, temperature=60)
    hass = FakeHass({WH: heater})
    load = _build_hot_water_tank_load(hass, _entry(power_sensor=False), V, "tank_1", 1)
    assert hass.data[DOMAIN]["loads"]["tank"]["tank_hvac_action"] == "idle"
    assert load.connector_status == "Available"
    assert load.l1_current == 0


def test_without_a_power_sensor_or_a_temperature_nothing_is_assumed():
    heater = _heater()
    heater.attributes.pop("current_temperature")
    hass = FakeHass({WH: heater})
    load = _build_hot_water_tank_load(hass, _entry(power_sensor=False), V, "tank_1", 1)
    assert hass.data[DOMAIN]["loads"]["tank"]["tank_hvac_action"] is None
    assert load.connector_status == "Charging"
    assert load.l1_current == 0


def test_an_unreadable_power_sensor_decides_nothing():
    hass = FakeHass({WH: _heater(), "sensor.tank_power": FakeState("unknown", "W")})
    load = _build_hot_water_tank_load(hass, _entry(), V, "tank_1", 1)
    assert hass.data[DOMAIN]["loads"]["tank"]["tank_hvac_action"] is None
    assert load.connector_status == "Charging"
    assert load.draw_assumed is True


def test_a_water_heater_switched_off_reads_off():
    hass = FakeHass({WH: _heater("off", MELCLOUD), "sensor.tank_power": FakeState("0", "W")})
    _build_hot_water_tank_load(hass, _entry(), V, "tank_1", 1)
    assert hass.data[DOMAIN]["loads"]["tank"]["tank_hvac_action"] == "off"


# --- control: the water heater's target, never its power or its mode ---


def _command(heater, limit):
    hass = FakeHass({WH: heater})
    asyncio.run(
        send_hot_water_tank_command(FakeSensor(hass, _entry()), limit, {}, 0.0)
    )
    return hass.services.calls


def test_a_water_heater_is_never_switched_off():
    # MELCloud's turn_off powers down the whole heat pump, space heating
    # included, so even a heater that offers it is only ever given a target.
    assert _command(_heater(features=MELCLOUD), limit=0) == [
        ("water_heater", "set_temperature", {"entity_id": WH, "temperature": 35}),
    ]
    assert _command(_heater(features=MELCLOUD), limit=8.7) == [
        ("water_heater", "set_temperature", {"entity_id": WH, "temperature": 45}),
    ]


def test_a_water_heater_with_no_off_switch_is_held_at_its_lowest_target():
    # The Vaillant: no ON_OFF, and no operation mode that could be called off.
    assert _command(_heater(features=VAILLANT), limit=0) == [
        ("water_heater", "set_temperature", {"entity_id": WH, "temperature": 35}),
    ]


def test_a_target_already_in_place_is_not_written_again():
    # A cloud water heater rate-limits writes; the climate path's every-cycle
    # re-assert would be one call per cycle for ever.
    heater = _heater()
    heater.attributes["temperature"] = 45.0
    assert _command(heater, limit=8.7) == []
    heater.attributes["temperature"] = 35
    assert _command(heater, limit=0) == []


def test_the_setpoint_is_clamped_to_the_water_heaters_range():
    heater = _heater(features=MELCLOUD)
    heater.attributes["max_temp"] = 40  # below the 45 C normal setpoint
    calls = _command(heater, limit=8.7)
    assert calls[-1] == (
        "water_heater", "set_temperature", {"entity_id": WH, "temperature": 40.0},
    )


if __name__ == "__main__":
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
    print(f"\n{'FAILED' if failed else 'OK'} - {len(failed)} failure(s)")
    sys.exit(1 if failed else 0)
