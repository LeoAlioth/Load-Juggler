"""A load slider follows a setting changed on the load's settings page.

Machine-authored tests - not yet human-reviewed.

Every load slider is seeded from its configured value, and the restored state
used to win over it for ever: a tank's away / normal / boost temperatures
entered on the settings page never reached the sliders (reported 2026-09-25).
The seed is now saved beside the state, and a changed seed means a changed
setting, which wins. Needs Home Assistant (Docker tier).
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from homeassistant.helpers.restore_state import RestoredExtraData

from custom_components.dynamic_ocpp_evse.const import (
    CONF_TANK_NORMAL_TEMPERATURE,
    DEFAULT_TANK_NORMAL_TEMPERATURE,
)
from custom_components.dynamic_ocpp_evse.number import TankTemperatureSlider


def _slider(configured, last_value, last_seed):
    entry = SimpleNamespace(
        entry_id="tank", data={}, options={CONF_TANK_NORMAL_TEMPERATURE: configured}
    )
    slider = TankTemperatureSlider(
        MagicMock(), entry, "Tank", "tank", "normal",
        CONF_TANK_NORMAL_TEMPERATURE, DEFAULT_TANK_NORMAL_TEMPERATURE, "Normal",
    )
    slider.async_get_last_state = AsyncMock(
        return_value=None if last_value is None else SimpleNamespace(state=str(last_value))
    )
    slider.async_get_last_extra_data = AsyncMock(
        return_value=None if last_seed is None else RestoredExtraData({"seed": last_seed})
    )
    slider.async_write_ha_state = MagicMock()
    slider._write_to_load_data = MagicMock()
    asyncio.run(slider._restore_and_publish_number())
    return slider


def test_a_setting_changed_since_the_last_save_wins():
    # Saved at 45 while the setting was 45; the setting is now 42.
    slider = _slider(configured=42, last_value=45, last_seed=45)
    assert slider.native_value == 42
    slider._write_to_load_data.assert_called_once_with(42)


def test_a_slider_moved_by_hand_keeps_its_value():
    # Moved to 50 while the setting stayed 45.
    assert _slider(configured=45, last_value=50, last_seed=45).native_value == 50


def test_a_state_saved_before_seeds_existed_restores_as_before():
    assert _slider(configured=42, last_value=50, last_seed=None).native_value == 50


def test_a_new_slider_starts_from_the_setting():
    assert _slider(configured=42, last_value=None, last_seed=None).native_value == 42


def test_the_seed_is_saved_with_the_state():
    slider = _slider(configured=42, last_value=45, last_seed=45)
    assert slider.extra_restore_state_data.as_dict() == {"seed": 42}
