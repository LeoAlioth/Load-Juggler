"""Tests for Dynamic OCPP EVSE config flow."""

from unittest.mock import patch, PropertyMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.dynamic_ocpp_evse.const import (
    DOMAIN,
    ENTRY_TYPE,
    ENTRY_TYPE_HUB,
    ENTRY_TYPE_CHARGER,
    CONF_NAME,
    CONF_ENTITY_ID,
    CONF_HUB_ENTRY_ID,
    CONF_CHARGER_PRIORITY,
    CONF_EVSE_MINIMUM_CHARGE_CURRENT,
    CONF_EVSE_MAXIMUM_CHARGE_CURRENT,
    CONF_CHARGE_RATE_UNIT,
    CONF_PROFILE_VALIDITY_MODE,
    CONF_UPDATE_FREQUENCY,
    CONF_OCPP_PROFILE_TIMEOUT,
    CONF_CHARGE_PAUSE_DURATION,
    CONF_STACK_LEVEL,
    DEFAULT_MIN_CHARGE_CURRENT,
    DEFAULT_MAX_CHARGE_CURRENT,
    DEFAULT_UPDATE_FREQUENCY,
    DEFAULT_OCPP_PROFILE_TIMEOUT,
    DEFAULT_CHARGE_PAUSE_DURATION,
    DEFAULT_STACK_LEVEL,
    DEFAULT_CHARGE_RATE_UNIT,
    DEFAULT_PROFILE_VALIDITY_MODE,
)


async def test_user_step_shows_form(hass: HomeAssistant):
    """Test that the user step shows the setup type form."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "user"


async def test_user_step_hub_selected(hass: HomeAssistant):
    """Test selecting hub in user step advances to hub_info."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={"setup_type": "hub"},
    )

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "hub_info"


async def test_hub_info_step(hass: HomeAssistant):
    """Test hub info step collects name and entity_id, advances to hub_grid."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={"setup_type": "hub"},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={CONF_NAME: "My EVSE Hub", CONF_ENTITY_ID: "my_evse"},
    )

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "hub_grid"


async def test_charger_config_validation_min_exceeds_max(
    hass: HomeAssistant,
    mock_hub_entry: MockConfigEntry,
):
    """Test that charger config rejects min_current > max_current."""
    mock_hub_entry.add_to_hass(hass)

    # Use integration_discovery source to go directly to charger_config
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "integration_discovery"},
        data={
            "hub_entry_id": mock_hub_entry.entry_id,
            "charger_id": "test_charger",
            "charger_name": "Test Charger",
            "device_id": "device_1",
            "current_import_entity": "sensor.test_charger_current_import",
            "current_offered_entity": "sensor.test_charger_current_offered",
        },
    )

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "charger_config"

    # Submit with min > max
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            CONF_CHARGER_PRIORITY: 1,
            CONF_EVSE_MINIMUM_CHARGE_CURRENT: 32,
            CONF_EVSE_MAXIMUM_CHARGE_CURRENT: 16,
            CONF_CHARGE_RATE_UNIT: "A",
            CONF_PROFILE_VALIDITY_MODE: DEFAULT_PROFILE_VALIDITY_MODE,
            CONF_UPDATE_FREQUENCY: DEFAULT_UPDATE_FREQUENCY,
            CONF_OCPP_PROFILE_TIMEOUT: DEFAULT_OCPP_PROFILE_TIMEOUT,
            CONF_CHARGE_PAUSE_DURATION: DEFAULT_CHARGE_PAUSE_DURATION,
            CONF_STACK_LEVEL: DEFAULT_STACK_LEVEL,
        },
    )

    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": "min_exceeds_max"}


async def test_charger_config_validation_zero_current(
    hass: HomeAssistant,
    mock_hub_entry: MockConfigEntry,
):
    """Test that charger config rejects zero current values."""
    mock_hub_entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "integration_discovery"},
        data={
            "hub_entry_id": mock_hub_entry.entry_id,
            "charger_id": "test_charger_2",
            "charger_name": "Test Charger 2",
            "device_id": "device_2",
            "current_import_entity": "sensor.test_charger_2_current_import",
            "current_offered_entity": "sensor.test_charger_2_current_offered",
        },
    )

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            CONF_CHARGER_PRIORITY: 1,
            CONF_EVSE_MINIMUM_CHARGE_CURRENT: 0,
            CONF_EVSE_MAXIMUM_CHARGE_CURRENT: 16,
            CONF_CHARGE_RATE_UNIT: "A",
            CONF_PROFILE_VALIDITY_MODE: DEFAULT_PROFILE_VALIDITY_MODE,
            CONF_UPDATE_FREQUENCY: DEFAULT_UPDATE_FREQUENCY,
            CONF_OCPP_PROFILE_TIMEOUT: DEFAULT_OCPP_PROFILE_TIMEOUT,
            CONF_CHARGE_PAUSE_DURATION: DEFAULT_CHARGE_PAUSE_DURATION,
            CONF_STACK_LEVEL: DEFAULT_STACK_LEVEL,
        },
    )

    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_current"}


async def test_charger_config_creates_entry(
    hass: HomeAssistant,
    mock_hub_entry: MockConfigEntry,
):
    """Test that valid charger config creates a config entry."""
    mock_hub_entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "integration_discovery"},
        data={
            "hub_entry_id": mock_hub_entry.entry_id,
            "charger_id": "valid_charger",
            "charger_name": "Valid Charger",
            "device_id": "device_valid",
            "current_import_entity": "sensor.valid_charger_current_import",
            "current_offered_entity": "sensor.valid_charger_current_offered",
        },
    )

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            CONF_CHARGER_PRIORITY: 1,
            CONF_EVSE_MINIMUM_CHARGE_CURRENT: 6,
            CONF_EVSE_MAXIMUM_CHARGE_CURRENT: 16,
            CONF_CHARGE_RATE_UNIT: "A",
            CONF_PROFILE_VALIDITY_MODE: DEFAULT_PROFILE_VALIDITY_MODE,
            CONF_UPDATE_FREQUENCY: DEFAULT_UPDATE_FREQUENCY,
            CONF_OCPP_PROFILE_TIMEOUT: DEFAULT_OCPP_PROFILE_TIMEOUT,
            CONF_CHARGE_PAUSE_DURATION: DEFAULT_CHARGE_PAUSE_DURATION,
            CONF_STACK_LEVEL: DEFAULT_STACK_LEVEL,
        },
    )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["title"] == "Valid Charger Charger"
    assert result["data"][ENTRY_TYPE] == ENTRY_TYPE_CHARGER


async def test_options_flow_hub_shows_form(
    hass: HomeAssistant,
    mock_hub_entry: MockConfigEntry,
    mock_setup,
):
    """Test that options flow for a hub entry shows the hub form."""
    mock_hub_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_hub_entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(mock_hub_entry.entry_id)

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "hub_grid"


async def test_options_flow_charger_shows_form(
    hass: HomeAssistant,
    mock_hub_entry: MockConfigEntry,
    mock_charger_entry: MockConfigEntry,
    mock_setup,
):
    """Test that options flow for a charger entry shows the charger form."""
    mock_hub_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_hub_entry.entry_id)
    await hass.async_block_till_done()

    mock_charger_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_charger_entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(mock_charger_entry.entry_id)

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "charger"
