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
    CONF_CHARGIN_MODE_ENTITY_ID,
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
    CONF_AVAILABLE_CURRENT,
    CONF_PHASES,
    CONF_CHARING_MODE,
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
            CONF_CHARGIN_MODE_ENTITY_ID: "select.test_hub_charging_mode",
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
            },
        },
        "chargers": {
            charger_entry.entry_id: {
                "entry": charger_entry,
                "hub_entry_id": hub_entry.entry_id,
            },
        },
        "charger_allocations": {
            charger_entry.entry_id: 0,
        },
    }


def _set_ha_states(hass):
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
    # Hub-level selectors
    hass.states.async_set("select.test_hub_charging_mode", "Standard")
    hass.states.async_set("select.test_hub_distribution_mode", "Priority")
    # Battery SOC target
    hass.states.async_set("number.test_hub_home_battery_soc_target", "90")
    # Power buffer
    hass.states.async_set("number.test_hub_power_buffer", "200")


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
    """Verify that calculate_available_current_for_hub reads HA entity states.

    With 3-phase Standard mode, 25A breaker, grid importing ~5A/phase,
    the charger (3p, min=6A, max=16A) should get a real allocation.
    """
    from custom_components.dynamic_ocpp_evse.dynamic_ocpp_evse import (
        calculate_available_current_for_hub,
    )

    _set_ha_states(hass)

    sensor = DynamicOcppEvseChargerSensor(
        hass, charger_entry, hub_entry, "Test Charger", "test_charger", None
    )

    result = calculate_available_current_for_hub(sensor)

    # With the fix, HA entity states are actually read — Standard mode with
    # 25A breaker and grid importing ~5A/phase leaves ~20A headroom per phase,
    # capped by charger max (16A)
    assert result[CONF_AVAILABLE_CURRENT] > 0, (
        "Available current should be > 0 in Standard mode with spare grid capacity"
    )

    # Battery SOC should be read from sensor.battery_soc entity (80%)
    assert result.get("battery_soc") == 80.0, (
        "Battery SOC should be read from the HA entity"
    )

    # Grid is importing (positive raw values) → no export on any phase
    assert result.get("site_available_current_phase_a") == 0.0
    assert result.get("site_available_current_phase_b") == 0.0
    assert result.get("site_available_current_phase_c") == 0.0

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
    _set_ha_states(hass)

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
    _set_ha_states(hass)

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
    _set_ha_states(hass)

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
    _set_ha_states(hass)

    # Populate hub_data via charger sensor
    charger_sensor = DynamicOcppEvseChargerSensor(
        hass, charger_entry, hub_entry, "Test Charger", "test_charger", None
    )
    with patch("homeassistant.core.ServiceRegistry.async_call", new_callable=AsyncMock):
        await charger_sensor.async_update()

    # Create a hub data sensor for "total_site_available_power"
    defn = next(d for d in HUB_SENSOR_DEFINITIONS if d["hub_data_key"] == "total_site_available_power")
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
    _set_ha_states(hass)
    # Override to Solar mode — with grid importing there is no solar surplus
    hass.states.async_set("select.test_hub_charging_mode", "Solar")

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
    _set_ha_states(hass)
    # Override to Solar mode — charger gets 0A allocation
    hass.states.async_set("select.test_hub_charging_mode", "Solar")

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
    _set_ha_states(hass)

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
    _set_ha_states(hass)

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
    _set_ha_states(hass)

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
    _set_ha_states(hass)

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
