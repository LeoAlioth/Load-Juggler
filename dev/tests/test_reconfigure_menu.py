"""Tests for the menu-based reconfigure flow.

Reconfiguring a multi-section device (hub, EVSE) opens a menu; picking a
section shows its page, submitting saves it and returns to the menu, and "Done"
closes. Single-page devices (plug, tank) go straight to their form.
"""

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import SOURCE_RECONFIGURE
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.dynamic_ocpp_evse.const import (
    DOMAIN,
    ENTRY_TYPE,
    ENTRY_TYPE_HUB,
    ENTRY_TYPE_CHARGER,
    CONF_NAME,
    CONF_ENTITY_ID,
    CONF_DEVICE_TYPE,
    DEVICE_TYPE_PLUG,
    DEVICE_TYPE_EVSE,
    CONF_HUB_ENTRY_ID,
    CONF_CHARGER_PRIORITY,
    CONF_PRIORITY_ORDER,
    CONF_MAIN_BREAKER_RATING,
    CONF_PHASE_VOLTAGE,
    CONF_INVERT_PHASES,
    CONF_GRID_EXPORT_LIMIT,
    CONF_SOLAR_GRACE_PERIOD,
    DEFAULT_SOLAR_GRACE_PERIOD,
)
from custom_components.dynamic_ocpp_evse.helpers import get_entry_value


def _start(hass, entry):
    return hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_RECONFIGURE, "entry_id": entry.entry_id}
    )


def _plug(hub, name="Pond Pump", priority=1):
    return MockConfigEntry(
        domain=DOMAIN,
        title=name,
        data={
            CONF_NAME: name,
            CONF_ENTITY_ID: name.lower().replace(" ", "_"),
            ENTRY_TYPE: ENTRY_TYPE_CHARGER,
            CONF_DEVICE_TYPE: DEVICE_TYPE_PLUG,
            CONF_HUB_ENTRY_ID: hub.entry_id,
        },
        options={CONF_CHARGER_PRIORITY: priority},
    )


async def test_reconfigure_hub_shows_menu(hass: HomeAssistant, mock_hub_entry):
    mock_hub_entry.add_to_hass(hass)

    result = await _start(hass, mock_hub_entry)

    assert result["type"] == FlowResultType.MENU
    assert result["step_id"] == "reconfigure"
    for option in (
        "reconfigure_hub_grid",
        "reconfigure_hub_inverter",
        "reconfigure_hub_battery",
        "reconfigure_priority",
        "reconfigure_finish",
    ):
        assert option in result["menu_options"]


async def test_reconfigure_hub_hides_inverter_page_once_imported(
    hass: HomeAssistant, mock_hub_entry
):
    """Once the hub's legacy inverter/battery fields have been imported onto an
    Inverter entry, the hub's own inverter page disappears — that hardware is
    edited on the inverter entry from then on."""
    from custom_components.dynamic_ocpp_evse.const import (
        MIGRATE_HUB_INVERTER_IMPORTED_FLAG,
    )

    mock_hub_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_hub_entry,
        data={**mock_hub_entry.data, MIGRATE_HUB_INVERTER_IMPORTED_FLAG: True},
    )

    result = await _start(hass, mock_hub_entry)

    assert result["type"] == FlowResultType.MENU
    assert "reconfigure_hub_inverter" not in result["menu_options"]
    # The hub's own pages remain for grid and the hub-scoped battery settings
    assert "reconfigure_hub_grid" in result["menu_options"]
    assert "reconfigure_hub_battery" in result["menu_options"]


async def test_reconfigure_finish_closes(hass: HomeAssistant, mock_hub_entry):
    mock_hub_entry.add_to_hass(hass)

    result = await _start(hass, mock_hub_entry)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "reconfigure_finish"}
    )

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"


async def test_reconfigure_grid_saves_and_returns_to_menu(
    hass: HomeAssistant, mock_hub_entry
):
    mock_hub_entry.add_to_hass(hass)

    result = await _start(hass, mock_hub_entry)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "reconfigure_hub_grid"}
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "reconfigure_hub_grid"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            CONF_MAIN_BREAKER_RATING: 40,
            CONF_INVERT_PHASES: False,
            CONF_PHASE_VOLTAGE: 230,
            CONF_GRID_EXPORT_LIMIT: 10500,
            CONF_SOLAR_GRACE_PERIOD: DEFAULT_SOLAR_GRACE_PERIOD,
        },
    )

    # Saving a section drops back to the menu, not closes.
    assert result["type"] == FlowResultType.MENU
    assert result["step_id"] == "reconfigure"
    # The change was persisted to options immediately.
    assert mock_hub_entry.options[CONF_MAIN_BREAKER_RATING] == 40


async def test_reconfigure_priority_reorders_devices(
    hass: HomeAssistant, mock_hub_entry
):
    mock_hub_entry.add_to_hass(hass)
    first = _plug(mock_hub_entry, "Pond Pump", priority=1)
    second = _plug(mock_hub_entry, "Power Strip", priority=2)
    first.add_to_hass(hass)
    second.add_to_hass(hass)

    result = await _start(hass, mock_hub_entry)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "reconfigure_priority"}
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "reconfigure_priority"

    # Reverse the order: second device first.
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={CONF_PRIORITY_ORDER: [second.entry_id, first.entry_id]},
    )

    assert result["type"] == FlowResultType.MENU
    assert get_entry_value(second, CONF_CHARGER_PRIORITY, None) == 1
    assert get_entry_value(first, CONF_CHARGER_PRIORITY, None) == 2


async def test_reconfigure_evse_shows_menu(hass: HomeAssistant, mock_charger_entry):
    mock_charger_entry.add_to_hass(hass)

    result = await _start(hass, mock_charger_entry)

    assert result["type"] == FlowResultType.MENU
    for option in (
        "reconfigure_charger",
        "reconfigure_charger_current",
        "reconfigure_charger_timing",
        "reconfigure_finish",
    ):
        assert option in result["menu_options"]


async def test_reconfigure_plug_goes_straight_to_form(
    hass: HomeAssistant, mock_hub_entry
):
    mock_hub_entry.add_to_hass(hass)
    plug = _plug(mock_hub_entry)
    plug.add_to_hass(hass)

    result = await _start(hass, plug)

    # Single-page device: no menu, straight to the form.
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "reconfigure_plug"
