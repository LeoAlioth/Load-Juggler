"""Tests for the sensor entity update cycle.

These tests create actual sensor entity instances with mocked HA states
and call async_update() to verify the data flow from HA entities through
the calculation engine to the sensor state.
"""

from unittest.mock import patch, AsyncMock, MagicMock
from datetime import datetime, timedelta

import pytest
from homeassistant.core import HomeAssistant
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
    CONF_EVSE_CURRENT_IMPORT_ENTITY_ID,
    CONF_EVSE_CURRENT_OFFERED_ENTITY_ID,
    CONF_PHASE_A_CURRENT_ENTITY_ID,
    CONF_PHASE_B_CURRENT_ENTITY_ID,
    CONF_PHASE_C_CURRENT_ENTITY_ID,
    CONF_MAIN_BREAKER_RATING,
    CONF_INVERT_PHASES,
    CONF_MAX_IMPORT_POWER_ENTITY_ID,
    CONF_PHASE_VOLTAGE,
    CONF_EXCESS_EXPORT_THRESHOLD,
    CONF_BATTERY_SOC_ENTITY_ID,
    CONF_BATTERY_POWER_ENTITY_ID,
    CONF_BATTERY_MAX_CHARGE_POWER,
    CONF_BATTERY_MAX_DISCHARGE_POWER,
    CONF_BATTERY_SOC_HYSTERESIS,
    CONF_CHARGING_MODE_ENTITY_ID,
    CONF_BATTERY_SOC_TARGET_ENTITY_ID,
    CONF_ALLOW_GRID_CHARGING_ENTITY_ID,
    CONF_POWER_BUFFER_ENTITY_ID,
    CONF_CHARGER_PRIORITY,
    CONF_EVSE_MINIMUM_CHARGE_CURRENT,
    CONF_EVSE_MAXIMUM_CHARGE_CURRENT,
    CONF_CHARGE_RATE_UNIT,
    CONF_PROFILE_VALIDITY_MODE,
    CONF_UPDATE_FREQUENCY,
    CONF_OCPP_PROFILE_TIMEOUT,
    CONF_CHARGE_PAUSE_DURATION,
    CONF_STACK_LEVEL,
    CONF_TOTAL_ALLOCATED_CURRENT,
    CONF_PHASES,
    CONF_CHARGING_MODE,
    DEFAULT_MIN_CHARGE_CURRENT,
    DEFAULT_MAX_CHARGE_CURRENT,
    DEFAULT_PHASE_VOLTAGE,
    DEFAULT_MAIN_BREAKER_RATING,
    DEFAULT_EXCESS_EXPORT_THRESHOLD,
    DEFAULT_BATTERY_MAX_POWER,
    DEFAULT_BATTERY_SOC_HYSTERESIS,
    DEFAULT_CHARGE_PAUSE_DURATION,
)
from custom_components.dynamic_ocpp_evse.sensor import (
    DynamicOcppEvseChargerSensor,
    DynamicOcppEvseHubSensor,
    DynamicOcppEvseHubDataSensor,
    HUB_SENSOR_DEFINITIONS,
)


# ── Fixtures ───────────────────────────────────────────────────────────


@pytest.fixture
def hub_entry() -> MockConfigEntry:
    """Hub config entry with full grid + battery configuration."""
    return MockConfigEntry(
        domain=DOMAIN,
        version=2,
        minor_version=1,
        title="Test Hub",
        data={
            CONF_NAME: "Test Hub",
            CONF_ENTITY_ID: "test_hub",
            ENTRY_TYPE: ENTRY_TYPE_HUB,
            CONF_CHARGING_MODE_ENTITY_ID: "select.test_hub_charging_mode",
            CONF_BATTERY_SOC_TARGET_ENTITY_ID: "number.test_hub_home_battery_soc_target",
            CONF_ALLOW_GRID_CHARGING_ENTITY_ID: "switch.test_hub_allow_grid_charging",
            CONF_POWER_BUFFER_ENTITY_ID: "number.test_hub_power_buffer",
        },
        options={
            CONF_PHASE_A_CURRENT_ENTITY_ID: "sensor.inverter_phase_a",
            CONF_PHASE_B_CURRENT_ENTITY_ID: "sensor.inverter_phase_b",
            CONF_PHASE_C_CURRENT_ENTITY_ID: "sensor.inverter_phase_c",
            CONF_MAIN_BREAKER_RATING: 25,
            CONF_INVERT_PHASES: False,
            CONF_MAX_IMPORT_POWER_ENTITY_ID: "sensor.grid_power_limit",
            CONF_PHASE_VOLTAGE: 230,
            CONF_EXCESS_EXPORT_THRESHOLD: 13000,
            CONF_BATTERY_SOC_ENTITY_ID: "sensor.battery_soc",
            CONF_BATTERY_POWER_ENTITY_ID: "sensor.battery_power",
            CONF_BATTERY_MAX_CHARGE_POWER: 5000,
            CONF_BATTERY_MAX_DISCHARGE_POWER: 5000,
            CONF_BATTERY_SOC_HYSTERESIS: 3,
        },
    )


@pytest.fixture
def charger_entry(hub_entry: MockConfigEntry) -> MockConfigEntry:
    """Charger config entry linked to the test hub."""
    return MockConfigEntry(
        domain=DOMAIN,
        version=2,
        minor_version=1,
        title="Test Charger",
        data={
            CONF_ENTITY_ID: "test_charger",
            CONF_NAME: "Test Charger",
            ENTRY_TYPE: ENTRY_TYPE_CHARGER,
            CONF_CHARGER_ID: "test_charger",
            CONF_OCPP_DEVICE_ID: "ocpp_device_1",
            CONF_EVSE_CURRENT_IMPORT_ENTITY_ID: "sensor.test_charger_current_import",
            CONF_EVSE_CURRENT_OFFERED_ENTITY_ID: "sensor.test_charger_current_offered",
            CONF_HUB_ENTRY_ID: hub_entry.entry_id,
        },
        options={
            CONF_CHARGER_PRIORITY: 1,
            CONF_EVSE_MINIMUM_CHARGE_CURRENT: 6,
            CONF_EVSE_MAXIMUM_CHARGE_CURRENT: 16,
            CONF_CHARGE_RATE_UNIT: "A",
            CONF_PROFILE_VALIDITY_MODE: "relative",
            CONF_UPDATE_FREQUENCY: 15,
            CONF_OCPP_PROFILE_TIMEOUT: 120,
            CONF_CHARGE_PAUSE_DURATION: 180,
            CONF_STACK_LEVEL: 3,
        },
    )


@pytest.fixture
def setup_domain_data(hass, hub_entry, charger_entry):
    """Initialize hass.data[DOMAIN] with hub and charger structures."""
    hass.data[DOMAIN] = {
        "hubs": {
            hub_entry.entry_id: {
                "entry": hub_entry,
                "chargers": [charger_entry.entry_id],
                "charging_mode": "Standard",
                "distribution_mode": "Priority",
                "allow_grid_charging": True,
                "power_buffer": 0,
                "max_import_power": None,
                "battery_soc_target": 80,
                "battery_soc_min": 20,
            },
        },
        "chargers": {
            charger_entry.entry_id: {
                "entry": charger_entry,
                "hub_entry_id": hub_entry.entry_id,
                "min_current": None,
                "max_current": None,
                "device_power": None,
                "dynamic_control": True,
            },
        },
        "charger_allocations": {
            charger_entry.entry_id: 0,
        },
    }


def _set_ha_states(hass, hub_entry):
    """Populate HA entity states simulating a real solar installation.

    This represents a 3-phase system with:
    - Grid importing ~5A per phase
    - Battery at 80% SOC, discharging 500W
    - OCPP charger currently drawing 10A on L1
    """
    # Phase current sensors (what the hub reads to determine grid usage)
    hass.states.async_set(
        "sensor.inverter_phase_a", "5.0",
        {"device_class": "current", "unit_of_measurement": "A"},
    )
    hass.states.async_set(
        "sensor.inverter_phase_b", "4.5",
        {"device_class": "current", "unit_of_measurement": "A"},
    )
    hass.states.async_set(
        "sensor.inverter_phase_c", "3.8",
        {"device_class": "current", "unit_of_measurement": "A"},
    )
    # Grid power limit
    hass.states.async_set(
        "sensor.grid_power_limit", "17250",
        {"device_class": "power", "unit_of_measurement": "W"},
    )
    # Battery
    hass.states.async_set(
        "sensor.battery_soc", "80",
        {"device_class": "battery", "unit_of_measurement": "%"},
    )
    hass.states.async_set(
        "sensor.battery_power", "-500",
        {"device_class": "power", "unit_of_measurement": "W"},
    )
    # OCPP charger sensor — currently drawing 10A on L1
    hass.states.async_set(
        "sensor.test_charger_current_import", "10.0",
        {
            "l1_current": 10.0,
            "l2_current": 0.0,
            "l3_current": 0.0,
            "device_class": "current",
            "unit_of_measurement": "A",
        },
    )
    hass.states.async_set(
        "sensor.test_charger_current_offered", "16.0",
        {"device_class": "current", "unit_of_measurement": "A"},
    )
    # Connector status — car is charging
    hass.states.async_set("sensor.test_charger_status_connector", "Charging")
    # Charge control switch — on
    hass.states.async_set("switch.test_charger_charge_control", "on")
    # Hub-level runtime state (written to hass.data, not entity states)
    hub_data = hass.data[DOMAIN]["hubs"][hub_entry.entry_id]
    hub_data["charging_mode"] = "Standard"
    hub_data["distribution_mode"] = "Priority"
    hub_data["allow_grid_charging"] = True
    hub_data["power_buffer"] = 200
    hub_data["battery_soc_target"] = 90
    hub_data["battery_soc_min"] = 20


# ── Sensor creation tests ─────────────────────────────────────────────


async def test_charger_sensor_initializes(hass, hub_entry, charger_entry):
    """Test that the charger sensor initializes with correct attributes."""
    sensor = DynamicOcppEvseChargerSensor(
        hass, charger_entry, hub_entry, "Test Charger", "test_charger", None
    )
    assert sensor.state is None
    assert sensor.unit_of_measurement == "A"
    assert sensor.device_class == "current"
    assert sensor.icon == "mdi:ev-station"
    assert sensor._pause_started_at is None

    attrs = sensor.extra_state_attributes
    assert attrs["pause_active"] is False
    assert attrs["allocated_current"] is None
    assert attrs[CONF_HUB_ENTRY_ID] == hub_entry.entry_id


async def test_hub_sensor_initializes(hass, hub_entry):
    """Test that the hub sensor initializes with correct attributes."""
    sensor = DynamicOcppEvseHubSensor(hass, hub_entry, "Test Hub", "test_hub")
    assert sensor.state == 0.0  # Returns 0 when no data, not None
    assert sensor.unit_of_measurement == "W"
    assert sensor.device_class == "power"


async def test_hub_data_sensors_initialize(hass, hub_entry):
    """Test that all hub data sensors from HUB_SENSOR_DEFINITIONS are created."""
    sensors = []
    for defn in HUB_SENSOR_DEFINITIONS:
        sensor = DynamicOcppEvseHubDataSensor(
            hass, hub_entry, "Test Hub", "test_hub", defn
        )
        sensors.append(sensor)

    assert len(sensors) == len(HUB_SENSOR_DEFINITIONS)
    # Verify each sensor has correct properties from its definition
    for sensor, defn in zip(sensors, HUB_SENSOR_DEFINITIONS):
        assert sensor._attr_name == f"Test Hub {defn['name_suffix']}"
        assert sensor._attr_unique_id == f"test_hub_{defn['unique_id_suffix']}"
        assert sensor.state is None  # No data yet


# ── Calculation engine reads HA entity states ─────────────────────────


async def test_calculate_available_current_reads_ha_entities(
    hass,
    hub_entry,
    charger_entry,
    setup_domain_data,
):
    """Verify that run_hub_calculation reads HA entity states.

    With 3-phase Standard mode, 25A breaker, grid importing ~5A/phase,
    the charger (3p, min=6A, max=16A) should get a real allocation.
    """
    from custom_components.dynamic_ocpp_evse.dynamic_ocpp_evse import (
        run_hub_calculation,
    )

    _set_ha_states(hass, hub_entry)

    sensor = DynamicOcppEvseChargerSensor(
        hass, charger_entry, hub_entry, "Test Charger", "test_charger", None
    )

    result = run_hub_calculation(sensor)

    # With the fix, HA entity states are actually read — Standard mode with
    # 25A breaker and grid importing ~5A/phase leaves ~20A headroom per phase,
    # capped by charger max (16A)
    assert result[CONF_TOTAL_ALLOCATED_CURRENT] > 0, (
        "Available current should be > 0 in Standard mode with spare grid capacity"
    )

    # Battery SOC should be read from sensor.battery_soc entity (80%)
    assert result.get("battery_soc") == 80.0, (
        "Battery SOC should be read from the HA entity"
    )

    # Grid importing ~5A/phase with 25A breaker → ~20A available per phase
    assert result.get("available_current_a") == 20.0
    assert result.get("available_current_b") == 20.5
    assert result.get("available_current_c") == 21.2

    # Charger targets should contain our charger with a real allocation
    charger_targets = result.get("charger_targets", {})
    assert charger_entry.entry_id in charger_targets, (
        "Charger should appear in charger_targets"
    )
    assert charger_targets[charger_entry.entry_id] > 0, (
        "Charger target should be > 0 in Standard mode with available capacity"
    )


# ── Charger sensor update cycle tests ─────────────────────────────────


async def test_charger_sensor_update_calls_ocpp(
    hass,
    hub_entry,
    charger_entry,
    setup_domain_data,
):
    """Test that async_update sends an OCPP set_charge_rate service call."""
    _set_ha_states(hass, hub_entry)

    sensor = DynamicOcppEvseChargerSensor(
        hass, charger_entry, hub_entry, "Test Charger", "test_charger", None
    )

    # Mock the OCPP service call — we don't have a real OCPP integration
    with patch("homeassistant.core.ServiceRegistry.async_call", new_callable=AsyncMock) as mock_call:
        await sensor.async_update()

        # The sensor should have called ocpp.set_charge_rate
        ocpp_calls = [
            c for c in mock_call.call_args_list
            if c[0][0] == "ocpp" and c[0][1] == "set_charge_rate"
        ]
        assert len(ocpp_calls) == 1, (
            f"Expected exactly 1 OCPP call, got {len(ocpp_calls)}"
        )

        call_data = ocpp_calls[0][0][2]  # positional arg 3 = service_data
        assert call_data["devid"] == "ocpp_device_1"
        assert "custom_profile" in call_data


async def test_charger_sensor_update_writes_hub_data(
    hass,
    hub_entry,
    charger_entry,
    setup_domain_data,
):
    """Test that async_update populates hass.data hub_data for hub sensor to read."""
    _set_ha_states(hass, hub_entry)

    sensor = DynamicOcppEvseChargerSensor(
        hass, charger_entry, hub_entry, "Test Charger", "test_charger", None
    )

    with patch("homeassistant.core.ServiceRegistry.async_call", new_callable=AsyncMock):
        await sensor.async_update()

    # Hub data should now be populated
    hub_data = hass.data[DOMAIN].get("hub_data", {}).get(hub_entry.entry_id, {})
    assert hub_data, "hub_data should be populated after charger sensor update"
    assert "last_update" in hub_data
    assert "total_site_available_power" in hub_data


async def test_hub_sensor_reads_hub_data(
    hass,
    hub_entry,
    charger_entry,
    setup_domain_data,
):
    """Test that hub sensor reads values written by charger sensor."""
    _set_ha_states(hass, hub_entry)

    # First: run charger sensor to populate hub_data
    charger_sensor = DynamicOcppEvseChargerSensor(
        hass, charger_entry, hub_entry, "Test Charger", "test_charger", None
    )
    with patch("homeassistant.core.ServiceRegistry.async_call", new_callable=AsyncMock):
        await charger_sensor.async_update()

    # Then: run hub sensor update
    hub_sensor = DynamicOcppEvseHubSensor(hass, hub_entry, "Test Hub", "test_hub")
    await hub_sensor.async_update()

    # Hub sensor should have read the data
    assert hub_sensor._total_site_available_power is not None or hub_sensor.state == 0.0


async def test_hub_data_sensor_reads_values(
    hass,
    hub_entry,
    charger_entry,
    setup_domain_data,
):
    """Test that individual hub data sensors read their specific values."""
    _set_ha_states(hass, hub_entry)

    # Populate hub_data via charger sensor
    charger_sensor = DynamicOcppEvseChargerSensor(
        hass, charger_entry, hub_entry, "Test Charger", "test_charger", None
    )
    with patch("homeassistant.core.ServiceRegistry.async_call", new_callable=AsyncMock):
        await charger_sensor.async_update()

    # Create a hub data sensor for "grid_power"
    defn = next(d for d in HUB_SENSOR_DEFINITIONS if d["hub_data_key"] == "grid_power")
    data_sensor = DynamicOcppEvseHubDataSensor(hass, hub_entry, "Test Hub", "test_hub", defn)
    await data_sensor.async_update()

    # The sensor should have read from hub_data
    assert data_sensor.state is not None or data_sensor.state == 0


# ── Charge pause logic ────────────────────────────────────────────────


async def test_charge_pause_starts_when_below_minimum(
    hass,
    hub_entry,
    charger_entry,
    setup_domain_data,
):
    """Test that charge pause starts when allocated current < min_current.

    Uses Solar mode with grid importing (no export surplus). The charger
    is active (connector_status=Charging) but gets 0A because there is
    no solar power available — triggering the pause logic.
    """
    _set_ha_states(hass, hub_entry)
    # Override to Solar mode — with grid importing there is no solar surplus
    hass.data[DOMAIN]["hubs"][hub_entry.entry_id]["charging_mode"] = "Solar"

    sensor = DynamicOcppEvseChargerSensor(
        hass, charger_entry, hub_entry, "Test Charger", "test_charger", None
    )

    with patch("homeassistant.core.ServiceRegistry.async_call", new_callable=AsyncMock):
        await sensor.async_update()

    # In Solar mode with no export, charger gets 0A which is < min (6A)
    assert sensor._pause_started_at is not None, (
        "Pause should start when allocated current (0) < min_current (6)"
    )
    assert sensor.extra_state_attributes["pause_active"] is True


async def test_charge_pause_holds_at_zero(
    hass,
    hub_entry,
    charger_entry,
    setup_domain_data,
):
    """Test that during pause, the OCPP profile limit is set to 0.

    Uses Solar mode with no export surplus so the charger gets 0A allocation.
    """
    _set_ha_states(hass, hub_entry)
    # Override to Solar mode — charger gets 0A allocation
    hass.data[DOMAIN]["hubs"][hub_entry.entry_id]["charging_mode"] = "Solar"

    sensor = DynamicOcppEvseChargerSensor(
        hass, charger_entry, hub_entry, "Test Charger", "test_charger", None
    )

    with patch("homeassistant.core.ServiceRegistry.async_call", new_callable=AsyncMock) as mock_call:
        await sensor.async_update()

        # Find the OCPP call and check the limit
        ocpp_calls = [
            c for c in mock_call.call_args_list
            if c[0][0] == "ocpp" and c[0][1] == "set_charge_rate"
        ]
        assert len(ocpp_calls) == 1
        profile = ocpp_calls[0][0][2]["custom_profile"]
        limit = profile["chargingSchedule"]["chargingSchedulePeriod"][0]["limit"]
        assert limit == 0, f"During pause, limit should be 0A but got {limit}"


async def test_no_ocpp_call_without_device_id(
    hass,
    hub_entry,
    setup_domain_data,
):
    """Test that sensor skips OCPP call when OCPP device ID is missing."""
    _set_ha_states(hass, hub_entry)

    # Create a charger entry without OCPP device ID
    charger_entry_no_device = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        minor_version=1,
        title="No Device Charger",
        data={
            CONF_ENTITY_ID: "no_device_charger",
            CONF_NAME: "No Device",
            ENTRY_TYPE: ENTRY_TYPE_CHARGER,
            CONF_CHARGER_ID: "no_device",
            # No CONF_OCPP_DEVICE_ID!
            CONF_EVSE_CURRENT_IMPORT_ENTITY_ID: "sensor.test_charger_current_import",
            CONF_EVSE_CURRENT_OFFERED_ENTITY_ID: "sensor.test_charger_current_offered",
            CONF_HUB_ENTRY_ID: hub_entry.entry_id,
        },
        options={
            CONF_CHARGER_PRIORITY: 1,
            CONF_EVSE_MINIMUM_CHARGE_CURRENT: 6,
            CONF_EVSE_MAXIMUM_CHARGE_CURRENT: 16,
            CONF_CHARGE_RATE_UNIT: "A",
            CONF_PROFILE_VALIDITY_MODE: "relative",
            CONF_UPDATE_FREQUENCY: 15,
            CONF_OCPP_PROFILE_TIMEOUT: 120,
            CONF_CHARGE_PAUSE_DURATION: 180,
            CONF_STACK_LEVEL: 3,
        },
    )

    # Register in domain data
    hass.data[DOMAIN]["chargers"][charger_entry_no_device.entry_id] = {
        "entry": charger_entry_no_device,
        "hub_entry_id": hub_entry.entry_id,
        "min_current": None,
        "max_current": None,
        "device_power": None,
        "dynamic_control": True,
    }
    hass.data[DOMAIN]["hubs"][hub_entry.entry_id]["chargers"].append(
        charger_entry_no_device.entry_id
    )
    hass.data[DOMAIN]["charger_allocations"][charger_entry_no_device.entry_id] = 0

    sensor = DynamicOcppEvseChargerSensor(
        hass, charger_entry_no_device, hub_entry, "No Device", "no_device_charger", None
    )

    with patch("homeassistant.core.ServiceRegistry.async_call", new_callable=AsyncMock) as mock_call:
        await sensor.async_update()

        # Should NOT have called any OCPP service
        ocpp_calls = [
            c for c in mock_call.call_args_list
            if c[0][0] == "ocpp"
        ]
        assert len(ocpp_calls) == 0, "Should not call OCPP without device ID"


# ── OCPP profile format tests ─────────────────────────────────────────


async def test_relative_profile_format(
    hass,
    hub_entry,
    charger_entry,
    setup_domain_data,
):
    """Test that a relative-mode profile has correct structure."""
    _set_ha_states(hass, hub_entry)

    sensor = DynamicOcppEvseChargerSensor(
        hass, charger_entry, hub_entry, "Test Charger", "test_charger", None
    )

    with patch("homeassistant.core.ServiceRegistry.async_call", new_callable=AsyncMock) as mock_call:
        await sensor.async_update()

        ocpp_calls = [
            c for c in mock_call.call_args_list
            if c[0][0] == "ocpp" and c[0][1] == "set_charge_rate"
        ]
        profile = ocpp_calls[0][0][2]["custom_profile"]

        # Relative profile structure
        assert profile["chargingProfileKind"] == "Relative"
        assert profile["chargingProfilePurpose"] == "TxDefaultProfile"
        assert profile["stackLevel"] == 3
        assert "duration" in profile["chargingSchedule"]
        assert profile["chargingSchedule"]["chargingRateUnit"] == "A"


async def test_absolute_profile_format(
    hass,
    hub_entry,
    setup_domain_data,
):
    """Test that an absolute-mode profile has validFrom/validTo timestamps."""
    _set_ha_states(hass, hub_entry)

    # Create charger with absolute profile mode
    charger_absolute = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        minor_version=1,
        title="Abs Charger",
        data={
            CONF_ENTITY_ID: "abs_charger",
            CONF_NAME: "Abs Charger",
            ENTRY_TYPE: ENTRY_TYPE_CHARGER,
            CONF_CHARGER_ID: "abs_charger",
            CONF_OCPP_DEVICE_ID: "device_abs",
            CONF_EVSE_CURRENT_IMPORT_ENTITY_ID: "sensor.test_charger_current_import",
            CONF_EVSE_CURRENT_OFFERED_ENTITY_ID: "sensor.test_charger_current_offered",
            CONF_HUB_ENTRY_ID: hub_entry.entry_id,
        },
        options={
            CONF_CHARGER_PRIORITY: 1,
            CONF_EVSE_MINIMUM_CHARGE_CURRENT: 6,
            CONF_EVSE_MAXIMUM_CHARGE_CURRENT: 16,
            CONF_CHARGE_RATE_UNIT: "A",
            CONF_PROFILE_VALIDITY_MODE: "absolute",
            CONF_UPDATE_FREQUENCY: 15,
            CONF_OCPP_PROFILE_TIMEOUT: 120,
            CONF_CHARGE_PAUSE_DURATION: 180,
            CONF_STACK_LEVEL: 3,
        },
    )

    hass.data[DOMAIN]["chargers"][charger_absolute.entry_id] = {
        "entry": charger_absolute,
        "hub_entry_id": hub_entry.entry_id,
        "min_current": None,
        "max_current": None,
        "device_power": None,
        "dynamic_control": True,
    }
    hass.data[DOMAIN]["hubs"][hub_entry.entry_id]["chargers"].append(
        charger_absolute.entry_id
    )
    hass.data[DOMAIN]["charger_allocations"][charger_absolute.entry_id] = 0

    sensor = DynamicOcppEvseChargerSensor(
        hass, charger_absolute, hub_entry, "Abs Charger", "abs_charger", None
    )

    with patch("homeassistant.core.ServiceRegistry.async_call", new_callable=AsyncMock) as mock_call:
        await sensor.async_update()

        ocpp_calls = [
            c for c in mock_call.call_args_list
            if c[0][0] == "ocpp" and c[0][1] == "set_charge_rate"
        ]
        profile = ocpp_calls[0][0][2]["custom_profile"]

        # Absolute profile structure
        assert profile["chargingProfileKind"] == "Absolute"
        assert "validFrom" in profile
        assert "validTo" in profile
        assert "startSchedule" in profile["chargingSchedule"]


# ── Charge rate unit conversion ────────────────────────────────────────


async def test_watts_charge_rate_conversion(
    hass,
    hub_entry,
    setup_domain_data,
):
    """Test that charge rate in Watts mode converts A to W correctly."""
    _set_ha_states(hass, hub_entry)

    charger_watts = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        minor_version=1,
        title="Watts Charger",
        data={
            CONF_ENTITY_ID: "watts_charger",
            CONF_NAME: "Watts Charger",
            ENTRY_TYPE: ENTRY_TYPE_CHARGER,
            CONF_CHARGER_ID: "watts_charger",
            CONF_OCPP_DEVICE_ID: "device_watts",
            CONF_EVSE_CURRENT_IMPORT_ENTITY_ID: "sensor.test_charger_current_import",
            CONF_EVSE_CURRENT_OFFERED_ENTITY_ID: "sensor.test_charger_current_offered",
            CONF_HUB_ENTRY_ID: hub_entry.entry_id,
        },
        options={
            CONF_CHARGER_PRIORITY: 1,
            CONF_EVSE_MINIMUM_CHARGE_CURRENT: 6,
            CONF_EVSE_MAXIMUM_CHARGE_CURRENT: 16,
            CONF_CHARGE_RATE_UNIT: "W",
            CONF_PROFILE_VALIDITY_MODE: "relative",
            CONF_UPDATE_FREQUENCY: 15,
            CONF_OCPP_PROFILE_TIMEOUT: 120,
            CONF_CHARGE_PAUSE_DURATION: 180,
            CONF_STACK_LEVEL: 3,
        },
    )

    hass.data[DOMAIN]["chargers"][charger_watts.entry_id] = {
        "entry": charger_watts,
        "hub_entry_id": hub_entry.entry_id,
        "min_current": None,
        "max_current": None,
        "device_power": None,
        "dynamic_control": True,
    }
    hass.data[DOMAIN]["hubs"][hub_entry.entry_id]["chargers"].append(
        charger_watts.entry_id
    )
    hass.data[DOMAIN]["charger_allocations"][charger_watts.entry_id] = 0

    sensor = DynamicOcppEvseChargerSensor(
        hass, charger_watts, hub_entry, "Watts Charger", "watts_charger", None
    )

    with patch("homeassistant.core.ServiceRegistry.async_call", new_callable=AsyncMock) as mock_call:
        await sensor.async_update()

        ocpp_calls = [
            c for c in mock_call.call_args_list
            if c[0][0] == "ocpp" and c[0][1] == "set_charge_rate"
        ]
        profile = ocpp_calls[0][0][2]["custom_profile"]
        assert profile["chargingSchedule"]["chargingRateUnit"] == "W"


# ── Result dict completeness test ────────────────────────────────────


async def test_result_dict_all_keys_populated(
    hass,
    hub_entry,
    charger_entry,
    setup_domain_data,
):
    """Verify every key in the result dict is populated (not None) when
    HA entities are fully configured.

    This test acts as a safety net: if a new hub sensor key is added to
    sensor.py but not to the result dict in dynamic_ocpp_evse.py, this
    test will catch the mismatch.
    """
    from custom_components.dynamic_ocpp_evse.dynamic_ocpp_evse import (
        run_hub_calculation,
    )

    _set_ha_states(hass, hub_entry)

    sensor = DynamicOcppEvseChargerSensor(
        hass, charger_entry, hub_entry, "Test Charger", "test_charger", None
    )

    result = run_hub_calculation(sensor)

    # Every key that hub_data storage (sensor.py) reads must be present
    # in the result dict AND must not be None when entities are configured.
    expected_keys = {
        CONF_TOTAL_ALLOCATED_CURRENT,
        CONF_PHASES,
        CONF_CHARGING_MODE,
        "calc_used",
        "battery_soc",
        "battery_soc_min",
        "battery_soc_target",
        "battery_power",
        "available_battery_power",
        "available_current_a",
        "available_current_b",
        "available_current_c",
        "total_site_available_power",
        "grid_power",
        "available_grid_power",
        "total_evse_power",
        "solar_power",
        "available_solar_power",
        "charger_targets",
        "charger_names",
        "distribution_mode",
    }

    missing = expected_keys - set(result.keys())
    assert not missing, f"Result dict missing keys: {missing}"

    none_keys = {k for k in expected_keys if result.get(k) is None}
    assert not none_keys, (
        f"These result dict keys are None when all HA entities are configured: {none_keys}"
    )


async def test_result_dict_values_are_reasonable(
    hass,
    hub_entry,
    charger_entry,
    setup_domain_data,
):
    """Verify result dict values match expectations for the test scenario.

    Test scenario: 3-phase, 25A breaker, importing ~5A/phase, battery at 80%
    SOC discharging 500W, charger drawing 10A on L1.
    """
    from custom_components.dynamic_ocpp_evse.dynamic_ocpp_evse import (
        run_hub_calculation,
    )

    _set_ha_states(hass, hub_entry)

    sensor = DynamicOcppEvseChargerSensor(
        hass, charger_entry, hub_entry, "Test Charger", "test_charger", None
    )

    result = run_hub_calculation(sensor)

    # --- Grid / phase values ---
    assert result[CONF_PHASES] == 3
    # Importing ~5A/phase with 25A breaker → ~20A available per phase
    assert result["available_current_a"] == 20.0
    assert result["available_current_b"] == 20.5
    assert result["available_current_c"] == 21.2
    # Total site available: (20 + 20.5 + 21.2) * 230 ≈ 14191W
    assert result["total_site_available_power"] > 14000
    # Net consumption: (5.0 + 4.5 + 3.8) * 230 ≈ 3059W
    assert 3000 < result["grid_power"] < 3200
    # Grid headroom (same as available since no export):
    # (25-5)*230 + (25-4.5)*230 + (25-3.8)*230 = 4600 + 4715 + 4876 = 14191
    assert result["available_grid_power"] > 14000
    # --- Battery ---
    assert result["battery_soc"] == 80.0
    assert result["battery_power"] == -500.0  # charging at 500W
    assert result["battery_soc_min"] is not None
    assert result["battery_soc_target"] == 90.0
    # SOC 80% >= min 20% and battery_max_discharge = 5000 → available
    assert result["available_battery_power"] == 5000

    # --- EVSE ---
    # Charger drawing 10A on L1 → 10 * 230 = 2300W
    assert result["total_evse_power"] == 2300

    # --- Charger targets ---
    assert result[CONF_CHARGING_MODE] == "Standard"
    assert result["distribution_mode"] == "Priority"
    assert charger_entry.entry_id in result["charger_targets"]
    assert result[CONF_TOTAL_ALLOCATED_CURRENT] > 0


async def test_allow_grid_charging_off_reduces_available(
    hass,
    hub_entry,
    charger_entry,
    setup_domain_data,
):
    """When allow_grid_charging switch is OFF, the grid contribution is removed
    so the charger target should be lower than when ON (inverter-only power)."""
    from custom_components.dynamic_ocpp_evse.dynamic_ocpp_evse import (
        run_hub_calculation,
    )

    # First: run with grid charging ON
    _set_ha_states(hass, hub_entry)
    sensor_on = DynamicOcppEvseChargerSensor(
        hass, charger_entry, hub_entry, "Test Charger", "test_charger", None
    )
    result_on = run_hub_calculation(sensor_on)
    target_on = result_on["charger_targets"].get(charger_entry.entry_id, 0)

    # Then: run with grid charging OFF
    hass.data[DOMAIN]["hubs"][hub_entry.entry_id]["allow_grid_charging"] = False
    sensor_off = DynamicOcppEvseChargerSensor(
        hass, charger_entry, hub_entry, "Test Charger", "test_charger", None
    )
    result_off = run_hub_calculation(sensor_off)
    target_off = result_off["charger_targets"].get(charger_entry.entry_id, 0)

    # Grid charging OFF should yield less power than ON
    assert target_off < target_on, (
        f"allow_grid_charging=off ({target_off:.1f}A) should give less than "
        f"allow_grid_charging=on ({target_on:.1f}A)"
    )


async def test_power_buffer_reduces_grid_available(
    hass,
    hub_entry,
    charger_entry,
    setup_domain_data,
):
    """Power buffer is subtracted from max_grid_import_power, reducing
    the grid available power and thus the charger target.

    Test scenario: 3-phase, importing ~5A/phase (~3060W total consumption).
    grid_power_limit = 6000W → available for EVs = (6000-3060)/230 ≈ 12.8A total → 4.3A/phase.
    With 2000W buffer → effective = 4000W → (4000-3060)/230 ≈ 4.1A total → below min_current → 0A.
    """
    from custom_components.dynamic_ocpp_evse.dynamic_ocpp_evse import (
        run_hub_calculation,
    )

    _set_ha_states(hass, hub_entry)
    # Set a low grid power limit so it becomes the binding constraint
    hass.states.async_set("sensor.grid_power_limit", "6000")

    # Run with power buffer = 0
    hass.data[DOMAIN]["hubs"][hub_entry.entry_id]["power_buffer"] = 0
    sensor_no_buf = DynamicOcppEvseChargerSensor(
        hass, charger_entry, hub_entry, "Test Charger", "test_charger", None
    )
    result_no_buf = run_hub_calculation(sensor_no_buf)
    target_no_buf = result_no_buf["charger_targets"].get(charger_entry.entry_id, 0)

    # Run with 2000W buffer → effective grid limit drops significantly
    hass.data[DOMAIN]["hubs"][hub_entry.entry_id]["power_buffer"] = 2000
    sensor_buf = DynamicOcppEvseChargerSensor(
        hass, charger_entry, hub_entry, "Test Charger", "test_charger", None
    )
    result_buf = run_hub_calculation(sensor_buf)
    target_buf = result_buf["charger_targets"].get(charger_entry.entry_id, 0)

    # With the buffer reducing effective grid import, charger gets less power
    assert target_buf < target_no_buf, (
        f"power_buffer=2000W ({target_buf:.1f}A) should give less than "
        f"power_buffer=0 ({target_no_buf:.1f}A)"
    )


# ── Rate limiting tests ──────────────────────────────────────────────


async def test_rate_limit_ramp_up_capped(
    hass,
    hub_entry,
    charger_entry,
    setup_domain_data,
):
    """Test that ramp-up is capped at RAMP_UP_RATE * site_update_frequency per cycle.

    With site_update_frequency=5s and RAMP_UP_RATE=0.1 A/s, max ramp-up is 0.5A.
    If previous smoothed value was 6A and engine wants 16A, the allocated should be 6.5A.
    """
    from custom_components.dynamic_ocpp_evse.const import RAMP_UP_RATE, DEFAULT_SITE_UPDATE_FREQUENCY

    _set_ha_states(hass, hub_entry)

    sensor = DynamicOcppEvseChargerSensor(
        hass, charger_entry, hub_entry, "Test Charger", "test_charger", None
    )
    # Simulate previous cycle had smoothed 6A
    sensor._rate_limited_current = 6.0
    # Need prev modes set so mode_changed = False
    sensor._prev_charging_mode = "Standard"
    sensor._prev_distribution_mode = "Priority"

    with patch("homeassistant.core.ServiceRegistry.async_call", new_callable=AsyncMock) as mock_call:
        await sensor.async_update()

        ocpp_calls = [
            c for c in mock_call.call_args_list
            if c[0][0] == "ocpp" and c[0][1] == "set_charge_rate"
        ]
        assert len(ocpp_calls) == 1
        profile = ocpp_calls[0][0][2]["custom_profile"]
        limit = profile["chargingSchedule"]["chargingSchedulePeriod"][0]["limit"]

        # Engine would allocate 16A (max), but rate limit caps at 6 + 0.5 = 6.5A
        max_allowed = 6.0 + RAMP_UP_RATE * DEFAULT_SITE_UPDATE_FREQUENCY
        assert limit <= max_allowed, (
            f"Rate-limited ramp-up should be <= {max_allowed}A, got {limit}A"
        )
        assert limit > 6.0, f"Limit should have increased from 6A, got {limit}A"


async def test_rate_limit_ramp_down_capped(
    hass,
    hub_entry,
    charger_entry,
    setup_domain_data,
):
    """Test that ramp-down is capped at RAMP_DOWN_RATE * site_update_frequency per cycle.

    With site_update_frequency=5s and RAMP_DOWN_RATE=0.2 A/s, max ramp-down is 1.0A.
    If previous smoothed value was 16A and engine wants 6A, the allocated should be 15A.
    """
    from custom_components.dynamic_ocpp_evse.const import RAMP_DOWN_RATE, DEFAULT_SITE_UPDATE_FREQUENCY

    _set_ha_states(hass, hub_entry)
    sensor = DynamicOcppEvseChargerSensor(
        hass, charger_entry, hub_entry, "Test Charger", "test_charger", None
    )
    # Simulate previous cycle had smoothed 16A
    sensor._rate_limited_current = 16.0
    # Need prev modes set so mode_changed = False
    sensor._prev_charging_mode = "Eco"
    sensor._prev_distribution_mode = "Priority"

    # Eco mode with battery SOC below target — engine gives min_current (6A)
    hass.data[DOMAIN]["hubs"][hub_entry.entry_id]["charging_mode"] = "Eco"
    hass.states.async_set("sensor.battery_soc", "50")
    hass.data[DOMAIN]["hubs"][hub_entry.entry_id]["battery_soc_target"] = 90

    with patch("homeassistant.core.ServiceRegistry.async_call", new_callable=AsyncMock) as mock_call:
        await sensor.async_update()

        ocpp_calls = [
            c for c in mock_call.call_args_list
            if c[0][0] == "ocpp" and c[0][1] == "set_charge_rate"
        ]
        assert len(ocpp_calls) == 1
        profile = ocpp_calls[0][0][2]["custom_profile"]
        limit = profile["chargingSchedule"]["chargingSchedulePeriod"][0]["limit"]

        # Engine wants 6A (eco min), but ramp-down caps at 16 - 1.0 = 15A
        min_allowed = 16.0 - RAMP_DOWN_RATE * DEFAULT_SITE_UPDATE_FREQUENCY
        assert limit >= min_allowed, (
            f"Rate-limited ramp-down should be >= {min_allowed}A, got {limit}A"
        )
        assert limit < 16.0, f"Limit should have decreased from 16A, got {limit}A"


async def test_rate_limit_not_applied_on_resume_from_pause(
    hass,
    hub_entry,
    charger_entry,
    setup_domain_data,
):
    """Test that rate limiting is NOT applied when resuming from pause (0 → N).

    When _rate_limited_current is 0 (pause), the charger should jump directly
    to the calculated value without rate limiting.
    """
    _set_ha_states(hass, hub_entry)

    sensor = DynamicOcppEvseChargerSensor(
        hass, charger_entry, hub_entry, "Test Charger", "test_charger", None
    )
    # Simulate coming out of pause — rate_limited_current is 0
    sensor._rate_limited_current = 0.0

    with patch("homeassistant.core.ServiceRegistry.async_call", new_callable=AsyncMock) as mock_call:
        await sensor.async_update()

        ocpp_calls = [
            c for c in mock_call.call_args_list
            if c[0][0] == "ocpp" and c[0][1] == "set_charge_rate"
        ]
        assert len(ocpp_calls) == 1
        profile = ocpp_calls[0][0][2]["custom_profile"]
        limit = profile["chargingSchedule"]["chargingSchedulePeriod"][0]["limit"]

        # Should jump directly to full allocation (16A max), not be rate-limited
        assert limit > 1.5, (
            f"Resume from pause should NOT rate-limit, got {limit}A (would be 1.5 if limited)"
        )


# ── Auto-reset detection tests ───────────────────────────────────────


async def test_auto_reset_mismatch_counter_increments(
    hass,
    hub_entry,
    charger_entry,
    setup_domain_data,
):
    """Test that mismatch counter increments when current_offered differs."""
    _set_ha_states(hass, hub_entry)

    sensor = DynamicOcppEvseChargerSensor(
        hass, charger_entry, hub_entry, "Test Charger", "test_charger", None
    )
    # Simulate: last cycle we sent 16A
    sensor._last_commanded_limit = 16.0

    # But charger is offering 0A (stuck / ignoring us)
    hass.states.async_set(
        "sensor.test_charger_current_offered", "0.0",
        {"device_class": "current", "unit_of_measurement": "A"},
    )

    with patch("homeassistant.core.ServiceRegistry.async_call", new_callable=AsyncMock):
        await sensor.async_update()

    assert sensor._mismatch_count >= 1, (
        f"Mismatch count should be >= 1, got {sensor._mismatch_count}"
    )


async def test_auto_reset_counter_resets_on_compliance(
    hass,
    hub_entry,
    charger_entry,
    setup_domain_data,
):
    """Test that mismatch counter resets when charger becomes compliant."""
    _set_ha_states(hass, hub_entry)

    sensor = DynamicOcppEvseChargerSensor(
        hass, charger_entry, hub_entry, "Test Charger", "test_charger", None
    )
    sensor._mismatch_count = 3  # Simulate prior mismatches
    sensor._last_commanded_limit = 16.0

    # Charger is offering 16A — matches what we sent
    hass.states.async_set(
        "sensor.test_charger_current_offered", "16.0",
        {"device_class": "current", "unit_of_measurement": "A"},
    )

    with patch("homeassistant.core.ServiceRegistry.async_call", new_callable=AsyncMock):
        await sensor.async_update()

    assert sensor._mismatch_count == 0, (
        f"Mismatch count should reset to 0 when compliant, got {sensor._mismatch_count}"
    )


async def test_auto_reset_triggers_after_threshold(
    hass,
    hub_entry,
    charger_entry,
    setup_domain_data,
):
    """Test that auto-reset fires after sustained mismatch reaches threshold."""
    from custom_components.dynamic_ocpp_evse.const import AUTO_RESET_MISMATCH_THRESHOLD

    _set_ha_states(hass, hub_entry)

    sensor = DynamicOcppEvseChargerSensor(
        hass, charger_entry, hub_entry, "Test Charger", "test_charger", None
    )
    # Pre-set mismatch count to one below threshold
    sensor._mismatch_count = AUTO_RESET_MISMATCH_THRESHOLD - 1
    sensor._last_commanded_limit = 16.0

    # Charger offering 0A — big mismatch
    hass.states.async_set(
        "sensor.test_charger_current_offered", "0.0",
        {"device_class": "current", "unit_of_measurement": "A"},
    )

    with patch("homeassistant.core.ServiceRegistry.async_call", new_callable=AsyncMock) as mock_call:
        await sensor.async_update()

        # Check that reset_ocpp_evse was called
        reset_calls = [
            c for c in mock_call.call_args_list
            if len(c[0]) >= 2 and c[0][0] == DOMAIN and c[0][1] == "reset_ocpp_evse"
        ]
        assert len(reset_calls) == 1, (
            f"Auto-reset should have been triggered, got {len(reset_calls)} calls"
        )
        assert sensor._last_auto_reset_at is not None


async def test_auto_reset_cooldown_prevents_retrigger(
    hass,
    hub_entry,
    charger_entry,
    setup_domain_data,
):
    """Test that cooldown prevents immediate re-triggering after reset."""
    from custom_components.dynamic_ocpp_evse.const import AUTO_RESET_MISMATCH_THRESHOLD

    _set_ha_states(hass, hub_entry)

    sensor = DynamicOcppEvseChargerSensor(
        hass, charger_entry, hub_entry, "Test Charger", "test_charger", None
    )
    # Simulate: just reset recently
    sensor._last_auto_reset_at = datetime.now()
    sensor._last_commanded_limit = 16.0
    sensor._mismatch_count = AUTO_RESET_MISMATCH_THRESHOLD + 5  # Would trigger

    # Charger still offering 0A
    hass.states.async_set(
        "sensor.test_charger_current_offered", "0.0",
        {"device_class": "current", "unit_of_measurement": "A"},
    )

    with patch("homeassistant.core.ServiceRegistry.async_call", new_callable=AsyncMock) as mock_call:
        await sensor.async_update()

        # Should NOT trigger reset during cooldown
        reset_calls = [
            c for c in mock_call.call_args_list
            if len(c[0]) >= 2 and c[0][0] == DOMAIN and c[0][1] == "reset_ocpp_evse"
        ]
        assert len(reset_calls) == 0, (
            f"Should NOT reset during cooldown, got {len(reset_calls)} reset calls"
        )


async def test_feedback_loop_subtracts_charger_draw_from_consumption(
    hass,
    hub_entry,
    charger_entry,
    setup_domain_data,
):
    """Test that charger draw is subtracted from grid consumption before engine runs.

    Grid CTs measure total site current INCLUDING charger draws. Without the
    feedback loop fix, the engine double-counts charger power. With the fix,
    the charger's 10A L1 draw is subtracted from phase_a consumption (5A),
    resulting in adjusted consumption of 0A on phase A.

    This means the engine sees more available headroom on phase A than the
    raw sensor reading would suggest.
    """
    from custom_components.dynamic_ocpp_evse.dynamic_ocpp_evse import (
        run_hub_calculation,
    )

    _set_ha_states(hass, hub_entry)

    sensor = DynamicOcppEvseChargerSensor(
        hass, charger_entry, hub_entry, "Test Charger", "test_charger", None
    )

    result = run_hub_calculation(sensor)

    # The charger draws 10A on L1 (from entity attributes).
    # Phase A grid reading is 5.0A import.
    # After subtracting: adjusted consumption = max(0, 5.0 - 10.0) = 0.0A
    # The engine should see 25A available on phase A (full breaker rating).
    # Charger target should still be 16A (max) since there's plenty of headroom.
    charger_targets = result.get("charger_targets", {})
    target = charger_targets.get(charger_entry.entry_id, 0)
    assert target == 16.0, (
        f"With feedback loop fix, charger should get full 16A (max), got {target}A"
    )

    # NOTE: The hub sensor display values (available_current_a etc.)
    # use the raw grid readings, NOT the adjusted consumption. This is intentional:
    # the display shows actual grid state, the engine uses adjusted values.
    assert result["available_current_a"] == 20.0
    assert result["available_current_b"] == 20.5
    assert result["available_current_c"] == 21.2


async def test_feedback_loop_with_constrained_breaker(
    hass,
    hub_entry,
    charger_entry,
    setup_domain_data,
):
    """Test feedback loop fix with heavy charger draw on a normal breaker.

    Grid reads ~5A/phase import, but charger is drawing 4A/phase. Without fix,
    the engine sees 5A consumption; with fix it sees max(0, 5-4)=1A, giving
    more headroom: 25-1=24A on phase A vs 25-5=20A without fix.
    """
    from custom_components.dynamic_ocpp_evse.dynamic_ocpp_evse import (
        run_hub_calculation,
    )

    _set_ha_states(hass, hub_entry)
    # Charger drawing 4A on all 3 phases (instead of default 10/0/0)
    hass.states.async_set(
        "sensor.test_charger_current_import", "4.0",
        {
            "l1_current": 4.0,
            "l2_current": 4.0,
            "l3_current": 4.0,
            "device_class": "current",
            "unit_of_measurement": "A",
        },
    )

    sensor = DynamicOcppEvseChargerSensor(
        hass, charger_entry, hub_entry, "Test Charger", "test_charger", None
    )

    result = run_hub_calculation(sensor)

    # Phase A: consumption=5.0A, charger_l1=4.0A → adjusted=1.0A → headroom=24.0A
    # Phase B: consumption=4.5A, charger_l2=4.0A → adjusted=0.5A → headroom=24.5A
    # Phase C: consumption=3.8A, charger_l3=4.0A → adjusted=0.0A → headroom=25.0A
    # 3-phase charger gets min(24, 24.5, 25) = 16A (capped at max_current)
    charger_targets = result.get("charger_targets", {})
    target = charger_targets.get(charger_entry.entry_id, 0)
    assert target == 16.0, (
        f"With feedback loop fix, charger should get 16A (max), got {target}A"
    )

    # Verify the display values still use raw readings (not adjusted)
    assert result["available_current_a"] == 20.0
    assert result["available_current_b"] == 20.5
    assert result["available_current_c"] == 21.2


async def test_charge_pause_cancelled_on_charging_mode_change(
    hass,
    hub_entry,
    charger_entry,
    setup_domain_data,
):
    """Test that active charge pause is cancelled when user changes charging mode.

    Start in Solar mode (no surplus → pause starts), then switch to Standard mode.
    The pause should be cancelled immediately on the mode change.
    """
    _set_ha_states(hass, hub_entry)
    # Start in Solar mode — no export surplus → charger gets 0A → pause starts
    hass.data[DOMAIN]["hubs"][hub_entry.entry_id]["charging_mode"] = "Solar"

    sensor = DynamicOcppEvseChargerSensor(
        hass, charger_entry, hub_entry, "Test Charger", "test_charger", None
    )

    with patch("homeassistant.core.ServiceRegistry.async_call", new_callable=AsyncMock):
        # First update: Solar mode, no surplus → pause starts
        await sensor.async_update()
        assert sensor._pause_started_at is not None, "Pause should have started in Solar mode"
        assert sensor._prev_charging_mode == "Solar"

        # Switch to Standard mode
        hass.data[DOMAIN]["hubs"][hub_entry.entry_id]["charging_mode"] = "Standard"

        # Second update: mode changed → pause should be cancelled
        await sensor.async_update()
        assert sensor._pause_started_at is None, (
            "Pause should be cancelled when charging mode changes from Solar to Standard"
        )
        assert sensor._prev_charging_mode == "Standard"


async def test_charge_pause_cancelled_on_distribution_mode_change(
    hass,
    hub_entry,
    charger_entry,
    setup_domain_data,
):
    """Test that active charge pause is cancelled when user changes distribution mode.

    Start in Solar mode (triggers pause), then change BOTH distribution mode AND
    charging mode to Standard. The mode change cancels the pause, and Standard
    mode provides enough current to prevent a new pause from starting.
    """
    _set_ha_states(hass, hub_entry)
    # Start in Solar mode — charger gets 0A → pause starts
    hass.data[DOMAIN]["hubs"][hub_entry.entry_id]["charging_mode"] = "Solar"

    sensor = DynamicOcppEvseChargerSensor(
        hass, charger_entry, hub_entry, "Test Charger", "test_charger", None
    )

    with patch("homeassistant.core.ServiceRegistry.async_call", new_callable=AsyncMock):
        # First update: Solar mode → pause starts
        await sensor.async_update()
        assert sensor._pause_started_at is not None, "Pause should have started"
        assert sensor._prev_distribution_mode == "Priority"

        # Switch distribution mode AND charging mode so charger gets current
        hass.data[DOMAIN]["hubs"][hub_entry.entry_id]["distribution_mode"] = "Shared"
        hass.data[DOMAIN]["hubs"][hub_entry.entry_id]["charging_mode"] = "Standard"

        # Second update: distribution mode changed → pause cancelled,
        # Standard mode gives current → no new pause
        await sensor.async_update()
        assert sensor._pause_started_at is None, (
            "Pause should be cancelled when distribution mode changes"
        )
        assert sensor._prev_distribution_mode == "Shared"


async def test_charge_pause_remaining_seconds_attribute(
    hass,
    hub_entry,
    charger_entry,
    setup_domain_data,
):
    """Test that pause_remaining_seconds attribute is populated during active pause."""
    _set_ha_states(hass, hub_entry)
    hass.data[DOMAIN]["hubs"][hub_entry.entry_id]["charging_mode"] = "Solar"

    sensor = DynamicOcppEvseChargerSensor(
        hass, charger_entry, hub_entry, "Test Charger", "test_charger", None
    )

    with patch("homeassistant.core.ServiceRegistry.async_call", new_callable=AsyncMock):
        await sensor.async_update()

    # Pause should be active with remaining seconds
    attrs = sensor.extra_state_attributes
    assert attrs["pause_active"] is True
    assert attrs["pause_remaining_seconds"] is not None
    assert attrs["pause_remaining_seconds"] > 0
    assert attrs["pause_remaining_seconds"] <= 180  # Default pause duration


async def test_auto_reset_skips_when_car_not_plugged_in(
    hass,
    hub_entry,
    charger_entry,
    setup_domain_data,
):
    """Test that auto-reset check is skipped when connector is Available."""
    _set_ha_states(hass, hub_entry)
    # Car not plugged in
    hass.states.async_set("sensor.test_charger_status_connector", "Available")

    sensor = DynamicOcppEvseChargerSensor(
        hass, charger_entry, hub_entry, "Test Charger", "test_charger", None
    )
    sensor._mismatch_count = 10  # Would normally trigger
    sensor._last_commanded_limit = 16.0

    with patch("homeassistant.core.ServiceRegistry.async_call", new_callable=AsyncMock) as mock_call:
        await sensor.async_update()

        # Counter should be reset (car not plugged in)
        assert sensor._mismatch_count == 0, (
            "Mismatch count should reset when car not plugged in"
        )

        # No reset should have been triggered
        reset_calls = [
            c for c in mock_call.call_args_list
            if len(c[0]) >= 2 and c[0][0] == DOMAIN and c[0][1] == "reset_ocpp_evse"
        ]
        assert len(reset_calls) == 0


async def test_eco_mode_night_with_feedback_loop(
    hass,
    hub_entry,
    charger_entry,
    setup_domain_data,
):
    """Test Eco mode at night gives min_current, not inflated solar surplus.

    Reproduces the real-world bug where Eco mode targeted 11.2A at night instead
    of the expected 6A (min_current). Root cause: solar_production_total was
    derived from ORIGINAL consumption (before charger subtraction), but the
    engine's solar surplus calculation used ADJUSTED consumption. This created
    a fake surplus equal to the charger's own draw.

    With the fix, solar_production_total is recalculated after the feedback loop
    adjustment, so the surplus is correctly near zero at night.
    """
    from custom_components.dynamic_ocpp_evse.dynamic_ocpp_evse import (
        run_hub_calculation,
    )

    _set_ha_states(hass, hub_entry)
    # Night scenario: high consumption (includes charger draws), no export
    # Grid reads ~15A/phase import (consumption ~15A, export 0A)
    hass.states.async_set(
        "sensor.inverter_phase_a", "14.64",
        {"device_class": "current", "unit_of_measurement": "A"},
    )
    hass.states.async_set(
        "sensor.inverter_phase_b", "13.26",
        {"device_class": "current", "unit_of_measurement": "A"},
    )
    hass.states.async_set(
        "sensor.inverter_phase_c", "18.43",
        {"device_class": "current", "unit_of_measurement": "A"},
    )
    # Charger drawing ~10A on all 3 phases
    hass.states.async_set(
        "sensor.test_charger_current_import", "9.8",
        {
            "l1_current": 9.8,
            "l2_current": 9.8,
            "l3_current": 9.8,
            "device_class": "current",
            "unit_of_measurement": "A",
        },
    )
    # No battery (night, typical for non-battery setups)
    hass.states.async_set("sensor.battery_soc", "unknown")
    hass.states.async_set("sensor.battery_power", "unknown")
    # Eco mode
    hass.data[DOMAIN]["hubs"][hub_entry.entry_id]["charging_mode"] = "Eco"

    sensor = DynamicOcppEvseChargerSensor(
        hass, charger_entry, hub_entry, "Test Charger", "test_charger", None
    )

    result = run_hub_calculation(sensor)

    charger_targets = result.get("charger_targets", {})
    target = charger_targets.get(charger_entry.entry_id, 0)

    # Eco mode at night: no solar, so target should be min_current (6A)
    # NOT 11.2A (the bug value from fake solar surplus)
    assert target == 6.0, (
        f"Eco mode at night should give min_current (6A), got {target}A. "
        f"If >6A, solar_production_total was likely not recalculated after "
        f"feedback loop adjustment."
    )


async def test_dual_frequency_throttles_ocpp_commands(
    hass,
    hub_entry,
    charger_entry,
    setup_domain_data,
):
    """Test that site info refreshes on every cycle but OCPP commands are throttled.

    The sensor update loop runs at the fast site_update_frequency (default 5s),
    but OCPP set_charge_rate commands are only sent when the charger's
    update_frequency (default 15s) has elapsed.
    """
    _set_ha_states(hass, hub_entry)

    sensor = DynamicOcppEvseChargerSensor(
        hass, charger_entry, hub_entry, "Test Charger", "test_charger", None
    )

    with patch("homeassistant.core.ServiceRegistry.async_call", new_callable=AsyncMock) as mock_call:
        # First update: _last_command_time is 0, so command should fire
        await sensor.async_update()

        ocpp_calls = [
            c for c in mock_call.call_args_list
            if len(c[0]) >= 2 and c[0][0] == "ocpp" and c[0][1] == "set_charge_rate"
        ]
        assert len(ocpp_calls) == 1, (
            f"First update should send OCPP command, got {len(ocpp_calls)} calls"
        )

        # Verify hub_data was populated (site info refreshed)
        hub_entry_id = charger_entry.data.get("hub_entry_id")
        hub_data = hass.data.get(DOMAIN, {}).get("hub_data", {}).get(hub_entry_id, {})
        assert hub_data, "Hub data should be populated after first update"

        # Reset mock to count only new calls
        mock_call.reset_mock()

        # Second update immediately after: should be throttled (no OCPP command)
        # _last_command_time was just set, and update_frequency is 15s
        await sensor.async_update()

        ocpp_calls_2 = [
            c for c in mock_call.call_args_list
            if len(c[0]) >= 2 and c[0][0] == "ocpp" and c[0][1] == "set_charge_rate"
        ]
        assert len(ocpp_calls_2) == 0, (
            f"Second immediate update should be throttled, got {len(ocpp_calls_2)} OCPP calls"
        )

        # Verify hub_data was STILL refreshed (site info updates every cycle)
        hub_data_2 = hass.data.get(DOMAIN, {}).get("hub_data", {}).get(hub_entry_id, {})
        assert hub_data_2, "Hub data should still be populated on throttled cycle"
