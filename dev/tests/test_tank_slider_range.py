"""A tank's temperature sliders are bounded by its thermostat's own range.

Machine-authored tests - not yet human-reviewed.

The sliders are the only place the away / normal / boost temperatures are set
(the setup and settings pages no longer ask, 2026-09-25), so they take the
thermostat's min_temp / max_temp - a MELCloud heat pump's water heater accepts
40-60 °C, where the defaults are 30 / 45 / 65. Needs Home Assistant (Docker
tier).
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from custom_components.dynamic_ocpp_evse.const import (
    CONF_CLIMATE_ENTITY_ID,
    CONF_TANK_AWAY_TEMPERATURE,
    CONF_TANK_BOOST_TEMPERATURE,
    DEFAULT_TANK_AWAY_TEMPERATURE,
    DEFAULT_TANK_BOOST_TEMPERATURE,
)
from custom_components.dynamic_ocpp_evse.number import TankTemperatureSlider

WH = "water_heater.tc"


def _slider(kind, conf_key, default, thermostat, restored=None):
    states = {WH: thermostat} if thermostat is not None else {}
    hass = MagicMock()
    hass.states.get = states.get
    entry = SimpleNamespace(
        entry_id="tank", data={CONF_CLIMATE_ENTITY_ID: WH}, options={}
    )
    slider = TankTemperatureSlider(
        hass, entry, "Tank", "tank", kind, conf_key, default, kind.title()
    )
    slider.async_get_last_state = AsyncMock(
        return_value=None if restored is None else SimpleNamespace(state=str(restored))
    )
    slider.async_write_ha_state = MagicMock()
    slider._write_to_load_data = MagicMock()
    # What async_added_to_hass does, less HA's own setup and the listener.
    slider._sync_to_thermostat()
    asyncio.run(slider._restore_and_publish_number())
    return slider


def _heater(low=40.0, high=60.0):
    return SimpleNamespace(state="auto", attributes={"min_temp": low, "max_temp": high})


def test_the_defaults_land_inside_the_thermostats_range():
    away = _slider("away", CONF_TANK_AWAY_TEMPERATURE, DEFAULT_TANK_AWAY_TEMPERATURE, _heater())
    boost = _slider("boost", CONF_TANK_BOOST_TEMPERATURE, DEFAULT_TANK_BOOST_TEMPERATURE, _heater())
    assert (away.native_min_value, away.native_max_value) == (40.0, 60.0)
    assert away.native_value == 40.0  # 30 by default
    assert boost.native_value == 60.0  # 65 by default
    boost._write_to_load_data.assert_called_once_with(60.0)


def test_a_restored_value_is_clamped_into_the_range():
    boost = _slider(
        "boost", CONF_TANK_BOOST_TEMPERATURE, DEFAULT_TANK_BOOST_TEMPERATURE,
        _heater(), restored=75,
    )
    assert boost.native_value == 60.0


def test_without_a_thermostat_reading_the_wide_range_stays():
    boost = _slider(
        "boost", CONF_TANK_BOOST_TEMPERATURE, DEFAULT_TANK_BOOST_TEMPERATURE,
        None, restored=75,
    )
    assert (boost.native_min_value, boost.native_max_value) == (10, 90)
    assert boost.native_value == 75.0


def test_the_slider_follows_the_thermostat_when_its_range_arrives():
    heater = SimpleNamespace(state="unavailable", attributes={})
    boost = _slider(
        "boost", CONF_TANK_BOOST_TEMPERATURE, DEFAULT_TANK_BOOST_TEMPERATURE,
        heater, restored=75,
    )
    assert boost.native_value == 75.0
    boost._write_to_load_data.reset_mock()
    heater.attributes.update(min_temp=40.0, max_temp=60.0)
    boost._thermostat_changed(None)
    assert boost.native_max_value == 60.0
    assert boost.native_value == 60.0
    boost._write_to_load_data.assert_called_once_with(60.0)


def test_an_unchanged_thermostat_writes_nothing():
    boost = _slider(
        "boost", CONF_TANK_BOOST_TEMPERATURE, DEFAULT_TANK_BOOST_TEMPERATURE, _heater(),
    )
    boost.async_write_ha_state.reset_mock()
    boost._write_to_load_data.reset_mock()
    boost._thermostat_changed(None)
    boost.async_write_ha_state.assert_not_called()
    boost._write_to_load_data.assert_not_called()
