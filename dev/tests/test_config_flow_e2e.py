"""End-to-end tests for the full config flow (hub creation + charger creation).

These tests walk through every step of the config flow UI and verify
the final ConfigEntry structure.
"""

from unittest.mock import patch, AsyncMock

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.dynamic_ocpp_evse.const import (
    DOMAIN,
    ENTRY_TYPE,
    ENTRY_TYPE_HUB,
    ENTRY_TYPE_LOAD,
    CONF_NAME,
    CONF_ENTITY_ID,
    CONF_HUB_ENTRY_ID,
    CONF_CHARGER_ID,
    CONF_OCPP_DEVICE_ID,
    CONF_DEVICE_TYPE,
    DEVICE_TYPE_PLUG,
    CONF_PRIORITY_ORDER,
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
    CONF_LOAD_PRIORITY,
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
    CONF_INVERTER_FEATURES,
    ENTRY_TYPE_INVERTER,
)
from custom_components.dynamic_ocpp_evse.helpers import get_entry_value


def _plug_entry(hub, name, priority=1):
    """A smart-plug entry linked to a hub (a load, for priority ordering)."""
    return MockConfigEntry(
        domain=DOMAIN,
        title=name,
        data={
            CONF_NAME: name,
            CONF_ENTITY_ID: name.lower().replace(" ", "_"),
            ENTRY_TYPE: ENTRY_TYPE_LOAD,
            CONF_DEVICE_TYPE: DEVICE_TYPE_PLUG,
            CONF_HUB_ENTRY_ID: hub.entry_id,
        },
        options={CONF_LOAD_PRIORITY: priority},
    )


# ── Hub creation end-to-end ────────────────────────────────────────────


async def test_hub_creation_full_flow(hass: HomeAssistant):
    """Walk through user → hub_info → hub_grid and verify the created entry.

    Hardware is no longer part of hub creation: inverters, batteries, PV
    production sensors and forecast sources are all added afterwards as
    Inverter entries. The hub is grid connection + site policy, one page.
    """

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
            CONF_BATTERY_SOC_HYSTERESIS: 3,
        },
    )
    # Grid + site policy is the whole hub — no hardware pages at all.
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["title"] == "My Solar Hub"

    # Verify static data
    entry = result["result"]
    assert entry.data[ENTRY_TYPE] == ENTRY_TYPE_HUB
    assert entry.data[CONF_NAME] == "My Solar Hub"
    assert entry.data[CONF_ENTITY_ID] == "my_solar_hub"
    # Born imported: the legacy hub inverter/battery pages never show
    from custom_components.dynamic_ocpp_evse.const import (
        MIGRATE_HUB_INVERTER_IMPORTED_FLAG,
    )
    assert entry.data[MIGRATE_HUB_INVERTER_IMPORTED_FLAG] is True

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


def _bare_hub(hass, name="Forecast Hub", slug="forecast_hub", **options):
    """A hub with no hardware of its own — the post-slimming shape."""
    from custom_components.dynamic_ocpp_evse.const import (
        MIGRATE_HUB_INVERTER_IMPORTED_FLAG,
    )

    hub = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        minor_version=4,
        title=name,
        data={
            CONF_NAME: name,
            CONF_ENTITY_ID: slug,
            ENTRY_TYPE: ENTRY_TYPE_HUB,
            MIGRATE_HUB_INVERTER_IMPORTED_FLAG: True,
        },
        options={CONF_GRID_EXPORT_LIMIT: 5000, **options},
    )
    hub.add_to_hass(hass)
    return hub


async def test_inverter_forecast_devices_validated(hass: HomeAssistant):
    """The PV array belongs to the inverter, so its forecast devices are
    selected there: a device without any ``watts`` sensor is rejected and
    named, a valid multi-device selection is stored on the inverter entry."""

    _bare_hub(hass)
    east = _make_forecast_device(hass, "array_east")
    west = _make_forecast_device(hass, "array_west")
    wrong = _make_forecast_device(
        hass, "not_a_forecast", watts=False, name="Weather Station"
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={"setup_type": "inverter"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={CONF_INVERTER_FEATURES: ["solar", "battery", "battery_control"]},
    )
    assert result["step_id"] == "inverter_config"

    # A device without any watts-bearing sensor is rejected, named in the error
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            CONF_NAME: "Roof Array",
            CONF_ENTITY_ID: "lj_roof",
            CONF_SOLAR_FORECAST_DEVICE_IDS: [wrong],
        },
    )
    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {
        CONF_SOLAR_FORECAST_DEVICE_IDS: "forecast_device_no_watts"
    }
    assert result["description_placeholders"] == {"entity": "Weather Station"}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            CONF_NAME: "Roof Array",
            CONF_ENTITY_ID: "lj_roof",
            CONF_SOLAR_FORECAST_DEVICE_IDS: [east, west],
        },
    )
    assert result["step_id"] == "inverter_battery"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={}
    )
    # Write-control is the optional last page — submitting it empty keeps the
    # inverter advisory.
    assert result["step_id"] == "inverter_control"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={}
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    from custom_components.dynamic_ocpp_evse.const import ENTRY_TYPE_INVERTER

    inverter = next(
        e
        for e in hass.config_entries.async_entries(DOMAIN)
        if e.data.get(ENTRY_TYPE) == ENTRY_TYPE_INVERTER
    )
    stored = {**inverter.data, **inverter.options}
    assert stored[CONF_SOLAR_FORECAST_DEVICE_IDS] == [east, west]


async def test_inverter_empty_forecast_selection_accepted(hass: HomeAssistant):
    """Leaving the forecast selector empty is valid — the feature is simply off."""

    _bare_hub(hass, name="Plain Hub", slug="plain_hub")

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={"setup_type": "inverter"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={CONF_INVERTER_FEATURES: ["solar", "battery", "battery_control"]},
    )
    # The multi-device selector omits its key entirely when left empty
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={CONF_NAME: "Plain Inverter", CONF_ENTITY_ID: "lj_plain_inv"},
    )
    assert result["step_id"] == "inverter_battery"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={}
    )
    # Write-control is the optional last page — submitting it empty keeps the
    # inverter advisory.
    assert result["step_id"] == "inverter_control"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={}
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    from custom_components.dynamic_ocpp_evse.const import ENTRY_TYPE_INVERTER

    inverter = next(
        e
        for e in hass.config_entries.async_entries(DOMAIN)
        if e.data.get(ENTRY_TYPE) == ENTRY_TYPE_INVERTER
    )
    stored = {**inverter.data, **inverter.options}
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
            CONF_LOAD_PRIORITY: 1,
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
    assert entry.data[ENTRY_TYPE] == ENTRY_TYPE_LOAD
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
            CONF_LOAD_PRIORITY: 1,
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

    Setting the hub up auto-imports its legacy hardware (inverter, battery,
    solar sensor, forecast devices) onto a standalone inverter entry, so the
    options flow is a single page from then on: grid connection plus site
    policy, then straight to save.
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
    hass.states.async_set(
        "sensor.solar_production", "3200",
        {"device_class": "power", "unit_of_measurement": "W"},
    )

    # One page: grid + site policy. No hardware fields — those moved to the
    # inverter entry the setup above auto-created.
    result = await _open_options(hass, mock_hub_entry.entry_id)
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "hub_grid"
    assert not _schema_has(result["data_schema"], CONF_BATTERY_MAX_CHARGE_POWER)
    assert not _schema_has(result["data_schema"], CONF_SOLAR_PRODUCTION_ENTITY_ID)

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
            CONF_BATTERY_SOC_HYSTERESIS: 5,
            CONF_BASE_CONSUMPTION: 400,
            CONF_FORECAST_SOC_FLOOR: 35,
        },
    )
    # Saves and closes — this hub has no loads to prioritise.
    assert result["type"] == FlowResultType.CREATE_ENTRY

    # Options should now contain the submitted values
    assert mock_hub_entry.options.get(CONF_MAIN_BREAKER_RATING) == 25
    assert mock_hub_entry.options.get(CONF_GRID_EXPORT_LIMIT) == 13500
    assert mock_hub_entry.options.get(CONF_BATTERY_SOC_HYSTERESIS) == 5
    assert mock_hub_entry.options.get(CONF_BASE_CONSUMPTION) == 400
    assert mock_hub_entry.options.get(CONF_FORECAST_SOC_FLOOR) == 35


def _suggested_value(data_schema, key):
    """Read the suggested default a form schema offers for a field key."""
    for marker in data_schema.schema:
        if getattr(marker, "schema", None) == key:
            desc = getattr(marker, "description", None) or {}
            return desc.get("suggested_value")
    raise AssertionError(f"{key} not present in schema")


def _schema_default(data_schema, key):
    """Read the vol.Optional *default* a form schema carries for a field key.

    The sibling above reads ``suggested_value``; these are the fields that use a
    plain default instead, which voluptuous stores as a callable.
    """
    for marker in data_schema.schema:
        if getattr(marker, "schema", None) == key:
            default = getattr(marker, "default", None)
            return default() if callable(default) else default
    raise AssertionError(f"{key} not present in schema")


def _schema_has(data_schema, key) -> bool:
    """Whether a form schema offers a field at all."""
    return any(
        getattr(marker, "schema", None) == key for marker in data_schema.schema
    )


def _selector_config(data_schema, key) -> dict:
    """The selector config behind a field — e.g. to assert `multiple: True`."""
    for marker, validator in data_schema.schema.items():
        if getattr(marker, "schema", None) == key:
            return getattr(validator, "config", {}) or {}
    raise AssertionError(f"{key} not present in schema")


async def _open_inverter_settings(hass, entry_id, features=None):
    """Open an inverter's options: the features page first, then settings.

    ``features`` defaults to everything declared, so the settings page shows
    every section the combined page used to.
    """
    result = await _open_options(hass, entry_id)
    assert result["step_id"] == "inverter_features"
    if features is None:
        features = ["solar", "battery", "battery_control"]
    return await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={CONF_INVERTER_FEATURES: features}
    )


async def _open_options(hass, entry_id, step="settings"):
    """Open an entry's options menu and pick one of its entries.

    Every entry type opens on the same menu (settings / overview, plus "how it
    decides" for a hub), so the editable pages are one hop in.
    """
    result = await hass.config_entries.options.async_init(entry_id)
    assert result["type"] == FlowResultType.MENU
    return await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": step}
    )


async def test_inverter_options_does_not_autodetect_phases(
    hass: HomeAssistant,
    mock_hub_entry: MockConfigEntry,
    mock_setup,
):
    """Editing an existing inverter must NOT auto-pick inverter-output entities.

    Regression: the flow used to re-run auto-detection into empty fields, which
    could grab a 3-phase inverter from an unrelated building and silently add
    phantom L2/L3 phases that split available power across phases the site does
    not have. The hub here has no inverter-output phases, so neither does the
    inverter entry auto-imported from it; even with SolarEdge-matching entities
    present in HA, its options page must leave those fields empty.
    """
    from custom_components.dynamic_ocpp_evse.const import ENTRY_TYPE_INVERTER

    mock_hub_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_hub_entry.entry_id)
    await hass.async_block_till_done()

    # Setting the hub up auto-imports its legacy battery config onto an
    # inverter entry — that entry is where the inverter page lives now.
    inverter = next(
        e
        for e in hass.config_entries.async_entries(DOMAIN)
        if e.data.get(ENTRY_TYPE) == ENTRY_TYPE_INVERTER
    )

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

    result = await _open_inverter_settings(hass, inverter.entry_id)

    # None of the three phase fields may be pre-filled.
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "inverter_settings"
    schema = result["data_schema"]
    assert _suggested_value(schema, CONF_INVERTER_OUTPUT_PHASE_A_ENTITY_ID) is None
    assert _suggested_value(schema, CONF_INVERTER_OUTPUT_PHASE_B_ENTITY_ID) is None
    assert _suggested_value(schema, CONF_INVERTER_OUTPUT_PHASE_C_ENTITY_ID) is None


async def test_inverter_options_offers_and_clears_the_soc_slots(
    hass: HomeAssistant,
    mock_hub_entry: MockConfigEntry,
    mock_setup,
):
    """The SOC write-control is editable from the options page too, and an
    emptied slot list means OFF.

    The clearing half is the one that matters: a multi-entity selector omits its
    key entirely once the user removes the last entity, so without the explicit
    normalization the previously stored slots would stay armed while the form
    showed none — Load Juggler would keep writing entities the user believes it
    has released.
    """
    from custom_components.dynamic_ocpp_evse.const import (
        ENTRY_TYPE_INVERTER,
        CONF_SOC_LIMIT_ENTITY_IDS,
        CONF_SOC_LIMIT_NORMAL_ENTITY_ID,
    )

    mock_hub_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_hub_entry.entry_id)
    await hass.async_block_till_done()

    inverter = next(
        e
        for e in hass.config_entries.async_entries(DOMAIN)
        if e.data.get(ENTRY_TYPE) == ENTRY_TYPE_INVERTER
    )

    # Entities the hub already references, so the schemas render.
    for ent in (
        "sensor.inverter_phase_a",
        "sensor.inverter_phase_b",
        "sensor.inverter_phase_c",
    ):
        hass.states.async_set(
            ent, "5.0", {"device_class": "current", "unit_of_measurement": "A"}
        )
    hass.states.async_set("number.deye_tou_soc_1", "100", {"max": 100})
    hass.states.async_set("number.deye_tou_soc_2", "100", {"max": 100})
    hass.states.async_set("input_number.battery_ceiling", "90")

    result = await _open_inverter_settings(hass, inverter.entry_id)
    assert result["step_id"] == "inverter_settings"
    schema = result["data_schema"]
    assert _schema_has(schema, CONF_SOC_LIMIT_ENTITY_IDS)
    assert _schema_has(schema, CONF_SOC_LIMIT_NORMAL_ENTITY_ID)
    assert _selector_config(schema, CONF_SOC_LIMIT_ENTITY_IDS).get("multiple") is True

    # The frontend never submits a field it renders empty — a stored None
    # (e.g. the imported inverter's unset per-phase limit) arrives as an
    # omitted key, letting the schema default fill it.
    submitted = {
        key: value
        for key, value in {**inverter.data, **inverter.options}.items()
        if _schema_has(schema, key) and value is not None
    }
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            **submitted,
            CONF_SOC_LIMIT_ENTITY_IDS: [
                "number.deye_tou_soc_1",
                "number.deye_tou_soc_2",
            ],
            CONF_SOC_LIMIT_NORMAL_ENTITY_ID: "input_number.battery_ceiling",
        },
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert inverter.options[CONF_SOC_LIMIT_ENTITY_IDS] == [
        "number.deye_tou_soc_1",
        "number.deye_tou_soc_2",
    ]

    # Now clear them the way the UI does: the key simply isn't submitted.
    result = await _open_inverter_settings(hass, inverter.entry_id)
    schema = result["data_schema"]
    submitted = {
        key: value
        for key, value in {**inverter.data, **inverter.options}.items()
        if _schema_has(schema, key)
        and key != CONF_SOC_LIMIT_ENTITY_IDS
        and value is not None
    }
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input=submitted
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    assert inverter.options[CONF_SOC_LIMIT_ENTITY_IDS] == []


async def test_inverter_options_round_trip_the_minimum_charge_limit(
    hass: HomeAssistant,
    mock_hub_entry: MockConfigEntry,
    mock_setup,
):
    """The floor under the engaged charge limit is editable from the options
    page, and stores in the target register's own units.

    It sits beside the normal value on the same form and shares its convention —
    amps on an amps register, watts on a watts register — so a number field is
    the whole surface: no entity picker, and therefore no unit contract.
    """
    from custom_components.dynamic_ocpp_evse.const import (
        ENTRY_TYPE_INVERTER,
        CONF_CHARGE_LIMIT_MINIMUM,
        CONF_CHARGE_LIMIT_NORMAL,
        DEFAULT_CHARGE_LIMIT_MINIMUM,
    )

    mock_hub_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_hub_entry.entry_id)
    await hass.async_block_till_done()

    inverter = next(
        e
        for e in hass.config_entries.async_entries(DOMAIN)
        if e.data.get(ENTRY_TYPE) == ENTRY_TYPE_INVERTER
    )

    for ent in (
        "sensor.inverter_phase_a",
        "sensor.inverter_phase_b",
        "sensor.inverter_phase_c",
    ):
        hass.states.async_set(
            ent, "5.0", {"device_class": "current", "unit_of_measurement": "A"}
        )

    result = await _open_inverter_settings(hass, inverter.entry_id)
    assert result["step_id"] == "inverter_settings"
    schema = result["data_schema"]
    assert _schema_has(schema, CONF_CHARGE_LIMIT_MINIMUM)
    # Never negative, and defaulting to 0 — which is "no floor at all", the
    # behaviour that existed before the field.
    assert _selector_config(schema, CONF_CHARGE_LIMIT_MINIMUM).get("min") == 0
    assert DEFAULT_CHARGE_LIMIT_MINIMUM == 0

    submitted = {
        key: value
        for key, value in {**inverter.data, **inverter.options}.items()
        if _schema_has(schema, key) and value is not None
    }
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            **submitted,
            CONF_CHARGE_LIMIT_NORMAL: 100,
            CONF_CHARGE_LIMIT_MINIMUM: 2,
        },
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert inverter.options[CONF_CHARGE_LIMIT_MINIMUM] == 2

    # And the stored value comes back as the form's default on the next visit.
    result = await _open_inverter_settings(hass, inverter.entry_id)
    assert _schema_default(result["data_schema"], CONF_CHARGE_LIMIT_MINIMUM) == 2


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
    result = await _open_options(hass, mock_charger_entry.entry_id)
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "charger"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            CONF_LOAD_PRIORITY: 2,
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
    assert mock_charger_entry.options.get(CONF_LOAD_PRIORITY) == 2
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
    result = await _open_options(hass, mock_charger_entry.entry_id)
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "charger"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            CONF_LOAD_PRIORITY: 1,
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


async def test_options_flow_priority_reorders_devices(
    hass: HomeAssistant,
    mock_hub_entry: MockConfigEntry,
    mock_setup,
):
    """The hub options flow ends on the priority page, which re-ranks loads.

    Selection order becomes the served-first order: first chip = priority 1.
    """
    mock_hub_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_hub_entry.entry_id)
    await hass.async_block_till_done()

    first = _plug_entry(mock_hub_entry, "Pond Pump", priority=1)
    second = _plug_entry(mock_hub_entry, "Power Strip", priority=2)
    first.add_to_hass(hass)
    second.add_to_hass(hass)

    hass.states.async_set(
        "sensor.inverter_phase_a", "5.0",
        {"device_class": "current", "unit_of_measurement": "A"},
    )
    hass.states.async_set(
        "sensor.grid_power_limit", "8050",
        {"device_class": "power", "unit_of_measurement": "W"},
    )

    # Page 1: grid + site policy. The hub's hardware was auto-imported onto an
    # inverter entry during setup, so the next page is the priority order.
    result = await _open_options(hass, mock_hub_entry.entry_id)
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
            CONF_BATTERY_SOC_HYSTERESIS: 5,
            CONF_BASE_CONSUMPTION: 400,
            CONF_FORECAST_SOC_FLOOR: 35,
        },
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "priority"

    # Reverse the order: the second device is served first.
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={CONF_PRIORITY_ORDER: [second.entry_id, first.entry_id]},
    )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert get_entry_value(second, CONF_LOAD_PRIORITY, None) == 1
    assert get_entry_value(first, CONF_LOAD_PRIORITY, None) == 2


# ── Read-only pages: Overview + "How it decides" ───────────────────────
#
# Machine-authored tests — not yet human-reviewed.


async def test_overview_page_survives_no_live_data(
    hass: HomeAssistant,
    mock_hub_entry: MockConfigEntry,
    mock_setup,
):
    """The Overview must render (and say so) before the engine has ever run."""
    from custom_components.dynamic_ocpp_evse.config_flow import _overview_text

    mock_hub_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_hub_entry.entry_id)
    await hass.async_block_till_done()
    hass.data[DOMAIN].pop("hub_data", None)

    text = _overview_text(hass, mock_hub_entry.entry_id)
    assert "No live data yet" in text
    # The static sections still render — the page is never empty.
    assert "Grid" in text
    assert "Loads" in text

    # The step renders as a MENU (real "Refresh"/"Back" buttons instead of a
    # form's fixed "Next" submit) with the text in its description.
    result = await _open_options(hass, mock_hub_entry.entry_id, step="overview")
    assert result["type"] == FlowResultType.MENU
    assert result["step_id"] == "overview"
    assert result["menu_options"] == ["overview", "init"]
    assert "No live data yet" in result["description_placeholders"]["overview"]

    # "Refresh" re-enters the same step with freshly built text…
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={"next_step_id": "overview"}
    )
    assert result["type"] == FlowResultType.MENU
    assert result["step_id"] == "overview"

    # …and "Back" returns to the entry menu.
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={"next_step_id": "init"}
    )
    assert result["type"] == FlowResultType.MENU
    assert result["step_id"] == "init"


async def test_overview_page_reports_live_values(
    hass: HomeAssistant,
    mock_hub_entry: MockConfigEntry,
    mock_setup,
):
    """With hub_data present, the Overview reports the pools and each load."""
    from datetime import datetime, timezone
    from custom_components.dynamic_ocpp_evse.config_flow import _overview_text

    mock_hub_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_hub_entry.entry_id)
    await hass.async_block_till_done()

    plug = _plug_entry(mock_hub_entry, "Pond Pump", priority=1)
    plug.add_to_hass(hass)

    hass.data[DOMAIN]["hub_data"] = {
        mock_hub_entry.entry_id: {
            "last_update": datetime.now(timezone.utc),
            "grid_power": 1200,
            "solar_power": 3400,
            "total_export_power": 0,
            "battery_soc": 64,
            "battery_soc_min": 20,
            "battery_soc_target": 80,
            "battery_power": 500,
            "total_site_available_power": 7000,
            "available_solar_power": 2200,
            "available_grid_power": 4800,
            "available_battery_power": 1500,
            "available_current_a": 12.0,
            "available_current_b": 10.0,
            "available_current_c": 9.0,
            "total_evse_power": 2300,
            "excess_available": True,
            "excess_margin_power": 350,
        }
    }
    hass.data[DOMAIN]["load_allocations"] = {plug.entry_id: 4.3}
    hass.data[DOMAIN]["load_status"] = {plug.entry_id: "Charging"}

    text = _overview_text(hass, mock_hub_entry.entry_id)
    assert "3400 W" in text  # solar production
    assert "64 %" in text  # battery SOC
    assert "Pond Pump" in text
    assert "drawing 4.3 A" in text
    assert "Charging" in text
    assert "Excess trigger: on" in text


async def test_overview_page_for_a_load_entry(
    hass: HomeAssistant,
    mock_hub_entry: MockConfigEntry,
    mock_charger_entry: MockConfigEntry,
    mock_setup,
):
    """A load's Overview is scoped to that load, not the whole site."""
    from custom_components.dynamic_ocpp_evse.config_flow import _overview_text

    mock_hub_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_hub_entry.entry_id)
    await hass.async_block_till_done()
    mock_charger_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_charger_entry.entry_id)
    await hass.async_block_till_done()

    text = _overview_text(hass, mock_charger_entry.entry_id)
    assert "This load" in text
    assert "EVSE" in text
    assert "Priority: 1" in text
    assert "6 A–16 A" in text
    assert "Not in a circuit group" in text

    # The same single step serves it — no per-device-type duplicate.
    result = await _open_options(hass, mock_charger_entry.entry_id, step="overview")
    assert result["step_id"] == "overview"
    assert "This load" in result["description_placeholders"]["overview"]


async def test_summary_page_describes_the_configuration(
    hass: HomeAssistant,
    mock_hub_entry: MockConfigEntry,
    mock_setup,
):
    """"How it decides" lists site, sources, sharing and the load order."""
    from custom_components.dynamic_ocpp_evse.config_flow import _summary_text

    mock_hub_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_hub_entry.entry_id)
    await hass.async_block_till_done()

    plug = _plug_entry(mock_hub_entry, "Pond Pump", priority=2)
    plug.add_to_hass(hass)

    text = _summary_text(hass, mock_hub_entry.entry_id)
    assert "3-phase site at 230 V" in text
    assert "main breaker 25 A per phase" in text
    # The hub's legacy battery was auto-imported onto an inverter entry.
    assert "Power sources" in text
    assert "Distribution mode" in text
    assert "Pond Pump" in text
    assert "Smart plug" in text

    result = await _open_options(hass, mock_hub_entry.entry_id, step="summary")
    assert result["type"] == FlowResultType.MENU
    assert result["step_id"] == "summary"
    assert result["menu_options"] == ["init"]
    assert "Distribution mode" in result["description_placeholders"]["summary"]

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={"next_step_id": "init"}
    )
    assert result["type"] == FlowResultType.MENU


async def test_summary_omits_unconfigured_features(
    hass: HomeAssistant,
    mock_setup,
):
    """An off-grid hub with no export limit shows neither of those lines."""
    from custom_components.dynamic_ocpp_evse.config_flow import _summary_text

    hub = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        minor_version=2,
        title="Cabin",
        data={
            CONF_NAME: "Cabin",
            CONF_ENTITY_ID: "cabin",
            ENTRY_TYPE: ENTRY_TYPE_HUB,
        },
        options={
            CONF_INVERTER_OUTPUT_PHASE_A_ENTITY_ID: "sensor.inverter_phase_a",
            CONF_PHASE_VOLTAGE: 230,
            CONF_BATTERY_SOC_ENTITY_ID: "sensor.battery_soc",
            CONF_BATTERY_POWER_ENTITY_ID: "sensor.battery_power",
        },
    )
    hub.add_to_hass(hass)

    text = _summary_text(hass, hub.entry_id)
    assert "Off-grid" in text
    assert "Export limited" not in text
    assert "max-import limit" not in text
    assert "1-phase site" in text


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

    assert entry.minor_version == 7
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

    assert entry.minor_version == 7
    assert not entry.options.get(CONF_GRID_EXPORT_LIMIT)


# ── 2.4 → 2.5: the charger → load stored-string rename ────────────────


def _legacy_plug_entry(hub_entry_id: str, **overrides) -> MockConfigEntry:
    """A smart-plug entry as 2.4 wrote it: entry_type and priority key legacy."""
    data = {
        CONF_NAME: "Old Plug",
        CONF_ENTITY_ID: "old_plug",
        ENTRY_TYPE: "charger",
        CONF_DEVICE_TYPE: DEVICE_TYPE_PLUG,
        CONF_HUB_ENTRY_ID: hub_entry_id,
        "charger_priority": 2,
    }
    data.update(overrides.pop("data", {}))
    return MockConfigEntry(
        domain=DOMAIN,
        version=2,
        minor_version=4,
        title="Old Plug",
        data=data,
        options=overrides.pop("options", {"charger_priority": 3}),
    )


async def test_migration_renames_stored_charger_strings(hass: HomeAssistant):
    """entry_type "charger" → "load" and charger_priority → load_priority."""
    from custom_components.dynamic_ocpp_evse import async_migrate_entry
    from custom_components.dynamic_ocpp_evse.const import CONF_LOAD_PRIORITY

    entry = _legacy_plug_entry("hub-1")
    entry.add_to_hass(hass)

    assert await async_migrate_entry(hass, entry)

    assert entry.minor_version == 7
    assert entry.data[ENTRY_TYPE] == ENTRY_TYPE_LOAD == "load"
    # Both stores are renamed, and the legacy spelling is gone from each.
    assert entry.data[CONF_LOAD_PRIORITY] == 2
    assert entry.options[CONF_LOAD_PRIORITY] == 3
    assert "charger_priority" not in entry.data
    assert "charger_priority" not in entry.options
    # Untouched: the OCPP charge-point key is genuinely a charger's.
    assert entry.data[CONF_DEVICE_TYPE] == DEVICE_TYPE_PLUG


async def test_migration_of_stored_charger_strings_is_idempotent(
    hass: HomeAssistant,
):
    """An already-migrated entry survives a second pass unchanged."""
    from custom_components.dynamic_ocpp_evse import async_migrate_entry
    from custom_components.dynamic_ocpp_evse.const import CONF_LOAD_PRIORITY

    entry = _legacy_plug_entry("hub-1")
    entry.add_to_hass(hass)
    assert await async_migrate_entry(hass, entry)
    migrated_data = dict(entry.data)
    migrated_options = dict(entry.options)

    # Re-run from the same minor_version the first pass produced, and again
    # from below it — neither may reintroduce or lose anything.
    assert await async_migrate_entry(hass, entry)
    assert dict(entry.data) == migrated_data
    assert dict(entry.options) == migrated_options

    hass.config_entries.async_update_entry(entry, minor_version=4)
    assert await async_migrate_entry(hass, entry)
    assert dict(entry.data) == migrated_data
    assert dict(entry.options) == migrated_options
    assert entry.minor_version == 7


async def test_migration_leaves_non_load_entries_alone(hass: HomeAssistant):
    """A hub entry carries neither string — only its minor_version moves."""
    from custom_components.dynamic_ocpp_evse import async_migrate_entry

    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        minor_version=4,
        title="Hub",
        data={
            CONF_NAME: "Hub",
            CONF_ENTITY_ID: "hub",
            ENTRY_TYPE: ENTRY_TYPE_HUB,
        },
        options={CONF_PHASE_A_CURRENT_ENTITY_ID: "sensor.grid_a"},
    )
    entry.add_to_hass(hass)
    data_before, options_before = dict(entry.data), dict(entry.options)

    assert await async_migrate_entry(hass, entry)

    assert entry.minor_version == 7
    assert dict(entry.data) == data_before
    assert dict(entry.options) == options_before


async def test_migrated_load_entry_sets_up(hass: HomeAssistant, mock_hub_entry):
    """A 2.4-shaped plug entry migrates and then actually sets up.

    The end-to-end half: the rename touches the entry_type that drives the
    whole setup dispatch, so proving the shape is not enough — the entry has
    to reach ConfigEntryState.LOADED through the real setup path and land in
    the runtime buckets under their renamed keys.
    """
    mock_hub_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_hub_entry.entry_id)
    await hass.async_block_till_done()

    entry = _legacy_plug_entry(mock_hub_entry.entry_id)
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert entry.minor_version == 7
    assert entry.data[ENTRY_TYPE] == ENTRY_TYPE_LOAD
    assert entry.entry_id in hass.data[DOMAIN]["loads"]
    assert entry.entry_id in hass.data[DOMAIN]["load_allocations"]
    assert entry.entry_id in (
        hass.data[DOMAIN]["hubs"][mock_hub_entry.entry_id]["loads"]
    )


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
        CONF_CHARGE_LIMIT_ENTITY_ID,
        CONF_CHARGE_LIMIT_UNIT,
        CONF_CHARGE_LIMIT_MINIMUM,
        CONF_BATTERY_NOMINAL_VOLTAGE,
        CONF_SOC_LIMIT_ENTITY_IDS,
        CONF_SOC_LIMIT_NORMAL_ENTITY_ID,
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
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={CONF_INVERTER_FEATURES: ["solar", "battery", "battery_control"]},
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
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "inverter_control"

    # Both write-controls are configured on this one page. The SOC side is a
    # LIST — a Deye's ceiling lives in its time-of-use slots, one entity each —
    # with the everyday ceiling coming from an entity the user's own automations
    # keep owning.
    schema = result["data_schema"]
    assert _schema_has(schema, CONF_SOC_LIMIT_ENTITY_IDS)
    assert _schema_has(schema, CONF_SOC_LIMIT_NORMAL_ENTITY_ID)
    assert _selector_config(schema, CONF_SOC_LIMIT_ENTITY_IDS).get("multiple") is True
    # The floor under the engaged charge limit — a plain number in the target
    # register's own units, beside the normal value that uses the same
    # convention, and never an entity picker.
    assert _schema_has(schema, CONF_CHARGE_LIMIT_MINIMUM)
    assert _selector_config(schema, CONF_CHARGE_LIMIT_MINIMUM).get("min") == 0

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            CONF_CHARGE_LIMIT_ENTITY_ID: "number.deye_max_charge_current",
            CONF_CHARGE_LIMIT_UNIT: "A",
            CONF_CHARGE_LIMIT_MINIMUM: 2,
            CONF_BATTERY_NOMINAL_VOLTAGE: 51.2,
            CONF_SOC_LIMIT_ENTITY_IDS: [
                "number.deye_tou_soc_1",
                "number.deye_tou_soc_2",
            ],
            CONF_SOC_LIMIT_NORMAL_ENTITY_ID: "input_number.battery_ceiling",
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
    assert stored[CONF_CHARGE_LIMIT_ENTITY_ID] == "number.deye_max_charge_current"
    assert stored[CONF_CHARGE_LIMIT_MINIMUM] == 2
    assert stored[CONF_SOC_LIMIT_ENTITY_IDS] == [
        "number.deye_tou_soc_1",
        "number.deye_tou_soc_2",
    ]
    assert stored[CONF_SOC_LIMIT_NORMAL_ENTITY_ID] == "input_number.battery_ceiling"
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
            CONF_SOLAR_PRODUCTION_ENTITY_ID: "sensor.pv_power",
            CONF_SOLAR_FORECAST_DEVICE_IDS: ["dev_east"],
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
    # Named for the hardware, not the hub — none of the imported entities
    # belongs to a device here, so it falls back to the plain default.
    assert inverter.data[CONF_NAME] == "Inverter"
    assert inverter.title == "Inverter"
    assert inverter.options[CONF_INVERTER_MAX_POWER] == 17000
    assert inverter.options[CONF_BATTERY_SOC_ENTITY_ID] == "sensor.batt_soc"
    assert inverter.options[CONF_BATTERY_CAPACITY_KWH] == 10
    # The PV array belongs to the inverter too — sensor and forecast device.
    assert inverter.options[CONF_SOLAR_PRODUCTION_ENTITY_ID] == "sensor.pv_power"
    assert inverter.options[CONF_SOLAR_FORECAST_DEVICE_IDS] == ["dev_east"]

    # The hub is blanked and flagged; site policy stays behind.
    assert hub.data[MIGRATE_HUB_INVERTER_IMPORTED_FLAG] is True
    assert CONF_INVERTER_MAX_POWER not in hub.options
    assert CONF_BATTERY_SOC_ENTITY_ID not in hub.options
    assert CONF_SOLAR_PRODUCTION_ENTITY_ID not in hub.options
    assert CONF_SOLAR_FORECAST_DEVICE_IDS not in hub.options
    assert hub.options[CONF_GRID_EXPORT_LIMIT] == 13500
    assert hub.options[CONF_BATTERY_SOC_HYSTERESIS] == 3


async def test_hub_inverter_auto_import_names_after_the_device(hass: HomeAssistant):
    """The auto-created entry takes its name from the device behind the
    imported entities — "Site Load Management Inverter" is the hub wearing a
    hat, while the integration already calls the hardware something useful."""
    from homeassistant.helpers import device_registry as dr, entity_registry as er

    source = MockConfigEntry(domain="solarman", title="Deye")
    source.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=source.entry_id,
        identifiers={("solarman", "deye_hybrid")},
        name="Deye Hybrid",
    )
    soc = er.async_get(hass).async_get_or_create(
        "sensor", "solarman", "deye_soc",
        device_id=device.id, config_entry=source,
        suggested_object_id="deye_battery_soc",
    )

    hub = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        minor_version=4,
        title="Site Load Management",
        data={
            CONF_NAME: "Site Load Management",
            CONF_ENTITY_ID: "site_load_management",
            ENTRY_TYPE: ENTRY_TYPE_HUB,
        },
        options={CONF_BATTERY_SOC_ENTITY_ID: soc.entity_id},
    )
    hub.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "import"}, data={"hub_entry_id": hub.entry_id}
    )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["result"].data[CONF_NAME] == "Deye Hybrid"
    assert result["result"].title == "Deye Hybrid Inverter"


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


async def test_hub_inverter_import_merges_later_fields(hass: HomeAssistant):
    """A hub migrated by an earlier release still holds the fields that later
    releases move (here the solar sensor and forecast devices). The next
    import round merges them onto the SAME inverter entry — no second entry,
    and values already edited on the inverter are not overwritten."""
    from custom_components.dynamic_ocpp_evse.const import (
        ENTRY_TYPE_INVERTER,
        DEVICE_TYPE_INVERTER,
        CONF_DEVICE_TYPE,
        MIGRATE_HUB_INVERTER_IMPORTED_FLAG,
    )

    hub = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        minor_version=4,
        title="Migrated Hub",
        data={
            CONF_NAME: "Migrated Hub",
            CONF_ENTITY_ID: "migrated_hub",
            ENTRY_TYPE: ENTRY_TYPE_HUB,
            MIGRATE_HUB_INVERTER_IMPORTED_FLAG: True,
        },
        options={
            CONF_GRID_EXPORT_LIMIT: 13500,
            # Left behind by the earlier round, which did not move these yet
            CONF_SOLAR_PRODUCTION_ENTITY_ID: "sensor.pv_power",
            CONF_SOLAR_FORECAST_DEVICE_IDS: ["dev_east"],
        },
    )
    hub.add_to_hass(hass)
    existing = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        minor_version=4,
        title="Migrated Hub Inverter",
        unique_id=f"{hub.entry_id}_inverter_import",
        data={
            CONF_NAME: "Migrated Hub Inverter",
            CONF_ENTITY_ID: "migrated_hub_inverter",
            ENTRY_TYPE: ENTRY_TYPE_INVERTER,
            CONF_DEVICE_TYPE: DEVICE_TYPE_INVERTER,
            CONF_HUB_ENTRY_ID: hub.entry_id,
            "imported_from_hub": True,
        },
        options={
            CONF_INVERTER_MAX_POWER: 17000,
            CONF_BATTERY_SOC_ENTITY_ID: "sensor.batt_soc",
        },
    )
    existing.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "import"}, data={"hub_entry_id": hub.entry_id}
    )
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "already_configured"

    # Merged onto the existing entry, existing values untouched
    assert existing.options[CONF_SOLAR_PRODUCTION_ENTITY_ID] == "sensor.pv_power"
    assert existing.options[CONF_SOLAR_FORECAST_DEVICE_IDS] == ["dev_east"]
    assert existing.options[CONF_INVERTER_MAX_POWER] == 17000
    assert existing.options[CONF_BATTERY_SOC_ENTITY_ID] == "sensor.batt_soc"

    # Gone from the hub, so the trigger does not fire again next restart
    assert CONF_SOLAR_PRODUCTION_ENTITY_ID not in hub.options
    assert CONF_SOLAR_FORECAST_DEVICE_IDS not in hub.options
    assert hub.options[CONF_GRID_EXPORT_LIMIT] == 13500

    inverters = [
        e
        for e in hass.config_entries.async_entries(DOMAIN)
        if e.data.get(ENTRY_TYPE) == ENTRY_TYPE_INVERTER
    ]
    assert len(inverters) == 1


# ── Inverter features: the first page decides which pages follow ──────────


async def test_pv_only_inverter_skips_the_battery_pages_and_stores_no_battery(
    hass: HomeAssistant, mock_hub_entry: MockConfigEntry, mock_setup
):
    """Solar only: inverter_config creates the entry directly — no battery
    page, no write-control page — and the entry carries no battery cap. That
    default (5000 W on a PV-only entry) is the phantom that took 53 % of the
    charge advice on a live site."""
    mock_hub_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_hub_entry.entry_id)
    await hass.async_block_till_done()
    hass.states.async_set(
        "sensor.string_pv", "3000",
        {"device_class": "power", "unit_of_measurement": "W"},
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={"setup_type": "inverter"}
    )
    assert result["step_id"] == "inverter_features"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={CONF_INVERTER_FEATURES: ["solar"]}
    )
    assert result["step_id"] == "inverter_config"
    schema = result["data_schema"]
    assert _schema_has(schema, CONF_SOLAR_PRODUCTION_ENTITY_ID)
    assert not _schema_has(schema, CONF_BATTERY_SOC_ENTITY_ID)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            CONF_NAME: "String Only",
            CONF_ENTITY_ID: "lj_string_only",
            CONF_INVERTER_MAX_POWER: 8700,
            CONF_SOLAR_PRODUCTION_ENTITY_ID: "sensor.string_pv",
        },
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    options = result["options"]
    assert options[CONF_INVERTER_FEATURES] == ["solar"]
    assert options[CONF_SOLAR_PRODUCTION_ENTITY_ID] == "sensor.string_pv"
    assert options.get(CONF_BATTERY_MAX_CHARGE_POWER) is None
    assert options.get(CONF_BATTERY_MAX_DISCHARGE_POWER) is None
    assert options.get(CONF_BATTERY_SOC_ENTITY_ID) is None


async def test_battery_without_control_ends_on_the_battery_page(
    hass: HomeAssistant, mock_hub_entry: MockConfigEntry, mock_setup
):
    """Solar + battery, no write-control: the battery page is the last one."""
    mock_hub_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_hub_entry.entry_id)
    await hass.async_block_till_done()
    hass.states.async_set(
        "sensor.hyb_soc", "50", {"device_class": "battery", "unit_of_measurement": "%"}
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={"setup_type": "inverter"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={CONF_INVERTER_FEATURES: ["battery"]}
    )
    assert result["step_id"] == "inverter_config"
    assert not _schema_has(result["data_schema"], CONF_SOLAR_PRODUCTION_ENTITY_ID)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={CONF_NAME: "Hybrid NoCtl", CONF_ENTITY_ID: "lj_hyb_noctl"},
    )
    assert result["step_id"] == "inverter_battery"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={CONF_BATTERY_SOC_ENTITY_ID: "sensor.hyb_soc"},
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["options"][CONF_INVERTER_FEATURES] == ["battery"]
    assert result["options"][CONF_BATTERY_SOC_ENTITY_ID] == "sensor.hyb_soc"
    assert result["options"][CONF_BATTERY_MAX_CHARGE_POWER] == 5000


async def test_write_control_without_a_battery_is_refused(
    hass: HomeAssistant, mock_hub_entry: MockConfigEntry, mock_setup
):
    mock_hub_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_hub_entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={"setup_type": "inverter"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={CONF_INVERTER_FEATURES: ["solar", "battery_control"]},
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "inverter_features"
    assert result["errors"] == {CONF_INVERTER_FEATURES: "control_needs_battery"}


async def test_unticking_the_battery_in_options_clears_its_settings(
    hass: HomeAssistant, mock_hub_entry: MockConfigEntry, mock_setup
):
    """The options flow opens on the features page with the declared (or
    inferred) list suggested; unticking battery hides its section and clears
    every battery key on save, so a stale cap cannot outlive the battery."""
    mock_hub_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_hub_entry.entry_id)
    await hass.async_block_till_done()

    inverter = next(
        e
        for e in hass.config_entries.async_entries(DOMAIN)
        if e.data.get(ENTRY_TYPE) == ENTRY_TYPE_INVERTER
    )
    for ent in ("sensor.inverter_phase_a", "sensor.inverter_phase_b", "sensor.inverter_phase_c"):
        hass.states.async_set(
            ent, "5.0", {"device_class": "current", "unit_of_measurement": "A"}
        )
    stored_before = {**inverter.data, **inverter.options}
    assert stored_before.get(CONF_BATTERY_SOC_ENTITY_ID)

    result = await _open_options(hass, inverter.entry_id)
    assert result["step_id"] == "inverter_features"
    assert "battery" in _suggested_value(result["data_schema"], CONF_INVERTER_FEATURES)

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={CONF_INVERTER_FEATURES: ["solar"]}
    )
    assert result["step_id"] == "inverter_settings"
    schema = result["data_schema"]
    assert not _schema_has(schema, CONF_BATTERY_SOC_ENTITY_ID)
    assert not _schema_has(schema, CONF_BATTERY_MAX_CHARGE_POWER)

    submitted = {
        key: value
        for key, value in stored_before.items()
        if _schema_has(schema, key) and value is not None
    }
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input=submitted
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert inverter.options[CONF_INVERTER_FEATURES] == ["solar"]
    assert inverter.options[CONF_BATTERY_SOC_ENTITY_ID] is None
    assert inverter.options[CONF_BATTERY_POWER_ENTITY_ID] is None
    assert inverter.options[CONF_BATTERY_MAX_CHARGE_POWER] is None
    assert inverter.options[CONF_BATTERY_MAX_DISCHARGE_POWER] is None


async def test_migration_infers_features_and_drops_the_phantom_battery(
    hass: HomeAssistant,
):
    """A pre-2.7 PV-only inverter entry carrying the form's battery defaults
    comes out declared solar-only with those defaults cleared; a hybrid with a
    charge register comes out with all three features and nothing touched."""
    from custom_components.dynamic_ocpp_evse import async_migrate_entry
    from custom_components.dynamic_ocpp_evse.const import (
        CONF_CHARGE_LIMIT_ENTITY_ID,
        CONF_SOLAR_FORECAST_DEVICE_IDS,
    )

    hub = MockConfigEntry(
        domain=DOMAIN, version=2, minor_version=6, title="Hub",
        data={CONF_NAME: "Hub", CONF_ENTITY_ID: "hub", ENTRY_TYPE: ENTRY_TYPE_HUB},
        options={CONF_PHASE_A_CURRENT_ENTITY_ID: "sensor.grid_a"},
    )
    pv_only = MockConfigEntry(
        domain=DOMAIN, version=2, minor_version=6, title="SolarEdge",
        data={
            CONF_NAME: "SolarEdge", CONF_ENTITY_ID: "lj_se",
            ENTRY_TYPE: ENTRY_TYPE_INVERTER, CONF_HUB_ENTRY_ID: hub.entry_id,
        },
        options={
            CONF_SOLAR_PRODUCTION_ENTITY_ID: "sensor.se_power",
            CONF_INVERTER_MAX_POWER: 8700,
            CONF_BATTERY_MAX_CHARGE_POWER: 5000,
            CONF_BATTERY_MAX_DISCHARGE_POWER: 5000,
        },
    )
    hybrid = MockConfigEntry(
        domain=DOMAIN, version=2, minor_version=6, title="Deye",
        data={
            CONF_NAME: "Deye", CONF_ENTITY_ID: "lj_deye",
            ENTRY_TYPE: ENTRY_TYPE_INVERTER, CONF_HUB_ENTRY_ID: hub.entry_id,
        },
        options={
            CONF_SOLAR_FORECAST_DEVICE_IDS: ["dev1"],
            CONF_BATTERY_SOC_ENTITY_ID: "sensor.deye_soc",
            CONF_BATTERY_MAX_CHARGE_POWER: 4500,
            CONF_CHARGE_LIMIT_ENTITY_ID: "number.deye_max_charge_current",
        },
    )
    for entry in (hub, pv_only, hybrid):
        entry.add_to_hass(hass)
        assert await async_migrate_entry(hass, entry)
        assert entry.minor_version == 7

    assert CONF_INVERTER_FEATURES not in hub.options
    assert pv_only.options[CONF_INVERTER_FEATURES] == ["solar"]
    assert pv_only.options[CONF_BATTERY_MAX_CHARGE_POWER] is None
    assert pv_only.options[CONF_BATTERY_MAX_DISCHARGE_POWER] is None
    assert pv_only.options[CONF_SOLAR_PRODUCTION_ENTITY_ID] == "sensor.se_power"
    assert hybrid.options[CONF_INVERTER_FEATURES] == [
        "solar", "battery", "battery_control"
    ]
    assert hybrid.options[CONF_BATTERY_MAX_CHARGE_POWER] == 4500
    assert hybrid.options[CONF_CHARGE_LIMIT_ENTITY_ID] == "number.deye_max_charge_current"

    # Idempotent: a second pass changes nothing.
    assert await async_migrate_entry(hass, pv_only)
    assert pv_only.options[CONF_INVERTER_FEATURES] == ["solar"]
