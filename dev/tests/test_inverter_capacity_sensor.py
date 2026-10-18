"""An inverter's total capacity read from a sensor (JUG-20).

Machine-authored tests - not yet human-reviewed.
"""

from types import SimpleNamespace

from custom_components.dynamic_ocpp_evse.const import (
    CONF_INVERTER_MAX_POWER,
    CONF_INVERTER_MAX_POWER_ENTITY_ID,
)
from custom_components.dynamic_ocpp_evse.engine.readers import _inverter_capacity

SENSOR = "sensor.inverter_capacity"


class _States:
    def __init__(self):
        self.mapping = {}

    def get(self, entity_id):
        return self.mapping.get(entity_id)


def _state(value, unit="W"):
    return SimpleNamespace(state=str(value), attributes={"unit_of_measurement": unit})


def _entry(fixed=5000, sensor=SENSOR):
    options = {CONF_INVERTER_MAX_POWER: fixed}
    if sensor:
        options[CONF_INVERTER_MAX_POWER_ENTITY_ID] = sensor
    return SimpleNamespace(entry_id="inv", data={}, options=options)


def _hass(**states):
    hass = SimpleNamespace(states=_States())
    hass.states.mapping.update(states)
    return hass


def test_without_a_sensor_the_fixed_capacity_stands():
    assert _inverter_capacity(_hass(), _entry(sensor=None), {}) == 5000


def test_the_sensor_replaces_the_fixed_capacity_each_cycle():
    hass, rt, entry = _hass(**{SENSOR: _state(3000)}), {}, _entry()
    assert _inverter_capacity(hass, entry, rt) == 3000
    hass.states.mapping[SENSOR] = _state(1.5, "kW")
    assert _inverter_capacity(hass, entry, rt) == 1500


def test_an_over_temperature_alarm_that_drops_it_to_zero_still_constrains():
    # The engine reads a falsy capacity as unconfigured, so 0 W lands on 1 W.
    assert _inverter_capacity(_hass(**{SENSOR: _state(0)}), _entry(), {}) == 1.0


def test_an_unavailable_sensor_holds_its_last_reading():
    hass, rt, entry = _hass(**{SENSOR: _state(800)}), {}, _entry()
    assert _inverter_capacity(hass, entry, rt) == 800
    hass.states.mapping[SENSOR] = _state("unavailable")
    assert _inverter_capacity(hass, entry, rt) == 800


def test_a_sensor_that_never_read_falls_back_to_the_fixed_capacity():
    assert _inverter_capacity(_hass(), _entry(), {}) == 5000
