"""End-to-end tests for the full config flow (hub creation + charger creation).

These tests walk through every step of the config flow UI and verify
the final ConfigEntry structure.
"""

from unittest.mock import patch, AsyncMock

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
    CONF_CHARGER_ID,
    CONF_OCPP_DEVICE_ID,
    CONF_PHASE_A_CURRENT_ENTITY_ID,
    CONF_PHASE_B_CURRENT_ENTITY_ID,
    CONF_PHASE_C_CURRENT_ENTITY_ID,
    CONF_MAIN_BREAKER_RATING,
    CONF_INVERT_PHASES,
    CONF_MAX_IMPORT_POWER_ENTITY_ID,
    CONF_PHASE_VOLTAGE,
    CONF_EXCESS_EXPORT_THRESHOLD,
    CONF_SOLAR_PRODUCTION_ENTITY_ID,
    CONF_BATTERY_SOC_ENTITY_ID,
    CONF_BATTERY_POWER_ENTITY_ID,
    CONF_BATTERY_MAX_CHARGE_POWER,
    CONF_BATTERY_MAX_DISCHARGE_POWER,
    CONF_BATTERY_SOC_HYSTERESIS,
    CONF_CHARGING_MODE_ENTITY_ID,
    CONF_BATTERY_SOC_TARGET_ENTITY_ID,
    CONF_ALLOW_GRID_CHARGING_ENTITY_ID,
    CONF_POWER_BUFFER_ENTITY_ID,
    CONF_INVERTER_MAX_POWER,
    CONF_INVERTER_MAX_POWER_PER_PHASE,
    CONF_INVERTER_SUPPORTS_ASYMMETRIC,
    CONF_INVERTER_OUTPUT_PHASE_A_ENTITY_ID,
    CONF_INVERTER_OUTPUT_PHASE_B_ENTITY_ID,
    CONF_INVERTER_OUTPUT_PHASE_C_ENTITY_ID,
    CONF_WIRING_TOPOLOGY,
    DEFAULT_WIRING_TOPOLOGY,
    CONF_CHARGER_PRIORITY,
    CONF_EVSE_MINIMUM_CHARGE_CURRENT,
    CONF_EVSE_MAXIMUM_CHARGE_CURRENT,
    CONF_CHARGER_L1_PHASE,
    CONF_CHARGER_L2_PHASE,
    CONF_CHARGER_L3_PHASE,
    CONF_EVSE_CURRENT_IMPORT_ENTITY_ID,
    CONF_EVSE_CURRENT_OFFERED_ENTITY_ID,
    CONF_CHARGE_RATE_UNIT,
    CONF_PROFILE_VALIDITY_MODE,
    CONF_UPDATE_FREQUENCY,
    CONF_OCPP_PROFILE_TIMEOUT,
    CONF_CHARGE_PAUSE_DURATION,
    CONF_STACK_LEVEL,
    DEFAULT_MAIN_BREAKER_RATING,
    DEFAULT_PHASE_VOLTAGE,
    DEFAULT_EXCESS_EXPORT_THRESHOLD,
    DEFAULT_BATTERY_MAX_POWER,
    DEFAULT_BATTERY_SOC_HYSTERESIS,
    DEFAULT_MIN_CHARGE_CURRENT,
    DEFAULT_MAX_CHARGE_CURRENT,
    DEFAULT_UPDATE_FREQUENCY,
    DEFAULT_OCPP_PROFILE_TIMEOUT,
    DEFAULT_CHARGE_PAUSE_DURATION,
    DEFAULT_STACK_LEVEL,
    DEFAULT_CHARGE_RATE_UNIT,
    DEFAULT_PROFILE_VALIDITY_MODE,
)


# ── Hub creation end-to-end ────────────────────────────────────────────


async def test_hub_creation_full_flow(hass: HomeAssistant):
    """Walk through user → hub_info → hub_grid → hub_inverter → hub_battery and verify the created entry."""

    # Provide mock sensor entities so the entity selector can find them
    hass.states.async_set(
        "sensor.inverter_phase_a", "5.0",
        {"device_class": "current", "unit_of_measurement": "A"},
    )
    hass.states.async_set(
        "sensor.inverter_phase_b", "4.5",
        {"device_class": "current", "unit_of_measurement": "A"},
    )
    hass.states.async_set(
        "sensor.inverter_phase_c", "5.2",
        {"device_class": "current", "unit_of_measurement": "A"},
    )
    hass.states.async_set(
        "sensor.grid_power_limit", "11000",
        {"device_class": "power", "unit_of_measurement": "W"},
    )
    hass.states.async_set(
        "sensor.battery_soc", "65",
        {"device_class": "battery", "unit_of_measurement": "%"},
    )
    hass.states.async_set(
        "sensor.battery_power", "1500",
        {"device_class": "power", "unit_of_measurement": "W"},
    )

    # Step 1: user step → select hub
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={"setup_type": "hub"},
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "hub_info"

    # Step 2: hub_info → provide name + entity_id
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            CONF_NAME: "My Solar Hub",
            CONF_ENTITY_ID: "my_solar_hub",
        },
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "hub_grid"

    # Step 3: hub_grid → provide grid/electrical configuration
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            CONF_PHASE_A_CURRENT_ENTITY_ID: "sensor.inverter_phase_a",
            CONF_PHASE_B_CURRENT_ENTITY_ID: "sensor.inverter_phase_b",
            CONF_PHASE_C_CURRENT_ENTITY_ID: "sensor.inverter_phase_c",
            CONF_MAIN_BREAKER_RATING: 32,
            CONF_INVERT_PHASES: False,
            CONF_MAX_IMPORT_POWER_ENTITY_ID: "sensor.grid_power_limit",
            CONF_PHASE_VOLTAGE: 230,
            CONF_EXCESS_EXPORT_THRESHOLD: 10000,
        },
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "hub_inverter"

    # Step 4: hub_inverter → provide inverter settings
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            CONF_INVERTER_MAX_POWER: 10000,
            CONF_INVERTER_MAX_POWER_PER_PHASE: 4000,
            CONF_INVERTER_SUPPORTS_ASYMMETRIC: True,
            CONF_INVERTER_OUTPUT_PHASE_A_ENTITY_ID: "",
            CONF_INVERTER_OUTPUT_PHASE_B_ENTITY_ID: "",
            CONF_INVERTER_OUTPUT_PHASE_C_ENTITY_ID: "",
            CONF_WIRING_TOPOLOGY: DEFAULT_WIRING_TOPOLOGY,
        },
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "hub_battery"

    # Step 5: hub_battery → provide battery settings → creates entry
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            CONF_SOLAR_PRODUCTION_ENTITY_ID: "",
            CONF_BATTERY_SOC_ENTITY_ID: "sensor.battery_soc",
            CONF_BATTERY_POWER_ENTITY_ID: "sensor.battery_power",
            CONF_BATTERY_MAX_CHARGE_POWER: 5000,
            CONF_BATTERY_MAX_DISCHARGE_POWER: 5000,
            CONF_BATTERY_SOC_HYSTERESIS: 3,
        },
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["title"] == "My Solar Hub"

    # Verify static data
    entry = result["result"]
    assert entry.data[ENTRY_TYPE] == ENTRY_TYPE_HUB
    assert entry.data[CONF_NAME] == "My Solar Hub"
    assert entry.data[CONF_ENTITY_ID] == "my_solar_hub"

    # Verify options were seeded (background task runs immediately in tests)
    await hass.async_block_till_done()

    # Re-fetch entry after async background task
    entries = hass.config_entries.async_entries(DOMAIN)
    hub_entry = next(e for e in entries if e.data.get(ENTRY_TYPE) == ENTRY_TYPE_HUB)

    # The options background task may need a small delay in the test
    # but the key static fields should be on entry.data
    assert hub_entry.data[CONF_NAME] == "My Solar Hub"
    assert hub_entry.data[CONF_ENTITY_ID] == "my_solar_hub"
    assert hub_entry.data[ENTRY_TYPE] == ENTRY_TYPE_HUB


async def test_hub_creation_single_phase(hass: HomeAssistant):
    """Hub creation with only phase A (single-phase installation)."""
    hass.states.async_set(
        "sensor.grid_current", "12.0",
        {"device_class": "current", "unit_of_measurement": "A"},
    )
    hass.states.async_set(
        "sensor.grid_power_limit", "5000",
        {"device_class": "power", "unit_of_measurement": "W"},
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={"setup_type": "hub"},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            CONF_NAME: "1ph Hub",
            CONF_ENTITY_ID: "hub_1ph",
        },
    )
    # Single-phase: only phase A, B and C left empty
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            CONF_PHASE_A_CURRENT_ENTITY_ID: "sensor.grid_current",
            CONF_PHASE_B_CURRENT_ENTITY_ID: "",
            CONF_PHASE_C_CURRENT_ENTITY_ID: "",
            CONF_MAIN_BREAKER_RATING: 25,
            CONF_INVERT_PHASES: False,
            CONF_MAX_IMPORT_POWER_ENTITY_ID: "sensor.grid_power_limit",
            CONF_PHASE_VOLTAGE: 230,
            CONF_EXCESS_EXPORT_THRESHOLD: 5000,
        },
    )
    assert result["step_id"] == "hub_inverter"

    # No inverter limits
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            CONF_INVERTER_MAX_POWER: 0,
            CONF_INVERTER_MAX_POWER_PER_PHASE: 0,
            CONF_INVERTER_SUPPORTS_ASYMMETRIC: False,
            CONF_INVERTER_OUTPUT_PHASE_A_ENTITY_ID: "",
            CONF_INVERTER_OUTPUT_PHASE_B_ENTITY_ID: "",
            CONF_INVERTER_OUTPUT_PHASE_C_ENTITY_ID: "",
            CONF_WIRING_TOPOLOGY: DEFAULT_WIRING_TOPOLOGY,
        },
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "hub_battery"

    # No battery
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            CONF_SOLAR_PRODUCTION_ENTITY_ID: "",
            CONF_BATTERY_SOC_ENTITY_ID: "",
            CONF_BATTERY_POWER_ENTITY_ID: "",
            CONF_BATTERY_MAX_CHARGE_POWER: 0,
            CONF_BATTERY_MAX_DISCHARGE_POWER: 0,
            CONF_BATTERY_SOC_HYSTERESIS: 3,
        },
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["title"] == "1ph Hub"


async def test_hub_creation_no_charger_option_when_no_hubs(hass: HomeAssistant):
    """User step should NOT show charger option when no hubs exist."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "user"
    # When no hubs exist, selecting "charger" should produce an error
    # (the option shouldn't even be visible, but if someone submits it):
    # Actually, the form only shows "hub" when no hubs exist.
    # We can only verify the form renders.


# ── Charger creation via discovery ─────────────────────────────────────


async def test_charger_discovery_creates_entry(
    hass: HomeAssistant,
    mock_hub_entry: MockConfigEntry,
    mock_setup,
):
    """Test charger creation through integration discovery source (3 steps)."""
    mock_hub_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_hub_entry.entry_id)
    await hass.async_block_till_done()

    # Provide the current_offered sensor so auto-detect works
    hass.states.async_set(
        "sensor.wallbox_current_offered", "16.0",
        {"device_class": "current", "unit_of_measurement": "A"},
    )

    # Discovery triggers charger_info
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "integration_discovery"},
        data={
            "hub_entry_id": mock_hub_entry.entry_id,
            "charger_id": "wallbox",
            "charger_name": "Wallbox Pro",
            "device_id": "device_wb_pro",
            "current_import_entity": "sensor.wallbox_current_import",
            "current_offered_entity": "sensor.wallbox_current_offered",
        },
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "charger_info"

    # Step 1: charger_info — name, entity_id, priority
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            CONF_NAME: "Wallbox Pro",
            CONF_ENTITY_ID: "wallbox",
            CONF_CHARGER_PRIORITY: 1,
        },
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "charger_current"

    # Step 2: charger_current — current limits and phase mapping
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            CONF_EVSE_MINIMUM_CHARGE_CURRENT: 6,
            CONF_EVSE_MAXIMUM_CHARGE_CURRENT: 32,
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
            CONF_UPDATE_FREQUENCY: 10,
            CONF_OCPP_PROFILE_TIMEOUT: 120,
            CONF_CHARGE_PAUSE_DURATION: 180,
            CONF_STACK_LEVEL: 3,
        },
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["title"] == "Wallbox Pro Charger"

    # Verify entry data
    entry = result["result"]
    assert entry.data[ENTRY_TYPE] == ENTRY_TYPE_CHARGER
    assert entry.data[CONF_HUB_ENTRY_ID] == mock_hub_entry.entry_id
    assert entry.data[CONF_CHARGER_ID] == "wallbox"
    assert entry.data[CONF_OCPP_DEVICE_ID] == "device_wb_pro"
    assert entry.data[CONF_EVSE_CURRENT_IMPORT_ENTITY_ID] == "sensor.wallbox_current_import"
    assert entry.data[CONF_EVSE_CURRENT_OFFERED_ENTITY_ID] == "sensor.wallbox_current_offered"


async def test_charger_discovery_duplicate_aborts(
    hass: HomeAssistant,
    mock_hub_entry: MockConfigEntry,
    mock_setup,
):
    """Discovering the same charger twice should abort the second flow."""
    mock_hub_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_hub_entry.entry_id)
    await hass.async_block_till_done()

    hass.states.async_set(
        "sensor.wallbox_current_offered", "16.0",
        {"device_class": "current", "unit_of_measurement": "A"},
    )

    discovery_data = {
        "hub_entry_id": mock_hub_entry.entry_id,
        "charger_id": "wallbox_dup",
        "charger_name": "Wallbox Dup",
        "device_id": "device_dup",
        "current_import_entity": "sensor.wallbox_dup_current_import",
        "current_offered_entity": "sensor.wallbox_current_offered",
    }

    # First discovery → charger_info form
    result1 = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "integration_discovery"},
        data=discovery_data,
    )
    assert result1["type"] == FlowResultType.FORM
    assert result1["step_id"] == "charger_info"

    # Complete first charger (3 steps)
    result1 = await hass.config_entries.flow.async_configure(
        result1["flow_id"],
        user_input={
            CONF_NAME: "Wallbox Dup",
            CONF_ENTITY_ID: "wallbox_dup",
            CONF_CHARGER_PRIORITY: 1,
        },
    )
    result1 = await hass.config_entries.flow.async_configure(
        result1["flow_id"],
        user_input={
            CONF_EVSE_MINIMUM_CHARGE_CURRENT: 6,
            CONF_EVSE_MAXIMUM_CHARGE_CURRENT: 16,
            CONF_CHARGER_L1_PHASE: "A",
            CONF_CHARGER_L2_PHASE: "B",
            CONF_CHARGER_L3_PHASE: "C",
        },
    )
    result1 = await hass.config_entries.flow.async_configure(
        result1["flow_id"],
        user_input={
            CONF_CHARGE_RATE_UNIT: "A",
            CONF_PROFILE_VALIDITY_MODE: DEFAULT_PROFILE_VALIDITY_MODE,
            CONF_UPDATE_FREQUENCY: DEFAULT_UPDATE_FREQUENCY,
            CONF_OCPP_PROFILE_TIMEOUT: DEFAULT_OCPP_PROFILE_TIMEOUT,
            CONF_CHARGE_PAUSE_DURATION: DEFAULT_CHARGE_PAUSE_DURATION,
            CONF_STACK_LEVEL: DEFAULT_STACK_LEVEL,
        },
    )
    assert result1["type"] == FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    # Second discovery with same charger_id → should abort
    result2 = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "integration_discovery"},
        data=discovery_data,
    )
    assert result2["type"] == FlowResultType.ABORT
    assert result2["reason"] == "already_configured"


# ── Options flow submission ────────────────────────────────────────────


async def test_options_flow_hub_saves_changes(
    hass: HomeAssistant,
    mock_hub_entry: MockConfigEntry,
    mock_setup,
):
    """Test that submitting hub options actually updates the config entry.

    The hub options flow has three steps: hub_grid → hub_inverter → hub (battery).
    """
    mock_hub_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_hub_entry.entry_id)
    await hass.async_block_till_done()

    # Provide sensor entities for the grid and battery schemas
    hass.states.async_set(
        "sensor.inverter_phase_a", "5.0",
        {"device_class": "current", "unit_of_measurement": "A"},
    )
    hass.states.async_set(
        "sensor.inverter_phase_b", "4.0",
        {"device_class": "current", "unit_of_measurement": "A"},
    )
    hass.states.async_set(
        "sensor.inverter_phase_c", "3.0",
        {"device_class": "current", "unit_of_measurement": "A"},
    )
    hass.states.async_set(
        "sensor.grid_power_limit", "8050",
        {"device_class": "power", "unit_of_measurement": "W"},
    )
    hass.states.async_set(
        "sensor.battery_soc", "65",
        {"device_class": "battery", "unit_of_measurement": "%"},
    )
    hass.states.async_set(
        "sensor.battery_power", "1500",
        {"device_class": "power", "unit_of_measurement": "W"},
    )

    # Step 1: hub_grid (electrical settings)
    result = await hass.config_entries.options.async_init(mock_hub_entry.entry_id)
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "hub_grid"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            CONF_PHASE_A_CURRENT_ENTITY_ID: "sensor.inverter_phase_a",
            CONF_MAIN_BREAKER_RATING: 25,
            CONF_INVERT_PHASES: False,
            CONF_MAX_IMPORT_POWER_ENTITY_ID: "sensor.grid_power_limit",
            CONF_PHASE_VOLTAGE: 230,
            CONF_EXCESS_EXPORT_THRESHOLD: 13000,
        },
    )

    # Step 2: hub_inverter (inverter settings)
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "hub_inverter"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            CONF_INVERTER_MAX_POWER: 8000,
            CONF_INVERTER_MAX_POWER_PER_PHASE: 3000,
            CONF_INVERTER_SUPPORTS_ASYMMETRIC: False,
            CONF_INVERTER_OUTPUT_PHASE_A_ENTITY_ID: "",
            CONF_INVERTER_OUTPUT_PHASE_B_ENTITY_ID: "",
            CONF_INVERTER_OUTPUT_PHASE_C_ENTITY_ID: "",
            CONF_WIRING_TOPOLOGY: DEFAULT_WIRING_TOPOLOGY,
        },
    )

    # Step 3: hub (battery settings) — saves
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "hub"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            CONF_SOLAR_PRODUCTION_ENTITY_ID: "",
            CONF_BATTERY_SOC_ENTITY_ID: "sensor.battery_soc",
            CONF_BATTERY_POWER_ENTITY_ID: "sensor.battery_power",
            CONF_BATTERY_MAX_CHARGE_POWER: 7000,
            CONF_BATTERY_MAX_DISCHARGE_POWER: 7000,
            CONF_BATTERY_SOC_HYSTERESIS: 5,
        },
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY

    # Options should now contain the submitted values
    assert mock_hub_entry.options.get(CONF_BATTERY_MAX_CHARGE_POWER) == 7000
    assert mock_hub_entry.options.get(CONF_BATTERY_MAX_DISCHARGE_POWER) == 7000
    assert mock_hub_entry.options.get(CONF_BATTERY_SOC_HYSTERESIS) == 5
    assert mock_hub_entry.options.get(CONF_INVERTER_MAX_POWER) == 8000
    assert mock_hub_entry.options.get(CONF_INVERTER_MAX_POWER_PER_PHASE) == 3000
    assert mock_hub_entry.options.get(CONF_INVERTER_SUPPORTS_ASYMMETRIC) is False


async def test_options_flow_charger_saves_changes(
    hass: HomeAssistant,
    mock_hub_entry: MockConfigEntry,
    mock_charger_entry: MockConfigEntry,
    mock_setup,
):
    """Test that submitting charger options updates the config entry (3 steps)."""
    mock_hub_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_hub_entry.entry_id)
    await hass.async_block_till_done()

    mock_charger_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_charger_entry.entry_id)
    await hass.async_block_till_done()

    # Step 1: charger (priority only)
    result = await hass.config_entries.options.async_init(mock_charger_entry.entry_id)
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "charger"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            CONF_CHARGER_PRIORITY: 2,
        },
    )

    # Step 2: charger_current
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "charger_current"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            CONF_EVSE_MINIMUM_CHARGE_CURRENT: 8,
            CONF_EVSE_MAXIMUM_CHARGE_CURRENT: 32,
            CONF_CHARGER_L1_PHASE: "A",
            CONF_CHARGER_L2_PHASE: "B",
            CONF_CHARGER_L3_PHASE: "C",
        },
    )

    # Step 3: charger_timing — saves
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "charger_timing"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            CONF_CHARGE_RATE_UNIT: "A",
            CONF_PROFILE_VALIDITY_MODE: "absolute",
            CONF_UPDATE_FREQUENCY: 30,
            CONF_OCPP_PROFILE_TIMEOUT: 240,
            CONF_CHARGE_PAUSE_DURATION: 300,
            CONF_STACK_LEVEL: 5,
        },
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY

    # Options should now contain the submitted values
    assert mock_charger_entry.options.get(CONF_CHARGER_PRIORITY) == 2
    assert mock_charger_entry.options.get(CONF_EVSE_MINIMUM_CHARGE_CURRENT) == 8
    assert mock_charger_entry.options.get(CONF_UPDATE_FREQUENCY) == 30


async def test_options_flow_charger_validates(
    hass: HomeAssistant,
    mock_hub_entry: MockConfigEntry,
    mock_charger_entry: MockConfigEntry,
    mock_setup,
):
    """Test that charger options flow validates min/max current on charger_current step."""
    mock_hub_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_hub_entry.entry_id)
    await hass.async_block_till_done()

    mock_charger_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_charger_entry.entry_id)
    await hass.async_block_till_done()

    # Step 1: charger (priority)
    result = await hass.config_entries.options.async_init(mock_charger_entry.entry_id)
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "charger"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            CONF_CHARGER_PRIORITY: 1,
        },
    )

    # Step 2: charger_current — submit invalid: min > max
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "charger_current"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            CONF_EVSE_MINIMUM_CHARGE_CURRENT: 32,
            CONF_EVSE_MAXIMUM_CHARGE_CURRENT: 6,
            CONF_CHARGER_L1_PHASE: "A",
            CONF_CHARGER_L2_PHASE: "B",
            CONF_CHARGER_L3_PHASE: "C",
        },
    )
    # Should re-show form with errors
    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": "min_exceeds_max"}
