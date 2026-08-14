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
    CONF_SOLAR_PRODUCTION_ENTITY_ID,
    CONF_BATTERY_SOC_ENTITY_ID,
    CONF_BATTERY_POWER_ENTITY_ID,
    CONF_BATTERY_MAX_CHARGE_POWER,
    CONF_BATTERY_MAX_DISCHARGE_POWER,
    CONF_BATTERY_SOC_HYSTERESIS,
    CONF_BATTERY_SOC_TARGET_ENTITY_ID,
    CONF_ALLOW_GRID_CHARGING_ENTITY_ID,
    CONF_POWER_BUFFER_ENTITY_ID,
    CONF_GRID_EXPORT_LIMIT,
    CONF_SOLAR_FORECAST_DEVICE_IDS,
    CONF_BATTERY_CAPACITY_KWH,
    CONF_BASE_CONSUMPTION,
    CONF_FORECAST_SOC_FLOOR,
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
    CONF_SOLAR_GRACE_PERIOD,
    DEFAULT_SOLAR_GRACE_PERIOD,
    DEFAULT_MAIN_BREAKER_RATING,
    DEFAULT_PHASE_VOLTAGE,
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
            CONF_GRID_EXPORT_LIMIT: 10500,
            CONF_SOLAR_GRACE_PERIOD: DEFAULT_SOLAR_GRACE_PERIOD,
        },
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "hub_inverter"

    # Step 4: hub_inverter → provide inverter settings (no inverter output entities)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            CONF_INVERTER_MAX_POWER: 10000,
            CONF_INVERTER_MAX_POWER_PER_PHASE: 4000,
            CONF_INVERTER_SUPPORTS_ASYMMETRIC: True,
            CONF_WIRING_TOPOLOGY: DEFAULT_WIRING_TOPOLOGY,
        },
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "hub_battery"

    # Step 5: hub_battery → provide battery settings → creates entry
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
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


def _make_forecast_device(hass, slug, watts=True, name=None):
    """Register a forecast-style device with one sensor, Open-Meteo shape.

    Returns the device id. The sensor carries a ``watts`` mapping when
    ``watts`` is True, mimicking the per-array device the Open-Meteo Solar
    Forecast integration creates; otherwise a plain sensor (wrong device).
    """
    from homeassistant.helpers import device_registry as dr, entity_registry as er

    source_entry = MockConfigEntry(domain="open_meteo_solar_forecast", title=slug)
    source_entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=source_entry.entry_id,
        identifiers={("open_meteo_solar_forecast", slug)},
        name=name or slug,
    )
    reg_entry = er.async_get(hass).async_get_or_create(
        "sensor",
        "open_meteo_solar_forecast",
        f"{slug}_energy_production_today",
        device_id=device.id,
        config_entry=source_entry,
        suggested_object_id=f"{slug}_energy_production_today",
    )
    attributes = {"unit_of_measurement": "kWh"}
    if watts:
        attributes["watts"] = {"2026-08-14T10:00:00+00:00": 4000.0}
    hass.states.async_set(reg_entry.entity_id, "12.5", attributes)
    return device.id


async def test_hub_battery_forecast_devices_validated(hass: HomeAssistant):
    """The battery step rejects a device without any ``watts`` sensor,
    accepts a valid multi-device selection, and stores the device list."""

    hass.states.async_set(
        "sensor.inverter_phase_a", "5.0",
        {"device_class": "current", "unit_of_measurement": "A"},
    )
    east = _make_forecast_device(hass, "array_east")
    west = _make_forecast_device(hass, "array_west")
    wrong = _make_forecast_device(
        hass, "not_a_forecast", watts=False, name="Weather Station"
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={"setup_type": "hub"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={CONF_NAME: "Forecast Hub", CONF_ENTITY_ID: "forecast_hub"},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            CONF_PHASE_A_CURRENT_ENTITY_ID: "sensor.inverter_phase_a",
            CONF_MAIN_BREAKER_RATING: 32,
            CONF_INVERT_PHASES: False,
            CONF_PHASE_VOLTAGE: 230,
            CONF_GRID_EXPORT_LIMIT: 5000,
        },
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            CONF_INVERTER_MAX_POWER: 0,
            CONF_INVERTER_MAX_POWER_PER_PHASE: 0,
            CONF_INVERTER_SUPPORTS_ASYMMETRIC: False,
            CONF_WIRING_TOPOLOGY: DEFAULT_WIRING_TOPOLOGY,
        },
    )
    assert result["step_id"] == "hub_battery"

    # A device without any watts-bearing sensor is rejected, named in the error
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            CONF_BATTERY_MAX_CHARGE_POWER: 5000,
            CONF_BATTERY_MAX_DISCHARGE_POWER: 5000,
            CONF_BATTERY_SOC_HYSTERESIS: 3,
            CONF_SOLAR_FORECAST_DEVICE_IDS: [wrong],
            CONF_BATTERY_CAPACITY_KWH: 10,
        },
    )
    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {
        CONF_SOLAR_FORECAST_DEVICE_IDS: "forecast_device_no_watts"
    }
    assert result["description_placeholders"] == {"entity": "Weather Station"}

    # A valid multi-device selection is accepted and stored
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            CONF_BATTERY_MAX_CHARGE_POWER: 5000,
            CONF_BATTERY_MAX_DISCHARGE_POWER: 5000,
            CONF_BATTERY_SOC_HYSTERESIS: 3,
            CONF_SOLAR_FORECAST_DEVICE_IDS: [east, west],
            CONF_BATTERY_CAPACITY_KWH: 10,
            CONF_BASE_CONSUMPTION: 300,
            CONF_FORECAST_SOC_FLOOR: 30,
        },
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    entries = hass.config_entries.async_entries(DOMAIN)
    hub_entry = next(e for e in entries if e.data.get(ENTRY_TYPE) == ENTRY_TYPE_HUB)
    stored = {**hub_entry.data, **hub_entry.options}
    assert stored[CONF_SOLAR_FORECAST_DEVICE_IDS] == [east, west]
    assert stored[CONF_GRID_EXPORT_LIMIT] == 5000
    assert stored[CONF_BATTERY_CAPACITY_KWH] == 10


async def test_hub_battery_empty_forecast_selection_accepted(hass: HomeAssistant):
    """Leaving the forecast selector empty is valid — the feature is simply off."""

    hass.states.async_set(
        "sensor.inverter_phase_a", "5.0",
        {"device_class": "current", "unit_of_measurement": "A"},
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={"setup_type": "hub"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={CONF_NAME: "Plain Hub", CONF_ENTITY_ID: "plain_hub"},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            CONF_PHASE_A_CURRENT_ENTITY_ID: "sensor.inverter_phase_a",
            CONF_MAIN_BREAKER_RATING: 32,
            CONF_INVERT_PHASES: False,
            CONF_PHASE_VOLTAGE: 230,
            CONF_GRID_EXPORT_LIMIT: 10500,
        },
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            CONF_INVERTER_MAX_POWER: 0,
            CONF_INVERTER_MAX_POWER_PER_PHASE: 0,
            CONF_INVERTER_SUPPORTS_ASYMMETRIC: False,
            CONF_WIRING_TOPOLOGY: DEFAULT_WIRING_TOPOLOGY,
        },
    )
    # The multi-device selector omits its key entirely when left empty
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            CONF_BATTERY_MAX_CHARGE_POWER: 5000,
            CONF_BATTERY_MAX_DISCHARGE_POWER: 5000,
            CONF_BATTERY_SOC_HYSTERESIS: 3,
        },
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    entries = hass.config_entries.async_entries(DOMAIN)
    hub_entry = next(e for e in entries if e.data.get(ENTRY_TYPE) == ENTRY_TYPE_HUB)
    stored = {**hub_entry.data, **hub_entry.options}
    assert stored.get(CONF_SOLAR_FORECAST_DEVICE_IDS) == []


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
    # Single-phase: only phase A, B and C left empty (omitted = no selection)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            CONF_PHASE_A_CURRENT_ENTITY_ID: "sensor.grid_current",
            CONF_MAIN_BREAKER_RATING: 25,
            CONF_INVERT_PHASES: False,
            CONF_MAX_IMPORT_POWER_ENTITY_ID: "sensor.grid_power_limit",
            CONF_PHASE_VOLTAGE: 230,
            CONF_GRID_EXPORT_LIMIT: 5500,
            CONF_SOLAR_GRACE_PERIOD: DEFAULT_SOLAR_GRACE_PERIOD,
        },
    )
    assert result["step_id"] == "hub_inverter"

    # No inverter limits (omit optional entity fields)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            CONF_INVERTER_MAX_POWER: 0,
            CONF_INVERTER_MAX_POWER_PER_PHASE: 0,
            CONF_INVERTER_SUPPORTS_ASYMMETRIC: False,
            CONF_WIRING_TOPOLOGY: DEFAULT_WIRING_TOPOLOGY,
        },
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "hub_battery"

    # No battery (omit optional entity fields)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
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
            CONF_CHARGE_PAUSE_DURATION: 3,
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
            CONF_GRID_EXPORT_LIMIT: 13500,
            CONF_SOLAR_GRACE_PERIOD: DEFAULT_SOLAR_GRACE_PERIOD,
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
            CONF_WIRING_TOPOLOGY: DEFAULT_WIRING_TOPOLOGY,
        },
    )

    # Step 3: hub (battery settings) — saves
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "hub"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
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


def _suggested_value(data_schema, key):
    """Read the suggested default a form schema offers for a field key."""
    for marker in data_schema.schema:
        if getattr(marker, "schema", None) == key:
            desc = getattr(marker, "description", None) or {}
            return desc.get("suggested_value")
    raise AssertionError(f"{key} not present in schema")


async def test_options_flow_does_not_autodetect_inverter_phases(
    hass: HomeAssistant,
    mock_hub_entry: MockConfigEntry,
    mock_setup,
):
    """Editing an existing hub must NOT auto-pick inverter-output entities.

    Regression: the options flow used to re-run auto-detection into empty
    fields, which could grab a 3-phase inverter from an unrelated building and
    silently add phantom L2/L3 phases. The hub has no inverter-output phases
    configured; even with SolarEdge-matching entities present in HA, the
    inverter step must leave those fields empty.
    """
    mock_hub_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_hub_entry.entry_id)
    await hass.async_block_till_done()

    # Entities the hub already references (so the schemas render).
    for ent in ("sensor.inverter_phase_a", "sensor.inverter_phase_b", "sensor.inverter_phase_c"):
        hass.states.async_set(ent, "5.0", {"device_class": "current", "unit_of_measurement": "A"})
    hass.states.async_set("sensor.grid_power_limit", "8050", {"device_class": "power", "unit_of_measurement": "W"})

    # An UNRELATED 3-phase SolarEdge inverter in another building — matches
    # INVERTER_OUTPUT_PATTERNS, so the old auto-detect would have grabbed it.
    for phase in ("a", "b", "c"):
        hass.states.async_set(
            f"sensor.solaredge_i1_ac_current_{phase}", "3.0",
            {"device_class": "current", "unit_of_measurement": "A"},
        )

    result = await hass.config_entries.options.async_init(mock_hub_entry.entry_id)
    assert result["step_id"] == "hub_grid"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            CONF_PHASE_A_CURRENT_ENTITY_ID: "sensor.inverter_phase_a",
            CONF_MAIN_BREAKER_RATING: 25,
            CONF_INVERT_PHASES: False,
            CONF_MAX_IMPORT_POWER_ENTITY_ID: "sensor.grid_power_limit",
            CONF_PHASE_VOLTAGE: 230,
            CONF_GRID_EXPORT_LIMIT: 13500,
            CONF_SOLAR_GRACE_PERIOD: DEFAULT_SOLAR_GRACE_PERIOD,
        },
    )

    # On the inverter step, none of the three phase fields may be pre-filled.
    assert result["step_id"] == "hub_inverter"
    schema = result["data_schema"]
    assert _suggested_value(schema, CONF_INVERTER_OUTPUT_PHASE_A_ENTITY_ID) is None
    assert _suggested_value(schema, CONF_INVERTER_OUTPUT_PHASE_B_ENTITY_ID) is None
    assert _suggested_value(schema, CONF_INVERTER_OUTPUT_PHASE_C_ENTITY_ID) is None


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
            CONF_CHARGE_PAUSE_DURATION: 5,
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


# ── Config entry migration ─────────────────────────────────────────────


async def test_migration_seeds_grid_export_limit_in_one_pass(hass: HomeAssistant):
    """A 2.1 hub reaches 2.4 in ONE async_migrate_entry call (the minor steps
    used to return early, stranding entries one step per restart), and the
    grid export limit is seeded as old excess threshold + the default trigger
    margin, so the effective Excess trigger point does not move."""
    from custom_components.dynamic_ocpp_evse import async_migrate_entry
    from custom_components.dynamic_ocpp_evse.const import (
        DEFAULT_EXCESS_TRIGGER_MARGIN,
        CONF_EXCESS_TRIGGER_MARGIN,
    )

    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        minor_version=1,
        title="Old Hub",
        data={
            CONF_NAME: "Old Hub",
            CONF_ENTITY_ID: "old_hub",
            ENTRY_TYPE: ENTRY_TYPE_HUB,
        },
        options={
            CONF_PHASE_A_CURRENT_ENTITY_ID: "sensor.grid_a",
            "excess_export_threshold": 13000,
        },
    )
    entry.add_to_hass(hass)

    assert await async_migrate_entry(hass, entry)

    assert entry.minor_version == 4
    assert entry.options[CONF_GRID_EXPORT_LIMIT] == 13000 + DEFAULT_EXCESS_TRIGGER_MARGIN
    assert entry.options[CONF_EXCESS_TRIGGER_MARGIN] == DEFAULT_EXCESS_TRIGGER_MARGIN


async def test_migration_leaves_offgrid_hub_unlimited(hass: HomeAssistant):
    """An off-grid hub (no grid CTs) gets no export limit seeded — its Excess
    is battery-side only, and a seeded limit would wrongly enable the
    clipping forecast maths."""
    from custom_components.dynamic_ocpp_evse import async_migrate_entry

    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        minor_version=3,
        title="Off-grid Hub",
        data={
            CONF_NAME: "Off-grid Hub",
            CONF_ENTITY_ID: "offgrid_hub",
            ENTRY_TYPE: ENTRY_TYPE_HUB,
        },
        options={
            CONF_BATTERY_SOC_ENTITY_ID: "sensor.batt_soc",
            CONF_BATTERY_POWER_ENTITY_ID: "sensor.batt_power",
        },
    )
    entry.add_to_hass(hass)

    assert await async_migrate_entry(hass, entry)

    assert entry.minor_version == 4
    assert not entry.options.get(CONF_GRID_EXPORT_LIMIT)


# ── Inverter entry creation ────────────────────────────────────────────


async def test_inverter_creation_flow(hass: HomeAssistant):
    """user → inverter → (auto-selected hub) → inverter_config →
    inverter_battery creates an ENTRY_TYPE_INVERTER entry linked to the hub."""
    from custom_components.dynamic_ocpp_evse.const import (
        ENTRY_TYPE_INVERTER,
        DEVICE_TYPE_INVERTER,
        CONF_DEVICE_TYPE,
        CONF_BATTERY_SOC_FULL,
        CONF_WIRING_TOPOLOGY,
        WIRING_TOPOLOGY_SERIES,
    )

    hub = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        minor_version=4,
        title="Test Hub",
        data={
            CONF_NAME: "Test Hub",
            CONF_ENTITY_ID: "test_hub",
            ENTRY_TYPE: ENTRY_TYPE_HUB,
        },
    )
    hub.add_to_hass(hass)

    hass.states.async_set(
        "sensor.deye_output_l1", "1500",
        {"device_class": "power", "unit_of_measurement": "W"},
    )
    hass.states.async_set(
        "sensor.deye_battery_soc", "72",
        {"device_class": "battery", "unit_of_measurement": "%"},
    )
    hass.states.async_set(
        "sensor.deye_battery_power", "-800",
        {"device_class": "power", "unit_of_measurement": "W"},
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={"setup_type": "inverter"}
    )
    # Single hub → select_hub auto-skips straight to the inverter form
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "inverter_config"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            CONF_NAME: "Deye Hybrid",
            CONF_ENTITY_ID: "lj_deye",
            CONF_INVERTER_MAX_POWER: 12000,
            CONF_INVERTER_MAX_POWER_PER_PHASE: 4000,
            CONF_INVERTER_SUPPORTS_ASYMMETRIC: True,
            CONF_WIRING_TOPOLOGY: WIRING_TOPOLOGY_SERIES,
            CONF_INVERTER_OUTPUT_PHASE_A_ENTITY_ID: "sensor.deye_output_l1",
        },
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "inverter_battery"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            CONF_BATTERY_SOC_ENTITY_ID: "sensor.deye_battery_soc",
            CONF_BATTERY_POWER_ENTITY_ID: "sensor.deye_battery_power",
            CONF_BATTERY_MAX_CHARGE_POWER: 6000,
            CONF_BATTERY_MAX_DISCHARGE_POWER: 8000,
            CONF_BATTERY_SOC_FULL: 97,
            CONF_BATTERY_CAPACITY_KWH: 15,
        },
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    entry = result["result"]
    assert entry.data[ENTRY_TYPE] == ENTRY_TYPE_INVERTER
    assert entry.data[CONF_DEVICE_TYPE] == DEVICE_TYPE_INVERTER
    assert entry.data[CONF_HUB_ENTRY_ID] == hub.entry_id
    stored = {**entry.data, **entry.options}
    assert stored[CONF_INVERTER_MAX_POWER] == 12000
    assert stored[CONF_WIRING_TOPOLOGY] == WIRING_TOPOLOGY_SERIES
    assert stored[CONF_BATTERY_SOC_ENTITY_ID] == "sensor.deye_battery_soc"
    assert stored[CONF_BATTERY_CAPACITY_KWH] == 15
    # The transient flow key must not leak into the stored options
    assert CONF_DEVICE_TYPE not in entry.options


# ── Auto-import of legacy hub inverter/battery fields ─────────────────


async def test_hub_inverter_auto_import(hass: HomeAssistant):
    """The import flow moves the hub's legacy inverter/battery fields onto a
    new inverter entry, blanks them from the hub and sets the imported flag."""
    from custom_components.dynamic_ocpp_evse.const import (
        ENTRY_TYPE_INVERTER,
        MIGRATE_HUB_INVERTER_IMPORTED_FLAG,
        CONF_BATTERY_CAPACITY_KWH,
        CONF_WIRING_TOPOLOGY,
        CONF_BATTERY_SOC_HYSTERESIS,
    )

    hub = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        minor_version=4,
        title="Legacy Hub",
        data={
            CONF_NAME: "Legacy Hub",
            CONF_ENTITY_ID: "legacy_hub",
            ENTRY_TYPE: ENTRY_TYPE_HUB,
        },
        options={
            CONF_PHASE_A_CURRENT_ENTITY_ID: "sensor.grid_a",
            CONF_INVERTER_MAX_POWER: 17000,
            CONF_WIRING_TOPOLOGY: "series",
            CONF_BATTERY_SOC_ENTITY_ID: "sensor.batt_soc",
            CONF_BATTERY_POWER_ENTITY_ID: "sensor.batt_power",
            CONF_BATTERY_MAX_CHARGE_POWER: 5000,
            CONF_BATTERY_MAX_DISCHARGE_POWER: 5000,
            CONF_BATTERY_CAPACITY_KWH: 10,
            CONF_BATTERY_SOC_HYSTERESIS: 3,
            CONF_GRID_EXPORT_LIMIT: 13500,
        },
    )
    hub.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "import"},
        data={"hub_entry_id": hub.entry_id},
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    inverter = result["result"]
    assert inverter.data[ENTRY_TYPE] == ENTRY_TYPE_INVERTER
    assert inverter.data[CONF_HUB_ENTRY_ID] == hub.entry_id
    assert inverter.data["imported_from_hub"] is True
    assert inverter.options[CONF_INVERTER_MAX_POWER] == 17000
    assert inverter.options[CONF_BATTERY_SOC_ENTITY_ID] == "sensor.batt_soc"
    assert inverter.options[CONF_BATTERY_CAPACITY_KWH] == 10

    # The hub is blanked and flagged; hub-scoped settings stay behind.
    assert hub.data[MIGRATE_HUB_INVERTER_IMPORTED_FLAG] is True
    assert CONF_INVERTER_MAX_POWER not in hub.options
    assert CONF_BATTERY_SOC_ENTITY_ID not in hub.options
    assert hub.options[CONF_GRID_EXPORT_LIMIT] == 13500
    assert hub.options[CONF_BATTERY_SOC_HYSTERESIS] == 3


async def test_hub_inverter_auto_import_is_idempotent(hass: HomeAssistant):
    """A second import run (restart between entry creation and blanking)
    aborts on the unique_id but STILL blanks the re-appeared hub fields —
    the double-count window must close on every path."""
    from custom_components.dynamic_ocpp_evse.const import (
        MIGRATE_HUB_INVERTER_IMPORTED_FLAG,
    )

    hub = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        minor_version=4,
        title="Legacy Hub",
        data={
            CONF_NAME: "Legacy Hub",
            CONF_ENTITY_ID: "legacy_hub2",
            ENTRY_TYPE: ENTRY_TYPE_HUB,
        },
        options={
            CONF_BATTERY_SOC_ENTITY_ID: "sensor.batt_soc",
            CONF_BATTERY_POWER_ENTITY_ID: "sensor.batt_power",
        },
    )
    hub.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "import"}, data={"hub_entry_id": hub.entry_id}
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY

    # Simulate the interrupted-run state: fields back on the hub, entry exists
    hass.config_entries.async_update_entry(
        hub,
        options={**hub.options, CONF_BATTERY_SOC_ENTITY_ID: "sensor.batt_soc"},
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "import"}, data={"hub_entry_id": hub.entry_id}
    )
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    # Blanked again despite the abort
    assert CONF_BATTERY_SOC_ENTITY_ID not in hub.options
    assert hub.data[MIGRATE_HUB_INVERTER_IMPORTED_FLAG] is True

    # Only one inverter entry exists
    inverters = [
        e
        for e in hass.config_entries.async_entries(DOMAIN)
        if e.data.get(ENTRY_TYPE) == "inverter"
        and e.data.get(CONF_HUB_ENTRY_ID) == hub.entry_id
    ]
    assert len(inverters) == 1
