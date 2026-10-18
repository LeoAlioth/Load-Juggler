"""The base house consumption read from a sensor (hub setting, clipping forecast).

Machine-authored tests - not yet human-reviewed.
"""

from types import SimpleNamespace

from custom_components.dynamic_ocpp_evse.const import (
    CONF_BASE_CONSUMPTION,
    CONF_BASE_CONSUMPTION_ENTITY_ID,
)
from custom_components.dynamic_ocpp_evse.engine.readers import _read_base_consumption

SENSOR = "sensor.base_consumption"


class _States:
    def __init__(self):
        self.mapping = {}

    def get(self, entity_id):
        return self.mapping.get(entity_id)


def _state(value, unit="W"):
    return SimpleNamespace(state=str(value), attributes={"unit_of_measurement": unit})


def _entry(typed=300, sensor=SENSOR):
    options = {CONF_BASE_CONSUMPTION: typed}
    if sensor:
        options[CONF_BASE_CONSUMPTION_ENTITY_ID] = sensor
    return SimpleNamespace(entry_id="hub", data={}, options=options)


def _hass(**states):
    hass = SimpleNamespace(states=_States())
    hass.states.mapping.update(states)
    return hass


def test_without_a_sensor_the_typed_figure_stands():
    assert _read_base_consumption(_hass(), _entry(sensor=None), {}) == 300


def test_the_sensor_replaces_the_typed_figure_each_cycle():
    hass, rt, entry = _hass(**{SENSOR: _state(450)}), {}, _entry()
    assert _read_base_consumption(hass, entry, rt) == 450
    hass.states.mapping[SENSOR] = _state(0.2, "kW")
    assert _read_base_consumption(hass, entry, rt) == 200


def test_an_unavailable_sensor_holds_its_last_reading():
    hass, rt, entry = _hass(**{SENSOR: _state(450)}), {}, _entry()
    assert _read_base_consumption(hass, entry, rt) == 450
    hass.states.mapping[SENSOR] = _state("unavailable")
    assert _read_base_consumption(hass, entry, rt) == 450


def test_a_sensor_that_never_read_falls_back_to_the_typed_figure():
    assert _read_base_consumption(_hass(), _entry(), {}) == 300


def test_a_negative_reading_counts_as_zero():
    assert _read_base_consumption(_hass(**{SENSOR: _state(-5)}), _entry(), {}) == 0
