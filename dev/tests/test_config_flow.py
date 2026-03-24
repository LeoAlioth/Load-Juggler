"""Tests for Dynamic OCPP EVSE config flow."""

from unittest.mock import patch, PropertyMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.entity_registry import RegistryEntry
from pytest_homeassistant_custom_component.common import MockConfigEntry, MockEntity

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
    CONF_CHARGER_L1_PHASE,
    CONF_CHARGER_L2_PHASE,
    CONF_CHARGER_L3_PHASE,
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


async def test_charger_current_validation_min_exceeds_max(
    hass: HomeAssistant,
    mock_hub_entry: MockConfigEntry,
):
    """Test that charger_current step rejects min_current > max_current."""
    mock_hub_entry.add_to_hass(hass)

    # Discovery lands on charger_info
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
    assert result["step_id"] == "charger_info"

    # Step 1: charger_info — submit name/id/priority
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            CONF_NAME: "Test Charger",
            CONF_ENTITY_ID: "test_charger",
            CONF_CHARGER_PRIORITY: 1,
        },
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "charger_current"

    # Step 2: charger_current — submit with min > max
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            CONF_EVSE_MINIMUM_CHARGE_CURRENT: 32,
            CONF_EVSE_MAXIMUM_CHARGE_CURRENT: 16,
            CONF_CHARGER_L1_PHASE: "A",
            CONF_CHARGER_L2_PHASE: "B",
            CONF_CHARGER_L3_PHASE: "C",
        },
    )

    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": "min_exceeds_max"}


async def test_charger_current_validation_min_exceeds_max(
    hass: HomeAssistant,
    mock_hub_entry: MockConfigEntry,
):
    """Test that charger_current step rejects min > max current values."""
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

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "charger_info"

    # Step 1: charger_info
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            CONF_NAME: "Test Charger 2",
            CONF_ENTITY_ID: "test_charger_2",
            CONF_CHARGER_PRIORITY: 1,
        },
    )

    # Step 2: charger_current — submit with min > max
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            CONF_EVSE_MINIMUM_CHARGE_CURRENT: 20,
            CONF_EVSE_MAXIMUM_CHARGE_CURRENT: 6,
            CONF_CHARGER_L1_PHASE: "A",
            CONF_CHARGER_L2_PHASE: "B",
            CONF_CHARGER_L3_PHASE: "C",
        },
    )

    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": "min_exceeds_max"}


async def test_charger_config_creates_entry(
    hass: HomeAssistant,
    mock_hub_entry: MockConfigEntry,
):
    """Test that valid charger config creates a config entry via 3 steps."""
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

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "charger_info"

    # Step 1: charger_info
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            CONF_NAME: "Valid Charger",
            CONF_ENTITY_ID: "valid_charger",
            CONF_CHARGER_PRIORITY: 1,
        },
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "charger_current"

    # Step 2: charger_current
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            CONF_EVSE_MINIMUM_CHARGE_CURRENT: 6,
            CONF_EVSE_MAXIMUM_CHARGE_CURRENT: 16,
            CONF_CHARGER_L1_PHASE: "A",
            CONF_CHARGER_L2_PHASE: "B",
            CONF_CHARGER_L3_PHASE: "C",
        },
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "charger_timing"

    # Step 3: charger_timing — creates entry
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
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


async def test_hub_grid_with_entities_without_device_class(
    hass: HomeAssistant,
    mock_setup,
):
    """Test hub_grid step with entities that have unit_of_measurement but no device_class.

    This tests that the config flow works correctly when sensors don't have
    device_class set, using unit_of_measurement for filtering instead.
    """
    # Create mock entities in the registry without device_class
    from homeassistant.helpers.entity_registry import async_get as _async_get_er
    entity_registry = _async_get_er(hass)

    # Grid current sensors with unit A but no device_class
    entity_registry.async_get_or_create(
        "sensor",
        "test",
        "grid_phase_a",
        suggested_object_id="grid_phase_a",
    )
    entity_registry.async_get_or_create(
        "sensor",
        "test",
        "grid_phase_b",
        suggested_object_id="grid_phase_b",
    )
    entity_registry.async_get_or_create(
        "sensor",
        "test",
        "grid_phase_c",
        suggested_object_id="grid_phase_c",
    )

    # Start config flow
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={"setup_type": "hub"},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={CONF_NAME: "Test Hub", CONF_ENTITY_ID: "test_hub"},
    )

    # Should show hub_grid step
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "hub_grid"

    # Submit grid config with entities that don't have device_class
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            "phase_a_current_entity_id": "sensor.grid_phase_a",
            "phase_b_current_entity_id": "sensor.grid_phase_b",
            "phase_c_current_entity_id": "sensor.grid_phase_c",
            "main_breaker_rating": 25,
            "invert_phases": False,
            "enable_max_import_power": True,
            "phase_voltage": 230,
            "excess_export_threshold": 13000,
            "auto_detect_phase_mapping": True,
            "solar_grace_period": 5,
        },
    )

    # Should advance to hub_inverter
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "hub_inverter"


async def test_hub_battery_with_soc_sensor_without_device_class(
    hass: HomeAssistant,
    mock_setup,
):
    """Test hub_battery step with SOC sensor that has unit % but no device_class.

    This tests that battery SOC sensors without device_class='battery' work
    correctly using unit_of_measurement='%' for filtering.
    """
    # Create mock entities in the registry without device_class
    from homeassistant.helpers.entity_registry import async_get as _async_get_er
    entity_registry = _async_get_er(hass)

    # Grid current sensors
    entity_registry.async_get_or_create(
        "sensor",
        "test",
        "grid_a",
        suggested_object_id="grid_a",
    )

    # Battery SOC sensor with % unit but no device_class
    entity_registry.async_get_or_create(
        "sensor",
        "test",
        "battery_soc",
        suggested_object_id="battery_soc",
    )

    # Battery power sensor
    entity_registry.async_get_or_create(
        "sensor",
        "test",
        "battery_power",
        suggested_object_id="battery_power",
    )

    # Solar production sensor
    entity_registry.async_get_or_create(
        "sensor",
        "test",
        "solar_power",
        suggested_object_id="solar_power",
    )

    # Start config flow and go through all hub steps
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={"setup_type": "hub"},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={CONF_NAME: "Test Hub", CONF_ENTITY_ID: "test_hub"},
    )

    # hub_grid step - only phase A, leave B/C as optional (not submitted)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            "phase_a_current_entity_id": "sensor.grid_a",
            "main_breaker_rating": 25,
            "invert_phases": False,
            "enable_max_import_power": True,
            "phase_voltage": 230,
            "excess_export_threshold": 13000,
            "auto_detect_phase_mapping": True,
            "solar_grace_period": 5,
        },
    )

    # hub_inverter step - just proceed, optional entity fields omitted
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            "inverter_max_power": 0,
            "inverter_max_power_per_phase": 0,
            "inverter_supports_asymmetric": False,
            "wiring_topology": "parallel",
        },
    )

    # Should show hub_battery step
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "hub_battery"

    # Submit battery config with SOC sensor that doesn't have device_class
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            "solar_production_entity_id": "sensor.solar_power",
            "battery_soc_entity_id": "sensor.battery_soc",
            "battery_power_entity_id": "sensor.battery_power",
            "battery_max_charge_power": 5000,
            "battery_max_discharge_power": 5000,
            "battery_soc_hysteresis": 3,
        },
    )

    # Should create entry
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["title"] == "Test Hub"


async def test_power_sensors_with_watts_unit_without_device_class(
    hass: HomeAssistant,
    mock_setup,
):
    """Test that power sensors with W/kW units work without device_class.

    Tests solar production and battery power sensors that use unit_of_measurement
    for filtering rather than device_class.
    """
    from homeassistant.helpers.entity_registry import async_get as _async_get_er
    entity_registry = _async_get_er(hass)

    # Grid sensors with A unit
    entity_registry.async_get_or_create(
        "sensor",
        "test",
        "grid_a",
        suggested_object_id="grid_a",
    )

    # Power sensors with W unit but no device_class
    entity_registry.async_get_or_create(
        "sensor",
        "test",
        "solar",
        suggested_object_id="solar_production",
    )
    entity_registry.async_get_or_create(
        "sensor",
        "test",
        "battery",
        suggested_object_id="battery_power",
    )

    # Go through config flow
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={"setup_type": "hub"},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={CONF_NAME: "Test Hub", CONF_ENTITY_ID: "test_hub"},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            "phase_a_current_entity_id": "sensor.grid_a",
            "main_breaker_rating": 25,
            "invert_phases": False,
            "enable_max_import_power": True,
            "phase_voltage": 230,
            "excess_export_threshold": 13000,
            "auto_detect_phase_mapping": True,
            "solar_grace_period": 5,
        },
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            "inverter_max_power": 0,
            "inverter_max_power_per_phase": 0,
            "inverter_supports_asymmetric": False,
            "wiring_topology": "parallel",
        },
    )

    # hub_battery step - use sensors without device_class, battery_soc_entity_id omitted (optional)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            "solar_production_entity_id": "sensor.solar_production",
            "battery_power_entity_id": "sensor.battery_power",
            "battery_max_charge_power": 5000,
            "battery_max_discharge_power": 5000,
            "battery_soc_hysteresis": 3,
        },
    )

    assert result["type"] == FlowResultType.CREATE_ENTRY
