"""Tests for the site update cycle.

These tests create actual sensor entity instances with mocked HA states and
drive one hub site cycle (`_run_site_cycle`, the hub coordinator's own update
function) to verify the data flow from HA entities through the calculation
engine to the sensor state and the device commands.
"""

import time
from unittest.mock import patch, AsyncMock, MagicMock
from datetime import datetime, timedelta, timezone

import pytest
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
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
    CONF_EVSE_CURRENT_IMPORT_ENTITY_ID,
    CONF_EVSE_CURRENT_IMPORT_L1_ENTITY_ID,
    CONF_EVSE_CURRENT_IMPORT_L2_ENTITY_ID,
    CONF_EVSE_CURRENT_IMPORT_L3_ENTITY_ID,
    CONF_EVSE_CURRENT_OFFERED_ENTITY_ID,
    CONF_EVSE_POWER_OFFERED_ENTITY_ID,
    CONF_PHASE_A_CURRENT_ENTITY_ID,
    CONF_PHASE_B_CURRENT_ENTITY_ID,
    CONF_PHASE_C_CURRENT_ENTITY_ID,
    CONF_MAIN_BREAKER_RATING,
    CONF_INVERT_PHASES,
    CONF_MAX_IMPORT_POWER_ENTITY_ID,
    CONF_PHASE_VOLTAGE,
    CONF_GRID_EXPORT_LIMIT,
    CONF_BATTERY_SOC_ENTITY_ID,
    CONF_BATTERY_POWER_ENTITY_ID,
    CONF_BATTERY_MAX_CHARGE_POWER,
    CONF_BATTERY_MAX_DISCHARGE_POWER,
    CONF_BATTERY_SOC_HYSTERESIS,
    CONF_BATTERY_SOC_TARGET_ENTITY_ID,
    CONF_ALLOW_GRID_CHARGING_ENTITY_ID,
    CONF_POWER_BUFFER_ENTITY_ID,
    CONF_LOAD_PRIORITY,
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
    DEFAULT_MIN_CHARGE_CURRENT,
    DEFAULT_MAX_CHARGE_CURRENT,
    DEFAULT_PHASE_VOLTAGE,
    DEFAULT_MAIN_BREAKER_RATING,
    DEFAULT_BATTERY_MAX_POWER,
    DEFAULT_BATTERY_SOC_HYSTERESIS,
    DEFAULT_CHARGE_PAUSE_DURATION,
    CONF_GRID_EXPORT_LIMIT,
    CONF_EXCESS_TRIGGER_MARGIN,
    CONF_SOLAR_PRODUCTION_ENTITY_ID,
    CONF_SOLAR_FORECAST_ENTITY_IDS,
    CONF_BASE_CONSUMPTION,
    CONF_BATTERY_CAPACITY_KWH,
    CONF_FORECAST_SOC_FLOOR,
    ENTRY_TYPE_INVERTER,
    CONF_CHARGE_LIMIT_ENTITY_ID,
    CONF_CHARGE_LIMIT_UNIT,
    CONF_CHARGE_LIMIT_MINIMUM,
    CONF_CHARGE_CONTROL_INTERVAL,
    CONF_BATTERY_NOMINAL_VOLTAGE,
    CHARGE_LIMIT_UNIT_AMPS,
    CHARGE_LIMIT_UNIT_WATTS,
    INVERTER_RT_APPLIED,
    INVERTER_RT_CONTROL_ENABLED,
    INVERTER_RT_ENFORCED_CHARGE_W,
    INVERTER_RT_LAST_WRITE,
    CONF_SOC_LIMIT_ENTITY_IDS,
    CONF_SOC_LIMIT_NORMAL_ENTITY_ID,
    INVERTER_RT_SOC_CONTROL_ENABLED,
    INPUT_STALE_TIMEOUT,
)
from custom_components.dynamic_ocpp_evse.sensor import (
    LoadJugglerDeviceSensor,
    DynamicOcppEvseHubSensor,
    DynamicOcppEvseHubDataSensor,
    HUB_SENSOR_DEFINITIONS,
)
from custom_components.dynamic_ocpp_evse.entities.inverter import (
    LoadJugglerInverterChargeControlSensor,
    LoadJugglerInverterSocControlSensor,
)
from custom_components.dynamic_ocpp_evse.control.inverter import (
    CONTROL_STATE_IDLE,
    CONTROL_STATE_LIMITING,
    CONTROL_STATE_OFF,
    soc_targets,
)
from custom_components.dynamic_ocpp_evse.entities.mixins import SITE_CYCLE_WORKERS


# ── Fixtures ───────────────────────────────────────────────────────────


@pytest.fixture
def hub_entry() -> MockConfigEntry:
    """Hub config entry with full grid + battery configuration."""
    return MockConfigEntry(
        domain=DOMAIN,
        version=2,
        minor_version=2,
        title="Test Hub",
        data={
            CONF_NAME: "Test Hub",
            CONF_ENTITY_ID: "test_hub",
            ENTRY_TYPE: ENTRY_TYPE_HUB,
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
            CONF_GRID_EXPORT_LIMIT: 13500,
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
        minor_version=2,
        title="Test Charger",
        data={
            CONF_ENTITY_ID: "test_charger",
            CONF_NAME: "Test Charger",
            ENTRY_TYPE: ENTRY_TYPE_LOAD,
            CONF_CHARGER_ID: "test_charger",
            CONF_OCPP_DEVICE_ID: "ocpp_device_1",
            CONF_EVSE_CURRENT_IMPORT_ENTITY_ID: "sensor.test_charger_current_import",
            CONF_EVSE_CURRENT_OFFERED_ENTITY_ID: "sensor.test_charger_current_offered",
            CONF_HUB_ENTRY_ID: hub_entry.entry_id,
        },
        options={
            CONF_LOAD_PRIORITY: 1,
            CONF_EVSE_MINIMUM_CHARGE_CURRENT: 6,
            CONF_EVSE_MAXIMUM_CHARGE_CURRENT: 16,
            CONF_CHARGE_RATE_UNIT: "A",
            CONF_PROFILE_VALIDITY_MODE: "relative",
            CONF_UPDATE_FREQUENCY: 15,
            CONF_OCPP_PROFILE_TIMEOUT: 120,
            CONF_CHARGE_PAUSE_DURATION: 3,
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
                "loads": [charger_entry.entry_id],
                "distribution_mode": "Priority",
                "allow_grid_charging": True,
                "power_buffer": 0,
                "max_import_power": None,
                "battery_soc_target": 80,
                "battery_soc_min": 20,
            },
        },
        "loads": {
            charger_entry.entry_id: {
                "entry": charger_entry,
                "hub_entry_id": hub_entry.entry_id,
                "min_current": None,
                "max_current": None,
                "device_power": None,
                "dynamic_control": True,
            },
        },
        "load_allocations": {
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
    hub_data["distribution_mode"] = "Priority"
    hub_data["allow_grid_charging"] = True
    hub_data["power_buffer"] = 200
    hub_data["battery_soc_target"] = 90
    hub_data["battery_soc_min"] = 20


async def _run_site_cycle(hass, hub_entry, *load_sensors):
    """Drive one hub site cycle exactly as the hub's coordinator does.

    Registers the given load sensors as this hub's load processors (production
    does it from LoadJugglerDeviceSensor.async_added_to_hass), then runs the
    coordinator's update function: ONE site calculation, the auto-detect
    notifications, the hub_data republish, then each load processor in turn.
    Returns the published hub_data.
    """
    from custom_components.dynamic_ocpp_evse.sensor import async_run_hub_cycle

    processors = (
        hass.data.setdefault(DOMAIN, {})
        .setdefault("load_processors", {})
        .setdefault(hub_entry.entry_id, {})
    )
    for load_sensor in load_sensors:
        processors[load_sensor.config_entry.entry_id] = load_sensor

    return await async_run_hub_cycle(hass, hub_entry)


# ── Sensor creation tests ─────────────────────────────────────────────


async def test_charger_sensor_initializes(hass, hub_entry, charger_entry):
    """Test that the charger sensor initializes with correct attributes."""
    sensor = LoadJugglerDeviceSensor(
        hass, charger_entry, hub_entry, "Test Charger", "test_charger"
    )
    # Modern sensor API: the value is native_value, and unit/class/state_class
    # are declared as _attr_* rather than overridden properties.
    assert sensor.native_value is None
    assert sensor.native_unit_of_measurement == "A"
    assert sensor.device_class == SensorDeviceClass.CURRENT
    assert sensor.state_class == SensorStateClass.MEASUREMENT
    assert sensor.icon == "mdi:ev-station"
    assert sensor._pause_started_at is None
    # No cycle has processed this load, so it has no permit to report.
    assert sensor.available is False

    attrs = sensor.extra_state_attributes
    assert "state_class" not in attrs, (
        "state_class belongs on the entity, not in extra_state_attributes — "
        "HA reads it from the sensor's own property"
    )
    assert attrs["pause_active"] is False
    assert attrs["allocated_current"] is None
    assert attrs[CONF_HUB_ENTRY_ID] == hub_entry.entry_id


async def test_hub_sensor_initializes(hass, hub_entry):
    """Test that the hub sensor initializes with correct attributes."""
    sensor = DynamicOcppEvseHubSensor(hass, hub_entry, "Test Hub", "test_hub")
    # Unknown, NOT 0.0: 0 W of remaining site power is a real reading (the site
    # is at its limit) and must not stand in for "nothing calculated yet".
    assert sensor.native_value is None
    assert sensor.native_unit_of_measurement == "W"
    assert sensor.device_class == SensorDeviceClass.POWER
    assert sensor.state_class == SensorStateClass.MEASUREMENT
    # No hub_data published yet — the producer has never run.
    assert sensor.available is False
    assert "state_class" not in sensor.extra_state_attributes


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
        assert sensor.native_value is None  # No data yet
        assert sensor.native_unit_of_measurement == defn["unit"]
        assert sensor.device_class == defn.get("device_class")
        # W/A/% sensors are instantaneous readings (MEASUREMENT); the advisory
        # kWh forecast sensors override to ENERGY + TOTAL per their definitions
        # (HA rejects ENERGY + MEASUREMENT; TOTAL fits a rises-and-falls amount).
        assert sensor.state_class == defn.get(
            "state_class", SensorStateClass.MEASUREMENT
        )
        assert sensor.available is False  # nothing published yet


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
    from custom_components.dynamic_ocpp_evse.engine.hub_calculation import (
        run_hub_calculation,
    )

    _set_ha_states(hass, hub_entry)

    result = run_hub_calculation(hass, hub_entry)

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

    # Per-phase remaining current (grid + inverter share) sums to
    # Site Remaining Power / voltage.
    per_phase_sum = (
        result.get("available_current_a", 0)
        + result.get("available_current_b", 0)
        + result.get("available_current_c", 0)
    )
    assert per_phase_sum == pytest.approx(
        result["total_site_available_power"] / 230, abs=1.0
    )

    # Charger targets should contain our charger with a real allocation
    load_targets = result.get("load_targets", {})
    assert charger_entry.entry_id in load_targets, (
        "Charger should appear in load_targets"
    )
    assert load_targets[charger_entry.entry_id] > 0, (
        "Charger target should be > 0 in Standard mode with available capacity"
    )


# ── Charger sensor update cycle tests ─────────────────────────────────


async def test_charger_sensor_update_calls_ocpp(
    hass,
    hub_entry,
    charger_entry,
    setup_domain_data,
):
    """Test that a site cycle sends an OCPP set_charge_rate service call."""
    _set_ha_states(hass, hub_entry)

    sensor = LoadJugglerDeviceSensor(
        hass, charger_entry, hub_entry, "Test Charger", "test_charger"
    )

    # Mock the OCPP service call — we don't have a real OCPP integration
    with patch("homeassistant.core.ServiceRegistry.async_call", new_callable=AsyncMock) as mock_call:
        await _run_site_cycle(hass, hub_entry, sensor)

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
    """Test that a site cycle populates hass.data hub_data for the hub sensors."""
    _set_ha_states(hass, hub_entry)

    sensor = LoadJugglerDeviceSensor(
        hass, charger_entry, hub_entry, "Test Charger", "test_charger"
    )

    with patch("homeassistant.core.ServiceRegistry.async_call", new_callable=AsyncMock):
        await _run_site_cycle(hass, hub_entry, sensor)

    # Hub data should now be populated
    hub_data = hass.data[DOMAIN].get("hub_data", {}).get(hub_entry.entry_id, {})
    assert hub_data, "hub_data should be populated after charger sensor update"
    assert "last_update" in hub_data
    assert "total_site_available_power" in hub_data


async def test_charger_update_republishes_every_hub_sensor_key(
    hass,
    hub_entry,
    charger_entry,
    setup_domain_data,
):
    """Every HUB_SENSOR_DEFINITIONS key must survive the site cycle's republish.

    Regression test: the republish used to be a hand-written key list that
    silently dropped sensor keys (available_grid_current & co.), leaving
    those hub sensors permanently unknown.
    """
    _set_ha_states(hass, hub_entry)

    sensor = LoadJugglerDeviceSensor(
        hass, charger_entry, hub_entry, "Test Charger", "test_charger"
    )
    with patch("homeassistant.core.ServiceRegistry.async_call", new_callable=AsyncMock):
        await _run_site_cycle(hass, hub_entry, sensor)

    hub_data = hass.data[DOMAIN].get("hub_data", {}).get(hub_entry.entry_id, {})
    missing = [
        d["hub_data_key"] for d in HUB_SENSOR_DEFINITIONS if d["hub_data_key"] not in hub_data
    ]
    assert not missing, f"hub sensor keys dropped by the load republish: {missing}"


# ── Site cycle: one calculation per hub, whatever the load count ──────


def _extra_charger_entry(hub_entry, suffix):
    """Another EVSE on the same hub, identical apart from its identity."""
    return MockConfigEntry(
        domain=DOMAIN,
        version=2,
        minor_version=2,
        title=f"Charger {suffix}",
        data={
            CONF_ENTITY_ID: f"charger_{suffix}",
            CONF_NAME: f"Charger {suffix}",
            ENTRY_TYPE: ENTRY_TYPE_LOAD,
            CONF_CHARGER_ID: f"charger_{suffix}",
            CONF_OCPP_DEVICE_ID: f"ocpp_device_{suffix}",
            CONF_EVSE_CURRENT_IMPORT_ENTITY_ID: "sensor.test_charger_current_import",
            CONF_EVSE_CURRENT_OFFERED_ENTITY_ID: "sensor.test_charger_current_offered",
            CONF_HUB_ENTRY_ID: hub_entry.entry_id,
        },
        options={
            CONF_LOAD_PRIORITY: 2,
            CONF_EVSE_MINIMUM_CHARGE_CURRENT: 6,
            CONF_EVSE_MAXIMUM_CHARGE_CURRENT: 16,
            CONF_CHARGE_RATE_UNIT: "A",
            CONF_PROFILE_VALIDITY_MODE: "relative",
            CONF_UPDATE_FREQUENCY: 15,
            CONF_OCPP_PROFILE_TIMEOUT: 120,
            CONF_CHARGE_PAUSE_DURATION: 3,
            CONF_STACK_LEVEL: 3,
        },
    )


async def test_one_calculation_per_cycle_with_three_loads(
    hass,
    hub_entry,
    charger_entry,
    setup_domain_data,
):
    """One site cycle = ONE engine run + each load processed exactly once.

    Regression for ISSUES.md #8: with a DataUpdateCoordinator per load, every
    load ran the whole site calculation, so all cycle-counted engine state
    (SETTLE_DRAW_CYCLES, the input EMAs, power_stable_count) advanced N times
    per real interval on an N-load site. This test fails on that architecture:
    three loads produced three engine runs per interval.
    """
    from custom_components.dynamic_ocpp_evse import sensor as sensor_module

    _set_ha_states(hass, hub_entry)

    load_sensors = [
        LoadJugglerDeviceSensor(
            hass, charger_entry, hub_entry, "Test Charger", "test_charger"
        )
    ]
    for suffix in ("b", "c"):
        extra = _extra_charger_entry(hub_entry, suffix)
        hass.data[DOMAIN]["loads"][extra.entry_id] = {
            "entry": extra,
            "hub_entry_id": hub_entry.entry_id,
            "min_current": None,
            "max_current": None,
            "device_power": None,
            "dynamic_control": True,
        }
        hass.data[DOMAIN]["hubs"][hub_entry.entry_id]["loads"].append(extra.entry_id)
        hass.data[DOMAIN]["load_allocations"][extra.entry_id] = 0
        load_sensors.append(
            LoadJugglerDeviceSensor(
                hass, extra, hub_entry, f"Charger {suffix}", f"charger_{suffix}"
            )
        )

    # Count processor runs without replacing the real behavior.
    process_counts = {}
    for load_sensor in load_sensors:
        real_process = load_sensor.async_process

        async def counted(hub_data, s=load_sensor, real_process=real_process):
            process_counts[s.config_entry.entry_id] = (
                process_counts.get(s.config_entry.entry_id, 0) + 1
            )
            await real_process(hub_data)

        load_sensor.async_process = counted

    engine_runs = []
    real_engine = sensor_module.run_hub_calculation

    def counted_engine(*args, **kwargs):
        engine_runs.append(args)
        return real_engine(*args, **kwargs)

    with patch.object(sensor_module, "run_hub_calculation", counted_engine), patch(
        "homeassistant.core.ServiceRegistry.async_call", new_callable=AsyncMock
    ):
        await _run_site_cycle(hass, hub_entry, *load_sensors)

    assert len(engine_runs) == 1, (
        f"one site cycle must run the calculation once, ran it {len(engine_runs)}x"
    )
    assert engine_runs[0] == (hass, hub_entry)
    assert process_counts == {
        load_sensor.config_entry.entry_id: 1 for load_sensor in load_sensors
    }, f"each load must be processed exactly once, got {process_counts}"
    # Really processed, not just called: every load carries a permit now.
    assert all(load_sensor._state is not None for load_sensor in load_sensors)


async def test_cycle_counted_engine_state_advances_once_per_cycle(
    hass,
    hub_entry,
    charger_entry,
    setup_domain_data,
):
    """The symptom behind ISSUES.md #8, asserted directly.

    `_settle_count` (the SETTLE_DRAW_CYCLES counter) advances once per ENGINE
    run. With a coordinator per load it advanced once per load per interval, so
    three loads reached the settle threshold three times too early and the
    engine freed a still-ramping car's gap to other loads. Two site cycles must
    leave the counter at 1, not 3.
    """
    _set_ha_states(hass, hub_entry)

    for suffix in ("b", "c"):
        extra = _extra_charger_entry(hub_entry, suffix)
        hass.data[DOMAIN]["loads"][extra.entry_id] = {
            "entry": extra,
            "hub_entry_id": hub_entry.entry_id,
            "dynamic_control": True,
        }
        hass.data[DOMAIN]["hubs"][hub_entry.entry_id]["loads"].append(extra.entry_id)
        hass.data[DOMAIN]["load_allocations"][extra.entry_id] = 0

    # Two cycles with an unchanged measured draw: the first seeds the counter,
    # the second is the first one that can increment it.
    await _run_site_cycle(hass, hub_entry)
    await _run_site_cycle(hass, hub_entry)

    counts = {
        entry_id: runtime.get("_settle_count")
        for entry_id, runtime in hass.data[DOMAIN]["loads"].items()
    }
    assert set(counts.values()) == {1}, (
        f"cycle-counted engine state must advance once per site cycle, got {counts}"
    )


async def test_site_cycle_publishes_hub_data_with_no_loads(
    hass,
    hub_entry,
    charger_entry,
    setup_domain_data,
):
    """A hub with zero loads still gets fresh hub_data every site cycle.

    This is what the hub sensor's own (deleted) self-calculating fallback
    existed for — a second engine writer with a different hub_data shape.
    The hub coordinator now covers it, with the published shape unchanged.
    """
    _set_ha_states(hass, hub_entry)
    hass.data[DOMAIN]["hubs"][hub_entry.entry_id]["loads"] = []
    hass.data[DOMAIN]["loads"] = {}

    published = await _run_site_cycle(hass, hub_entry)

    assert published is hass.data[DOMAIN]["hub_data"][hub_entry.entry_id]
    missing = [
        d["hub_data_key"]
        for d in HUB_SENSOR_DEFINITIONS
        if d["hub_data_key"] not in published
    ]
    assert not missing, f"hub sensor keys missing from the republish: {missing}"
    assert published["last_update"] is not None

    # And the hub sensor reads it without running anything itself.
    hub_sensor = DynamicOcppEvseHubSensor(hass, hub_entry, "Test Hub", "test_hub")
    await hub_sensor.async_update()
    assert hub_sensor._total_site_available_power is not None
    assert hub_sensor.native_value is not None
    # A cycle just published, so the producer is fresh.
    assert hub_sensor.available is True


async def test_hub_sensor_reads_hub_data(
    hass,
    hub_entry,
    charger_entry,
    setup_domain_data,
):
    """Test that hub sensor reads the values the site cycle published."""
    _set_ha_states(hass, hub_entry)

    # First: run a site cycle to populate hub_data
    charger_sensor = LoadJugglerDeviceSensor(
        hass, charger_entry, hub_entry, "Test Charger", "test_charger"
    )
    with patch("homeassistant.core.ServiceRegistry.async_call", new_callable=AsyncMock):
        await _run_site_cycle(hass, hub_entry, charger_sensor)

    # Then: run hub sensor update
    hub_sensor = DynamicOcppEvseHubSensor(hass, hub_entry, "Test Hub", "test_hub")
    await hub_sensor.async_update()

    # Hub sensor should have read the data — and publish it as its own value.
    assert hub_sensor._total_site_available_power is not None
    assert hub_sensor.native_value == round(
        hub_sensor._total_site_available_power, 0
    )
    assert hub_sensor.available is True


async def test_hub_data_sensor_reads_values(
    hass,
    hub_entry,
    charger_entry,
    setup_domain_data,
):
    """Test that individual hub data sensors read their specific values."""
    _set_ha_states(hass, hub_entry)

    # Populate hub_data via a site cycle
    charger_sensor = LoadJugglerDeviceSensor(
        hass, charger_entry, hub_entry, "Test Charger", "test_charger"
    )
    with patch("homeassistant.core.ServiceRegistry.async_call", new_callable=AsyncMock):
        await _run_site_cycle(hass, hub_entry, charger_sensor)

    # Create a hub data sensor for "grid_power"
    defn = next(d for d in HUB_SENSOR_DEFINITIONS if d["hub_data_key"] == "grid_power")
    data_sensor = DynamicOcppEvseHubDataSensor(hass, hub_entry, "Test Hub", "test_hub", defn)
    await data_sensor.async_update()

    # The sensor should have read from hub_data
    assert data_sensor.native_value is not None
    assert data_sensor.available is True


# ── Producer freshness: availability tracks the site cycle ────────────


async def test_site_remaining_power_is_unknown_not_zero_without_hub_data(
    hass, hub_entry
):
    """With no site cycle behind it, the hub sensor must not read 0 W.

    0 W of remaining power says "the site is at its limit" — an automation that
    sheds load on that number would act on a value nobody calculated. The
    sensor reports unknown, and unavailable on top, because its producer has
    never run.
    """
    hub_sensor = DynamicOcppEvseHubSensor(hass, hub_entry, "Test Hub", "test_hub")
    await hub_sensor.async_update()

    assert hub_sensor.native_value is None
    assert hub_sensor.native_value != 0.0
    assert hub_sensor.available is False


async def test_hub_sensors_go_unavailable_when_the_producer_goes_stale(
    hass,
    hub_entry,
    charger_entry,
    setup_domain_data,
):
    """A hub whose site cycle stopped takes its readers down with it.

    The values in hass.data are still there and still perfectly readable — that
    is exactly the trap. A sensor that keeps publishing the last engine result
    looks live, so the freshness gate is what turns "the engine died 10 minutes
    ago" into something a dashboard and an automation can both see.
    """
    _set_ha_states(hass, hub_entry)
    charger_sensor = LoadJugglerDeviceSensor(
        hass, charger_entry, hub_entry, "Test Charger", "test_charger"
    )
    with patch("homeassistant.core.ServiceRegistry.async_call", new_callable=AsyncMock):
        await _run_site_cycle(hass, hub_entry, charger_sensor)

    hub_sensor = DynamicOcppEvseHubSensor(hass, hub_entry, "Test Hub", "test_hub")
    defn = next(d for d in HUB_SENSOR_DEFINITIONS if d["hub_data_key"] == "grid_power")
    data_sensor = DynamicOcppEvseHubDataSensor(
        hass, hub_entry, "Test Hub", "test_hub", defn
    )
    await hub_sensor.async_update()
    await data_sensor.async_update()

    assert hub_sensor.available is True
    assert data_sensor.available is True
    last_reading = data_sensor.native_value

    # Age the publication past max(30 s, 3 x site_update_frequency) without
    # touching anything else — the producer stopped, the data did not move.
    hub_data = hass.data[DOMAIN]["hub_data"][hub_entry.entry_id]
    hub_data["last_update"] = datetime.now(timezone.utc) - timedelta(minutes=10)

    assert hub_sensor.available is False
    assert data_sensor.available is False
    # The last value is retained (it is what returns the moment a cycle runs
    # again) — availability, not the value, is what carries the staleness.
    assert data_sensor.native_value == last_reading


async def test_load_sensor_availability_follows_its_own_processing(
    hass,
    hub_entry,
    charger_entry,
    setup_domain_data,
):
    """The load's permit sensor is keyed on ITS last processed cycle.

    hub_data being fresh is not enough: a load the cycle never reached has no
    permit, and reporting the previous one would be a licence to draw power
    that was not granted this cycle.
    """
    _set_ha_states(hass, hub_entry)
    sensor = LoadJugglerDeviceSensor(
        hass, charger_entry, hub_entry, "Test Charger", "test_charger"
    )
    assert sensor.available is False  # never processed

    with patch("homeassistant.core.ServiceRegistry.async_call", new_callable=AsyncMock):
        await _run_site_cycle(hass, hub_entry, sensor)

    assert sensor._last_update is not None
    assert sensor.available is True

    # hub_data stays fresh; only this load's own processing goes stale.
    sensor._last_update = datetime.now(timezone.utc) - timedelta(minutes=10)
    assert sensor.available is False


async def test_site_cycle_adopts_readers_registered_before_the_hub(
    hass,
    hub_entry,
    charger_entry,
    setup_domain_data,
):
    """A reader that registered before its hub's coordinator existed gets bound.

    Child entries (groups, inverters, loads) can have their sensor platform set
    up before the hub's, so `hub_coordinators[hub_entry_id]` may be missing at
    the moment the entity is added. It registers in the site-cycle bucket
    anyway and the hub's next tick adopts it. The same pass re-binds readers
    left on a coordinator a hub reload replaced.
    """
    from custom_components.dynamic_ocpp_evse.entities.mixins import (
        SITE_CYCLE_LISTENERS,
        attach_site_cycle_listeners,
    )

    class _FakeCoordinator:
        def __init__(self):
            self.listeners = []

        def async_add_listener(self, cb):
            self.listeners.append(cb)
            return lambda: self.listeners.remove(cb)

    reader = DynamicOcppEvseHubDataSensor(
        hass,
        hub_entry,
        "Test Hub",
        "test_hub",
        next(d for d in HUB_SENSOR_DEFINITIONS if d["hub_data_key"] == "grid_power"),
    )
    hass.data[DOMAIN].setdefault(SITE_CYCLE_LISTENERS, {})[hub_entry.entry_id] = {
        id(reader): reader
    }

    first = _FakeCoordinator()
    attach_site_cycle_listeners(hass, hub_entry.entry_id, first)
    assert len(first.listeners) == 1
    assert reader._site_cycle_coordinator is first

    # Idempotent: the per-tick pass must not stack a listener every cycle.
    attach_site_cycle_listeners(hass, hub_entry.entry_id, first)
    assert len(first.listeners) == 1

    # A hub reload swaps the coordinator — the reader moves across, and does
    # not leave a subscription behind on the dead one.
    second = _FakeCoordinator()
    attach_site_cycle_listeners(hass, hub_entry.entry_id, second)
    assert first.listeners == []
    assert len(second.listeners) == 1
    assert reader._site_cycle_coordinator is second

    # No coordinator yet is a no-op, not a crash.
    attach_site_cycle_listeners(hass, hub_entry.entry_id, None)
    assert reader._site_cycle_coordinator is second


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
    # Override to Solar Only mode — with grid importing there is no solar surplus
    hass.data[DOMAIN]["loads"][charger_entry.entry_id]["operating_mode"] = "Solar Only"

    sensor = LoadJugglerDeviceSensor(
        hass, charger_entry, hub_entry, "Test Charger", "test_charger"
    )

    with patch("homeassistant.core.ServiceRegistry.async_call", new_callable=AsyncMock):
        await _run_site_cycle(hass, hub_entry, sensor)

    # In Solar Only mode with no export, charger gets 0A which is < min (6A)
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
    # Override to Solar Only mode — charger gets 0A allocation
    hass.data[DOMAIN]["loads"][charger_entry.entry_id]["operating_mode"] = "Solar Only"

    sensor = LoadJugglerDeviceSensor(
        hass, charger_entry, hub_entry, "Test Charger", "test_charger"
    )

    with patch("homeassistant.core.ServiceRegistry.async_call", new_callable=AsyncMock) as mock_call:
        await _run_site_cycle(hass, hub_entry, sensor)

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
        minor_version=2,
        title="No Device Charger",
        data={
            CONF_ENTITY_ID: "no_device_charger",
            CONF_NAME: "No Device",
            ENTRY_TYPE: ENTRY_TYPE_LOAD,
            CONF_CHARGER_ID: "no_device",
            # No CONF_OCPP_DEVICE_ID!
            CONF_EVSE_CURRENT_IMPORT_ENTITY_ID: "sensor.test_charger_current_import",
            CONF_EVSE_CURRENT_OFFERED_ENTITY_ID: "sensor.test_charger_current_offered",
            CONF_HUB_ENTRY_ID: hub_entry.entry_id,
        },
        options={
            CONF_LOAD_PRIORITY: 1,
            CONF_EVSE_MINIMUM_CHARGE_CURRENT: 6,
            CONF_EVSE_MAXIMUM_CHARGE_CURRENT: 16,
            CONF_CHARGE_RATE_UNIT: "A",
            CONF_PROFILE_VALIDITY_MODE: "relative",
            CONF_UPDATE_FREQUENCY: 15,
            CONF_OCPP_PROFILE_TIMEOUT: 120,
            CONF_CHARGE_PAUSE_DURATION: 3,
            CONF_STACK_LEVEL: 3,
        },
    )

    # Register in domain data
    hass.data[DOMAIN]["loads"][charger_entry_no_device.entry_id] = {
        "entry": charger_entry_no_device,
        "hub_entry_id": hub_entry.entry_id,
        "min_current": None,
        "max_current": None,
        "device_power": None,
        "dynamic_control": True,
    }
    hass.data[DOMAIN]["hubs"][hub_entry.entry_id]["loads"].append(
        charger_entry_no_device.entry_id
    )
    hass.data[DOMAIN]["load_allocations"][charger_entry_no_device.entry_id] = 0

    sensor = LoadJugglerDeviceSensor(
        hass, charger_entry_no_device, hub_entry, "No Device", "no_device_charger"
    )

    with patch("homeassistant.core.ServiceRegistry.async_call", new_callable=AsyncMock) as mock_call:
        await _run_site_cycle(hass, hub_entry, sensor)

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

    sensor = LoadJugglerDeviceSensor(
        hass, charger_entry, hub_entry, "Test Charger", "test_charger"
    )

    with patch("homeassistant.core.ServiceRegistry.async_call", new_callable=AsyncMock) as mock_call:
        await _run_site_cycle(hass, hub_entry, sensor)

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
        minor_version=2,
        title="Abs Charger",
        data={
            CONF_ENTITY_ID: "abs_charger",
            CONF_NAME: "Abs Charger",
            ENTRY_TYPE: ENTRY_TYPE_LOAD,
            CONF_CHARGER_ID: "abs_charger",
            CONF_OCPP_DEVICE_ID: "device_abs",
            CONF_EVSE_CURRENT_IMPORT_ENTITY_ID: "sensor.test_charger_current_import",
            CONF_EVSE_CURRENT_OFFERED_ENTITY_ID: "sensor.test_charger_current_offered",
            CONF_HUB_ENTRY_ID: hub_entry.entry_id,
        },
        options={
            CONF_LOAD_PRIORITY: 1,
            CONF_EVSE_MINIMUM_CHARGE_CURRENT: 6,
            CONF_EVSE_MAXIMUM_CHARGE_CURRENT: 16,
            CONF_CHARGE_RATE_UNIT: "A",
            CONF_PROFILE_VALIDITY_MODE: "absolute",
            CONF_UPDATE_FREQUENCY: 15,
            CONF_OCPP_PROFILE_TIMEOUT: 120,
            CONF_CHARGE_PAUSE_DURATION: 3,
            CONF_STACK_LEVEL: 3,
        },
    )

    hass.data[DOMAIN]["loads"][charger_absolute.entry_id] = {
        "entry": charger_absolute,
        "hub_entry_id": hub_entry.entry_id,
        "min_current": None,
        "max_current": None,
        "device_power": None,
        "dynamic_control": True,
    }
    hass.data[DOMAIN]["hubs"][hub_entry.entry_id]["loads"].append(
        charger_absolute.entry_id
    )
    hass.data[DOMAIN]["load_allocations"][charger_absolute.entry_id] = 0

    sensor = LoadJugglerDeviceSensor(
        hass, charger_absolute, hub_entry, "Abs Charger", "abs_charger"
    )

    with patch("homeassistant.core.ServiceRegistry.async_call", new_callable=AsyncMock) as mock_call:
        await _run_site_cycle(hass, hub_entry, sensor)

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
        minor_version=2,
        title="Watts Charger",
        data={
            CONF_ENTITY_ID: "watts_charger",
            CONF_NAME: "Watts Charger",
            ENTRY_TYPE: ENTRY_TYPE_LOAD,
            CONF_CHARGER_ID: "watts_charger",
            CONF_OCPP_DEVICE_ID: "device_watts",
            CONF_EVSE_CURRENT_IMPORT_ENTITY_ID: "sensor.test_charger_current_import",
            CONF_EVSE_CURRENT_OFFERED_ENTITY_ID: "sensor.test_charger_current_offered",
            CONF_HUB_ENTRY_ID: hub_entry.entry_id,
        },
        options={
            CONF_LOAD_PRIORITY: 1,
            CONF_EVSE_MINIMUM_CHARGE_CURRENT: 6,
            CONF_EVSE_MAXIMUM_CHARGE_CURRENT: 16,
            CONF_CHARGE_RATE_UNIT: "W",
            CONF_PROFILE_VALIDITY_MODE: "relative",
            CONF_UPDATE_FREQUENCY: 15,
            CONF_OCPP_PROFILE_TIMEOUT: 120,
            CONF_CHARGE_PAUSE_DURATION: 3,
            CONF_STACK_LEVEL: 3,
        },
    )

    hass.data[DOMAIN]["loads"][charger_watts.entry_id] = {
        "entry": charger_watts,
        "hub_entry_id": hub_entry.entry_id,
        "min_current": None,
        "max_current": None,
        "device_power": None,
        "dynamic_control": True,
    }
    hass.data[DOMAIN]["hubs"][hub_entry.entry_id]["loads"].append(
        charger_watts.entry_id
    )
    hass.data[DOMAIN]["load_allocations"][charger_watts.entry_id] = 0

    sensor = LoadJugglerDeviceSensor(
        hass, charger_watts, hub_entry, "Watts Charger", "watts_charger"
    )

    with patch("homeassistant.core.ServiceRegistry.async_call", new_callable=AsyncMock) as mock_call:
        await _run_site_cycle(hass, hub_entry, sensor)

        ocpp_calls = [
            c for c in mock_call.call_args_list
            if c[0][0] == "ocpp" and c[0][1] == "set_charge_rate"
        ]
        profile = ocpp_calls[0][0][2]["custom_profile"]
        assert profile["chargingSchedule"]["chargingRateUnit"] == "W"


# ── Hub status: name the unavailable sensor ──────────────────────────


async def test_hub_status_names_unavailable_sensor(
    hass,
    hub_entry,
    charger_entry,
    setup_domain_data,
):
    """An unavailable configured sensor is named in the status line itself,
    not just buried in the warnings attribute."""
    from custom_components.dynamic_ocpp_evse.engine.hub_calculation import (
        run_hub_calculation,
    )

    _set_ha_states(hass, hub_entry)
    # Knock out the battery power sensor.
    hass.states.async_set(
        "sensor.battery_power", "unavailable",
        {"device_class": "power", "unit_of_measurement": "W"},
    )

    result = run_hub_calculation(hass, hub_entry)

    # The status line names the sensor; the warning carries the entity_id.
    assert result["hub_status"].startswith("Sensor unavailable:")
    assert "Battery power sensor" in result["hub_status"]
    assert any(
        "sensor.battery_power" in w for w in result["hub_warnings"]
    )


# ── Cold-start grid failsafe: assumed for safety, never published ────


def _knock_out_grid_cts(hass):
    """Every grid CT unreadable, as on a cold start or an entry reload."""
    for entity_id in (
        "sensor.inverter_phase_a",
        "sensor.inverter_phase_b",
        "sensor.inverter_phase_c",
    ):
        hass.states.async_set(
            entity_id,
            "unavailable",
            {"device_class": "current", "unit_of_measurement": "A"},
        )


async def test_cold_start_grid_assumption_is_not_published_as_a_measurement(
    hass,
    hub_entry,
    charger_entry,
    setup_domain_data,
):
    """The failsafe drives the allocation; it must not become a reading.

    With the CTs unreadable and no EMA history, _resolve_grid_phases assumes
    every phase is loaded right up to the main breaker — deliberately, so a
    blind site hands out no grid headroom. That number is a safety fabrication,
    and publishing it painted a 3 x 25 A x 230 V = 17,250 W grid spike onto
    Current Grid Power, the recorder and long-term statistics on every reload.
    The allocation still runs on the assumption; the published grid
    MEASUREMENTS report nothing at all.
    """
    from custom_components.dynamic_ocpp_evse.engine.hub_calculation import (
        run_hub_calculation,
    )

    _set_ha_states(hass, hub_entry)
    _knock_out_grid_cts(hass)

    result = run_hub_calculation(hass, hub_entry)

    # --- The allocation side, unchanged: the worst case still binds. ---
    # Every phase is taken as loaded to the 25 A breaker, so the only grid
    # headroom left is the charger's own measured draw, which the feedback loop
    # hands back: post-feedback import (25-10) + 25 + 25 = 65 A = 14,950 W
    # against the 17,250 W import limit less the 200 W buffer -> 2,100 W.
    assert result["available_grid_power"] == 2100
    # The permit that leaves is well under the charger's 16 A maximum.
    assert result["load_targets"][charger_entry.entry_id] == 8.0
    assert result[CONF_TOTAL_ALLOCATED_CURRENT] == 8.0

    # --- The measurement side: None, not the fabrication. ---
    for key in ("grid_power", "total_export_power", "household_power"):
        assert result[key] is None, key
    # The exact spike the maintainer saw on the live site.
    assert result["grid_power"] != 3 * 25 * 230

    # A real reading resumes publication on the very next cycle.
    _set_ha_states(hass, hub_entry)
    recovered = run_hub_calculation(hass, hub_entry)
    assert recovered["grid_power"] is not None
    assert recovered["grid_power"] > 0
    assert recovered["total_export_power"] is not None
    assert recovered["household_power"] is not None


async def test_cold_start_hands_out_no_grid_sourced_permit(
    hass,
    hub_entry,
    charger_entry,
    setup_domain_data,
):
    """Pin the failsafe's whole purpose: a blind site allocates strictly less.

    The same site, the same sensors, the only difference being whether the CTs
    can be read. Nulling the published measurements must not loosen the
    allocation by a single amp.
    """
    from custom_components.dynamic_ocpp_evse.engine.hub_calculation import (
        run_hub_calculation,
    )

    _set_ha_states(hass, hub_entry)
    _knock_out_grid_cts(hass)
    blind = run_hub_calculation(hass, hub_entry)

    # Fresh hub runtime (no EMA history carried over) with healthy CTs, for
    # the comparison: a site that can see itself grants strictly more.
    hass.data[DOMAIN]["hubs"][hub_entry.entry_id].pop("_ema_inputs", None)
    hass.data[DOMAIN]["hubs"][hub_entry.entry_id].pop("grid_stale_since", None)
    _set_ha_states(hass, hub_entry)
    seeing = run_hub_calculation(hass, hub_entry)

    assert blind["available_grid_power"] < seeing["available_grid_power"]
    assert (
        blind["total_site_available_power"]
        < seeing["total_site_available_power"]
    )
    assert (
        blind["load_available"][charger_entry.entry_id]
        < seeing["load_available"][charger_entry.entry_id]
    )
    # And the blind cycle is the one publishing no grid measurement, so the
    # comparison is between a real reading and a deliberate silence.
    assert blind["grid_power"] is None
    assert seeing["grid_power"] is not None


async def test_a_held_grid_reading_still_publishes_after_a_dropout(
    hass,
    hub_entry,
    charger_entry,
    setup_domain_data,
):
    """A held EMA value is an estimate, not a fabrication — so it publishes.

    The distinction the whole fix rests on: the sensor died mid-run, so we know
    what the phase was doing moments ago and holding it is honest. Blanking the
    grid sensors through every brief CT dropout would be its own bug.
    """
    from custom_components.dynamic_ocpp_evse.engine.hub_calculation import (
        run_hub_calculation,
    )

    _set_ha_states(hass, hub_entry)
    healthy = run_hub_calculation(hass, hub_entry)
    assert healthy["grid_power"] is not None

    # Now the CTs drop out, with EMA history behind them.
    _knock_out_grid_cts(hass)
    held = run_hub_calculation(hass, hub_entry)

    assert held["grid_power"] is not None
    assert held["grid_power"] == pytest.approx(healthy["grid_power"], rel=0.05)
    assert held["household_power"] is not None


async def test_current_grid_power_sensor_reads_unknown_on_a_cold_start(
    hass,
    hub_entry,
    charger_entry,
    setup_domain_data,
):
    """Entity tier: None in hub_data reaches HA as `unknown`.

    Availability is not the mechanism here — the producer ran, on time, and
    reported honestly that it has no grid measurement. The state is unknown
    while the sensor stays available, which is exactly what keeps the fake
    spike out of the recorder.
    """
    _set_ha_states(hass, hub_entry)
    _knock_out_grid_cts(hass)

    charger_sensor = LoadJugglerDeviceSensor(
        hass, charger_entry, hub_entry, "Test Charger", "test_charger"
    )
    with patch("homeassistant.core.ServiceRegistry.async_call", new_callable=AsyncMock):
        await _run_site_cycle(hass, hub_entry, charger_sensor)

    defn = next(d for d in HUB_SENSOR_DEFINITIONS if d["hub_data_key"] == "grid_power")
    sensor = DynamicOcppEvseHubDataSensor(
        hass, hub_entry, "Test Hub", "test_hub", defn
    )
    await sensor.async_update()

    assert hass.data[DOMAIN]["hub_data"][hub_entry.entry_id]["grid_power"] is None
    assert sensor.native_value is None
    assert sensor.available is True

    # The first healthy cycle gives the sensor its reading.
    _set_ha_states(hass, hub_entry)
    with patch("homeassistant.core.ServiceRegistry.async_call", new_callable=AsyncMock):
        await _run_site_cycle(hass, hub_entry, charger_sensor)
    await sensor.async_update()
    assert sensor.native_value is not None


# ── Dead solar sensor: 0 W for the maths, nothing published ──────────


def _solar_hub(slug, solar_entity):
    """A hub whose own solar production sensor is its only fleet member.

    Measured solar (not derived), so ``solar_power`` is exactly that sensor's
    reading and ``household_power`` comes from the supply identity — the two
    published figures a fabricated 0 W would poison.
    """
    return MockConfigEntry(
        domain=DOMAIN,
        version=2,
        minor_version=2,
        title=f"Solar Hub {slug}",
        data={
            CONF_NAME: f"Solar Hub {slug}",
            CONF_ENTITY_ID: f"solar_hub_{slug}",
            ENTRY_TYPE: ENTRY_TYPE_HUB,
        },
        options={
            CONF_PHASE_A_CURRENT_ENTITY_ID: f"sensor.sl_{slug}_phase_a",
            CONF_MAIN_BREAKER_RATING: 25,
            CONF_PHASE_VOLTAGE: 230,
            CONF_SOLAR_PRODUCTION_ENTITY_ID: solar_entity,
        },
    )


def _set_solar_states(hass, slug, solar_entity, solar_state):
    """2 A of import on phase A, and whatever the solar sensor is doing."""
    hass.states.async_set(
        f"sensor.sl_{slug}_phase_a", "2.0",
        {"device_class": "current", "unit_of_measurement": "A"},
    )
    hass.states.async_set(
        solar_entity, solar_state,
        {"device_class": "power", "unit_of_measurement": "W"},
    )


async def test_a_dead_solar_sensor_publishes_no_production(hass):
    """Configured, unreadable, no history — 0 W internally, unknown outside.

    The one place that substitutes it (engine/readers.py) keeps handing 0 W to
    the calculation, because the household maths cannot take None and 0 W is
    the conservative figure. Publishing it painted a confident 0 W onto Current
    Solar Power — right at night, a lie in daylight, and in long-term
    statistics either way.

    Two hubs identical but for what their solar sensor says decide it: the one
    reading a real 0 W and the one that cannot be read at all must allocate
    identically (the internal figure really is the same 0 W), and differ only
    in what they publish.
    """
    from custom_components.dynamic_ocpp_evse.engine.hub_calculation import (
        run_hub_calculation,
    )

    dead = _solar_hub("dead", "sensor.sl_dead_pv")
    zero = _solar_hub("zero", "sensor.sl_zero_pv")
    hass.data[DOMAIN] = {
        "hubs": {dead.entry_id: {"loads": []}, zero.entry_id: {"loads": []}},
        "loads": {},
        "load_allocations": {},
    }
    _set_solar_states(hass, "dead", "sensor.sl_dead_pv", STATE_UNAVAILABLE)
    _set_solar_states(hass, "zero", "sensor.sl_zero_pv", "0")

    dead_result = run_hub_calculation(hass, dead)
    zero_result = run_hub_calculation(hass, zero)

    # --- The measurement side: nothing, rather than a confident 0 W. ---
    assert dead_result["solar_power"] is None
    # The household figure is the supply identity, which consumes solar — so it
    # carries the fabrication and goes with it.
    assert dead_result["household_power"] is None

    # --- The allocation side: byte-for-byte the same as a measured 0 W. ---
    assert zero_result["solar_power"] == 0
    for key in (
        "available_solar_power",
        "available_solar_current",
        "available_grid_power",
        "total_site_available_power",
        "available_current_a",
        "excess_available",
    ):
        assert dead_result[key] == zero_result[key], key


async def test_a_solar_reading_resumes_publication_on_the_first_cycle(hass):
    """The silence lasts exactly as long as the sensor is unreadable."""
    from custom_components.dynamic_ocpp_evse.engine.hub_calculation import (
        run_hub_calculation,
    )

    hub = _solar_hub("resume", "sensor.sl_resume_pv")
    hass.data[DOMAIN] = {
        "hubs": {hub.entry_id: {"loads": []}},
        "loads": {},
        "load_allocations": {},
    }
    _set_solar_states(hass, "resume", "sensor.sl_resume_pv", STATE_UNAVAILABLE)
    assert run_hub_calculation(hass, hub)["solar_power"] is None

    _set_solar_states(hass, "resume", "sensor.sl_resume_pv", "3000")
    recovered = run_hub_calculation(hass, hub)
    assert recovered["solar_power"] == 3000
    assert recovered["household_power"] is not None


async def test_a_held_solar_reading_still_publishes_after_a_dropout(hass):
    """A held EMA value is an estimate, not a fabrication — so it publishes.

    Same distinction the grid fix rests on: the sensor died moments ago and we
    know what it was reading, so holding it is honest. Blanking Current Solar
    Power through every brief dropout would be its own bug.
    """
    from custom_components.dynamic_ocpp_evse.engine.hub_calculation import (
        run_hub_calculation,
    )

    hub = _solar_hub("held", "sensor.sl_held_pv")
    hass.data[DOMAIN] = {
        "hubs": {hub.entry_id: {"loads": []}},
        "loads": {},
        "load_allocations": {},
    }
    _set_solar_states(hass, "held", "sensor.sl_held_pv", "4000")
    healthy = run_hub_calculation(hass, hub)
    assert healthy["solar_power"] == 4000

    _set_solar_states(hass, "held", "sensor.sl_held_pv", STATE_UNAVAILABLE)
    held = run_hub_calculation(hass, hub)
    assert held["solar_power"] == 4000
    assert held["household_power"] is not None


async def test_a_site_with_no_solar_sensor_is_unaffected(hass, hub_entry, charger_entry,
                                                         setup_domain_data):
    """No production sensor configured is not a fabrication.

    Such a site derives its production from the inverter output, or falls back
    to grid export plus the fleet's charging draw. Nothing there is invented,
    so the figure publishes exactly as it always did.
    """
    from custom_components.dynamic_ocpp_evse.engine.hub_calculation import (
        run_hub_calculation,
    )

    _set_ha_states(hass, hub_entry)
    result = run_hub_calculation(hass, hub_entry)

    assert result["solar_power"] is not None
    assert result["household_power"] is not None


def _solar_inverter(hub_entry, slug, solar_entity):
    return MockConfigEntry(
        domain=DOMAIN,
        version=2,
        minor_version=4,
        title=f"Inverter {slug}",
        data={
            CONF_NAME: f"Inverter {slug}",
            CONF_ENTITY_ID: f"lj_inv_{slug}",
            ENTRY_TYPE: ENTRY_TYPE_INVERTER,
            CONF_HUB_ENTRY_ID: hub_entry.entry_id,
        },
        options={CONF_SOLAR_PRODUCTION_ENTITY_ID: solar_entity},
    )


async def test_one_dead_inverter_keeps_its_sibling_publishing(hass):
    """Per-inverter honesty, fleet-total silence.

    Each inverter has a Solar Production sensor of its OWN, so a healthy
    member's real figure is a measurement worth keeping and only the dead
    member's device sensor reads unknown. The fleet total is a sum containing an
    invented term, which makes the whole sum fabricated — the same rule the
    grid phases follow, and here the true value is unknowable in BOTH
    directions (idle array or full output), which argues for silence rather
    than against it.
    """
    from custom_components.dynamic_ocpp_evse.engine.hub_calculation import (
        run_hub_calculation,
    )

    hub = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        minor_version=2,
        title="Fleet Hub",
        data={
            CONF_NAME: "Fleet Hub",
            CONF_ENTITY_ID: "fleet_hub",
            ENTRY_TYPE: ENTRY_TYPE_HUB,
        },
        options={
            CONF_PHASE_A_CURRENT_ENTITY_ID: "sensor.fl_phase_a",
            CONF_MAIN_BREAKER_RATING: 25,
            CONF_PHASE_VOLTAGE: 230,
        },
    )
    live = _solar_inverter(hub, "live", "sensor.fl_live_pv")
    dead = _solar_inverter(hub, "dead", "sensor.fl_dead_pv")
    live.add_to_hass(hass)
    dead.add_to_hass(hass)
    hass.data[DOMAIN] = {
        "hubs": {hub.entry_id: {"loads": []}},
        "loads": {},
        "load_allocations": {},
        "inverters": {},
    }
    hass.states.async_set(
        "sensor.fl_phase_a", "2.0",
        {"device_class": "current", "unit_of_measurement": "A"},
    )
    hass.states.async_set(
        "sensor.fl_live_pv", "3000",
        {"device_class": "power", "unit_of_measurement": "W"},
    )
    hass.states.async_set(
        "sensor.fl_dead_pv", STATE_UNAVAILABLE,
        {"device_class": "power", "unit_of_measurement": "W"},
    )

    result = run_hub_calculation(hass, hub)

    assert result["inverters"][live.entry_id]["solar_w"] == 3000
    assert result["inverters"][dead.entry_id]["solar_w"] is None
    assert result["solar_power"] is None


async def test_current_solar_power_clears_when_the_sensor_dies_mid_run(hass):
    """The mid-run case the grid fix could not reach — and the sensor hold.

    A solar sensor that dies at noon is held for INPUT_STALE_TIMEOUT (honest,
    published), and after that the stale guard substitutes its 0 W fallback:
    a mid-run None, which the grid keys could never produce. A hub data sensor
    that HELD its last value there would freeze 5,000 W of production onto a
    dark array — a stale reading that looks live, which is exactly what the
    suppression exists to prevent. It clears to unknown instead, and stays
    available: the producer ran and reported honestly that it has nothing.
    """
    from custom_components.dynamic_ocpp_evse.engine.hub_calculation import (
        run_hub_calculation,
    )
    from custom_components.dynamic_ocpp_evse.entities.hub import publish_hub_data

    hub = _solar_hub("midrun", "sensor.sl_midrun_pv")
    hass.data[DOMAIN] = {
        "hubs": {hub.entry_id: {"loads": []}},
        "loads": {},
        "load_allocations": {},
    }
    defn = next(
        d for d in HUB_SENSOR_DEFINITIONS if d["hub_data_key"] == "solar_power"
    )
    sensor = DynamicOcppEvseHubDataSensor(
        hass, hub, "Solar Hub", "solar_hub_midrun", defn
    )

    # Hours of healthy readings.
    _set_solar_states(hass, "midrun", "sensor.sl_midrun_pv", "5000")
    publish_hub_data(hass, hub.entry_id, run_hub_calculation(hass, hub))
    await sensor.async_update()
    assert sensor.native_value == 5000

    # The sensor dies. Within the timeout the held value still publishes.
    _set_solar_states(hass, "midrun", "sensor.sl_midrun_pv", STATE_UNAVAILABLE)
    publish_hub_data(hass, hub.entry_id, run_hub_calculation(hass, hub))
    await sensor.async_update()
    assert sensor.native_value == 5000

    # Past INPUT_STALE_TIMEOUT the guard gives up on the held value, and the
    # published figure goes with it.
    hub_runtime = hass.data[DOMAIN]["hubs"][hub.entry_id]
    hub_runtime["_input_stale_since"]["solar"] = (
        time.monotonic() - INPUT_STALE_TIMEOUT - 5
    )
    published = publish_hub_data(hass, hub.entry_id, run_hub_calculation(hass, hub))
    assert published["solar_power"] is None
    await sensor.async_update()
    assert sensor.native_value is None
    assert sensor.available is True

    # And a returning sensor publishes again on its very first reading (the
    # value ramps because the stale guard cleared the EMA — the point here is
    # that a number is being reported at all).
    _set_solar_states(hass, "midrun", "sensor.sl_midrun_pv", "4500")
    publish_hub_data(hass, hub.entry_id, run_hub_calculation(hass, hub))
    await sensor.async_update()
    assert sensor.native_value is not None
    assert sensor.native_value > 0


# ── Unreadable load monitor: 0 A for the loop, nothing published ─────


def _set_charger_import(hass, state, attrs=None):
    """Whatever the charger's Current Import sensor is doing this cycle."""
    hass.states.async_set(
        "sensor.test_charger_current_import",
        state,
        {
            "device_class": "current",
            "unit_of_measurement": "A",
            **(attrs or {}),
        },
    )


async def test_a_charging_car_with_a_dead_monitor_publishes_no_managed_power(
    hass,
    hub_entry,
    charger_entry,
    setup_domain_data,
):
    """The defect: a charging car could publish 0 W of Current Managed Power.

    Its Current Import sensor is unreadable, so the load carries 0 A into the
    cycle. That 0 is deliberately conservative for the feedback loop — which
    subtracts managed draws from the grid CTs, and must never subtract more
    than it can see — so it stays. What must not happen is publishing it: a car
    pulling 7 kW reported as 0 W, in the sensor and in long-term statistics.
    """
    from custom_components.dynamic_ocpp_evse.engine.hub_calculation import (
        run_hub_calculation,
    )

    _set_ha_states(hass, hub_entry)
    _set_charger_import(hass, STATE_UNAVAILABLE)  # the car is still "Charging"

    result = run_hub_calculation(hass, hub_entry)

    # --- The measurement side: nothing at all. ---
    assert result["total_evse_power"] is None
    assert result["load_draw"][charger_entry.entry_id] is None
    # Every household form nets the managed draw out, so it carries the same
    # fabrication — the car's kilowatts would sit inside the household figure.
    assert result["household_power"] is None

    # --- The allocation side keeps publishing, exactly as before. ---
    assert result["load_targets"][charger_entry.entry_id] > 0
    assert result["load_available"][charger_entry.entry_id] > 0
    assert result[CONF_TOTAL_ALLOCATED_CURRENT] > 0
    # The grid measurement is the raw CT reading and is untouched by any of it.
    assert result["grid_power"] is not None

    # --- The feedback loop's view: still the conservative 0 A. ---
    # A genuine 0 A reading gives byte-identical grid headroom, which is what
    # "the internal zero stays" means — nothing was handed back to the pools.
    _set_charger_import(
        hass, "0.0", {"l1_current": 0.0, "l2_current": 0.0, "l3_current": 0.0}
    )
    measured_zero = run_hub_calculation(hass, hub_entry)
    assert measured_zero["available_grid_power"] == result["available_grid_power"]
    # ...and a MEASURED 0 W publishes. Only the invented one is the bug.
    assert measured_zero["total_evse_power"] == 0
    assert measured_zero["household_power"] is not None


async def test_an_idle_charger_with_a_dead_monitor_still_publishes_zero(
    hass,
    hub_entry,
    charger_entry,
    setup_domain_data,
):
    """No car connected: 0 W is a fact, not a guess.

    An offline OCPP charger takes all of its sensors with it — status included
    — so this is the common case, and blanking Current Managed Power for every
    site with an idle charger would be a worse bug than the one being fixed.
    The engine books nothing for it and it reports itself inactive; both are
    things we know without a meter.
    """
    from custom_components.dynamic_ocpp_evse.engine.hub_calculation import (
        run_hub_calculation,
    )

    _set_ha_states(hass, hub_entry)
    _set_charger_import(hass, STATE_UNAVAILABLE)
    hass.states.async_set("sensor.test_charger_status_connector", "Available")

    result = run_hub_calculation(hass, hub_entry)

    assert result["load_targets"][charger_entry.entry_id] == 0
    assert result["total_evse_power"] == 0
    assert result["load_draw"][charger_entry.entry_id] == 0
    assert result["household_power"] is not None


async def test_a_healthy_monitor_resumes_managed_power_publication(
    hass,
    hub_entry,
    charger_entry,
    setup_domain_data,
):
    """The silence lasts exactly as long as the monitor is unreadable."""
    from custom_components.dynamic_ocpp_evse.engine.hub_calculation import (
        run_hub_calculation,
    )

    _set_ha_states(hass, hub_entry)
    _set_charger_import(hass, STATE_UNAVAILABLE)
    assert run_hub_calculation(hass, hub_entry)["total_evse_power"] is None

    _set_ha_states(hass, hub_entry)  # 10 A on L1 again
    recovered = run_hub_calculation(hass, hub_entry)
    assert recovered["total_evse_power"] == round(10.0 * 230, 0)
    assert recovered["load_draw"][charger_entry.entry_id] == 10.0
    assert recovered["household_power"] is not None


async def test_one_dead_phase_sensor_fabricates_the_whole_draw(
    hass,
    hub_entry,
    charger_entry,
    setup_domain_data,
):
    """Per-phase monitors: a partly-read draw is still a fabricated total.

    Two of three phase sensors read, the third is unreadable and silently
    contributes 0 A — which makes the sum look measured while it is short by
    whatever that phase is really pulling. There is no per-phase managed-power
    figure published to partial it out into, so the total goes.
    """
    from custom_components.dynamic_ocpp_evse.engine.hub_calculation import (
        run_hub_calculation,
    )

    per_phase = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        minor_version=2,
        title="Per-phase Charger",
        data={
            CONF_ENTITY_ID: "pp_charger",
            CONF_NAME: "Per-phase Charger",
            ENTRY_TYPE: ENTRY_TYPE_LOAD,
            CONF_CHARGER_ID: "pp_charger",
            CONF_EVSE_CURRENT_IMPORT_L1_ENTITY_ID: "sensor.pp_l1",
            CONF_EVSE_CURRENT_IMPORT_L2_ENTITY_ID: "sensor.pp_l2",
            CONF_EVSE_CURRENT_IMPORT_L3_ENTITY_ID: "sensor.pp_l3",
            CONF_HUB_ENTRY_ID: hub_entry.entry_id,
        },
        options={
            CONF_LOAD_PRIORITY: 1,
            CONF_EVSE_MINIMUM_CHARGE_CURRENT: 6,
            CONF_EVSE_MAXIMUM_CHARGE_CURRENT: 16,
        },
    )
    _set_ha_states(hass, hub_entry)
    hass.data[DOMAIN]["hubs"][hub_entry.entry_id]["loads"] = [per_phase.entry_id]
    hass.data[DOMAIN]["loads"] = {
        per_phase.entry_id: {
            "entry": per_phase,
            "hub_entry_id": hub_entry.entry_id,
            "dynamic_control": True,
        }
    }
    hass.states.async_set("sensor.pp_charger_status_connector", "Charging")
    for entity, value in (
        ("sensor.pp_l1", "10.0"),
        ("sensor.pp_l2", "10.0"),
        ("sensor.pp_l3", STATE_UNAVAILABLE),
    ):
        hass.states.async_set(
            entity, value,
            {"device_class": "current", "unit_of_measurement": "A"},
        )

    result = run_hub_calculation(hass, hub_entry, load_entries=[per_phase])

    assert result["total_evse_power"] is None
    assert result["load_draw"][per_phase.entry_id] is None
    # The engine still saw the two phases it could read — the internal draw is
    # untouched, so allocation and the feedback loop behave exactly as before.
    assert result["load_targets"][per_phase.entry_id] > 0

    # All three readable: the total publishes again.
    hass.states.async_set(
        "sensor.pp_l3", "10.0",
        {"device_class": "current", "unit_of_measurement": "A"},
    )
    healthy = run_hub_calculation(hass, hub_entry, load_entries=[per_phase])
    assert healthy["total_evse_power"] == round(30.0 * 230, 0)


async def test_current_managed_power_sensor_reads_unknown_mid_charge(
    hass,
    hub_entry,
    charger_entry,
    setup_domain_data,
):
    """Entity tier, and the mid-run hold: a monitor can die at any moment.

    Unlike the grid failsafe, this None is reachable mid-run — the car is
    charging and its meter simply stops answering. A sensor that HELD its last
    value would keep graphing 2.3 kW of managed power against a car whose draw
    is unknown, so the hub data sensors clear instead (entities/hub.py).
    """
    from custom_components.dynamic_ocpp_evse.entities.hub import publish_hub_data
    from custom_components.dynamic_ocpp_evse.engine.hub_calculation import (
        run_hub_calculation,
    )

    _set_ha_states(hass, hub_entry)
    defn = next(
        d for d in HUB_SENSOR_DEFINITIONS if d["hub_data_key"] == "total_evse_power"
    )
    sensor = DynamicOcppEvseHubDataSensor(
        hass, hub_entry, "Test Hub", "test_hub", defn
    )

    publish_hub_data(hass, hub_entry.entry_id, run_hub_calculation(hass, hub_entry))
    await sensor.async_update()
    assert sensor.native_value == round(10.0 * 230, 0)

    _set_charger_import(hass, STATE_UNAVAILABLE)
    published = publish_hub_data(
        hass, hub_entry.entry_id, run_hub_calculation(hass, hub_entry)
    )
    assert published["total_evse_power"] is None
    await sensor.async_update()
    assert sensor.native_value is None
    assert sensor.available is True

    _set_ha_states(hass, hub_entry)
    publish_hub_data(hass, hub_entry.entry_id, run_hub_calculation(hass, hub_entry))
    await sensor.async_update()
    assert sensor.native_value == round(10.0 * 230, 0)


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
    from custom_components.dynamic_ocpp_evse.engine.hub_calculation import (
        run_hub_calculation,
    )

    _set_ha_states(hass, hub_entry)

    result = run_hub_calculation(hass, hub_entry)

    # Every key that hub_data storage (sensor.py) reads must be present
    # in the result dict AND must not be None when entities are configured.
    expected_keys = {
        CONF_TOTAL_ALLOCATED_CURRENT,
        CONF_PHASES,
        "calc_used",
        "battery_soc",
        "battery_soc_min",
        "battery_soc_target",
        "battery_power",
        "available_battery_power",
        "available_current_a",
        "available_current_b",
        "available_current_c",
        "available_grid_current",
        "available_solar_current",
        "available_battery_current",
        "available_inverter_current",
        "total_site_available_power",
        "grid_power",
        "available_grid_power",
        "total_evse_power",
        "solar_power",
        "available_solar_power",
        "load_targets",
        "load_names",
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
    from custom_components.dynamic_ocpp_evse.engine.hub_calculation import (
        run_hub_calculation,
    )

    _set_ha_states(hass, hub_entry)

    result = run_hub_calculation(hass, hub_entry)

    # --- Grid / phase values ---
    assert result[CONF_PHASES] == 3
    # Per-phase remaining current = grid + inverter share; the three phases
    # sum to Site Remaining Power / voltage.
    per_phase_sum = (
        result["available_current_a"]
        + result["available_current_b"]
        + result["available_current_c"]
    )
    assert per_phase_sum == pytest.approx(
        result["total_site_available_power"] / 230, abs=1.0
    )
    # Total site available = grid import headroom + inverter-sourced power
    # (solar available + battery discharge available). Verified against the
    # individual components rather than a magic number.
    assert result["total_site_available_power"] == pytest.approx(
        result["available_grid_power"]
        + result["available_solar_power"]
        + result["available_battery_power"],
        abs=1.0,
    )
    # Net consumption: (5.0 + 4.5 + 3.8) * 230 ≈ 3059W
    assert 3000 < result["grid_power"] < 3200
    # Grid headroom: breaker ≈ 14191W,
    # capped by max_import (17050W) - post-feedback consumption
    # post-feedback: (0+4.5+3.8)*230 = 1909W → 17050-1909 = 15141W (no cap)
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

    # --- Grid sensor health ---
    assert result["grid_stale"] is False

    # --- Charger targets ---
    assert result["distribution_mode"] == "Priority"
    assert charger_entry.entry_id in result["load_targets"]
    assert result[CONF_TOTAL_ALLOCATED_CURRENT] > 0


async def test_allow_grid_charging_off_reduces_available(
    hass,
    hub_entry,
    charger_entry,
    setup_domain_data,
):
    """When allow_grid_charging switch is OFF, the grid contribution is removed
    so the charger target should be lower than when ON (inverter-only power)."""
    from custom_components.dynamic_ocpp_evse.engine.hub_calculation import (
        run_hub_calculation,
    )

    # First: run with grid charging ON
    _set_ha_states(hass, hub_entry)
    result_on = run_hub_calculation(hass, hub_entry)
    target_on = result_on["load_targets"].get(charger_entry.entry_id, 0)

    # Then: run with grid charging OFF
    hass.data[DOMAIN]["hubs"][hub_entry.entry_id]["allow_grid_charging"] = False
    result_off = run_hub_calculation(hass, hub_entry)
    target_off = result_off["load_targets"].get(charger_entry.entry_id, 0)

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
    from custom_components.dynamic_ocpp_evse.engine.hub_calculation import (
        run_hub_calculation,
    )

    _set_ha_states(hass, hub_entry)
    # Set a low grid power limit so it becomes the binding constraint
    hass.states.async_set("sensor.grid_power_limit", "6000")

    # Run with power buffer = 0
    hass.data[DOMAIN]["hubs"][hub_entry.entry_id]["power_buffer"] = 0
    result_no_buf = run_hub_calculation(hass, hub_entry)
    target_no_buf = result_no_buf["load_targets"].get(charger_entry.entry_id, 0)

    # Run with 2000W buffer → effective grid limit drops significantly
    hass.data[DOMAIN]["hubs"][hub_entry.entry_id]["power_buffer"] = 2000
    result_buf = run_hub_calculation(hass, hub_entry)
    target_buf = result_buf["load_targets"].get(charger_entry.entry_id, 0)

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
    """Test that ramp-up is capped by the smoothing pipeline (EMA + dead band + rate limit).

    With EMA_ALPHA=0.3, DEAD_BAND=0.3, site_update_frequency=2s, RAMP_UP_RATE=0.1 A/s:
    - Previous output was 6A, engine wants 16A.
    - EMA: 0.3*16 + 0.7*6 = 9.0A
    - Dead band: |9.0 - 6.0| = 3.0 > 0.3 → passes
    - Rate limit: max_up = 0.1 * 2 = 0.2A → capped at 6.2A
    """
    from custom_components.dynamic_ocpp_evse.const import RAMP_UP_RATE, DEFAULT_SITE_UPDATE_FREQUENCY

    _set_ha_states(hass, hub_entry)

    sensor = LoadJugglerDeviceSensor(
        hass, charger_entry, hub_entry, "Test Charger", "test_charger"
    )
    # Simulate previous cycle had output at 6A
    sensor._ema_current = 6.0
    sensor._rate_limited_current = 6.0
    sensor._prev_operating_mode = "Standard"
    sensor._prev_distribution_mode = "Priority"

    with patch("homeassistant.core.ServiceRegistry.async_call", new_callable=AsyncMock) as mock_call:
        await _run_site_cycle(hass, hub_entry, sensor)

        ocpp_calls = [
            c for c in mock_call.call_args_list
            if c[0][0] == "ocpp" and c[0][1] == "set_charge_rate"
        ]
        assert len(ocpp_calls) == 1
        profile = ocpp_calls[0][0][2]["custom_profile"]
        limit = profile["chargingSchedule"]["chargingSchedulePeriod"][0]["limit"]

        # Engine would allocate 16A (max), but smoothing pipeline caps the change
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
    """Test that ramp-down is capped by the smoothing pipeline.

    Previous output was 16A, engine wants 6A (Eco min).
    EMA pulls toward 6A, dead band passes, rate limit caps the per-cycle drop.
    """
    from custom_components.dynamic_ocpp_evse.const import RAMP_DOWN_RATE, DEFAULT_SITE_UPDATE_FREQUENCY

    _set_ha_states(hass, hub_entry)
    sensor = LoadJugglerDeviceSensor(
        hass, charger_entry, hub_entry, "Test Charger", "test_charger"
    )
    # Simulate previous cycle had output at 16A
    sensor._ema_current = 16.0
    sensor._rate_limited_current = 16.0
    sensor._prev_operating_mode = "Solar Priority"
    sensor._prev_distribution_mode = "Priority"

    # Solar Priority mode with battery SOC below target — engine gives min_current (6A)
    hass.data[DOMAIN]["loads"][charger_entry.entry_id]["operating_mode"] = "Solar Priority"
    hass.states.async_set("sensor.battery_soc", "50")
    hass.data[DOMAIN]["hubs"][hub_entry.entry_id]["battery_soc_target"] = 90

    with patch("homeassistant.core.ServiceRegistry.async_call", new_callable=AsyncMock) as mock_call:
        await _run_site_cycle(hass, hub_entry, sensor)

        ocpp_calls = [
            c for c in mock_call.call_args_list
            if c[0][0] == "ocpp" and c[0][1] == "set_charge_rate"
        ]
        assert len(ocpp_calls) == 1
        profile = ocpp_calls[0][0][2]["custom_profile"]
        limit = profile["chargingSchedule"]["chargingSchedulePeriod"][0]["limit"]

        # Engine wants 6A (eco min), but ramp-down caps the per-cycle drop
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
    """Test that smoothing is NOT applied when resuming from pause (0 → N).

    When _rate_limited_current is 0 (pause), the charger should jump directly
    to the calculated value without any smoothing or rate limiting.
    """
    _set_ha_states(hass, hub_entry)

    sensor = LoadJugglerDeviceSensor(
        hass, charger_entry, hub_entry, "Test Charger", "test_charger"
    )
    # Simulate coming out of pause — both EMA and rate_limited are 0
    sensor._ema_current = 0.0
    sensor._rate_limited_current = 0.0

    with patch("homeassistant.core.ServiceRegistry.async_call", new_callable=AsyncMock) as mock_call:
        await _run_site_cycle(hass, hub_entry, sensor)

        ocpp_calls = [
            c for c in mock_call.call_args_list
            if c[0][0] == "ocpp" and c[0][1] == "set_charge_rate"
        ]
        assert len(ocpp_calls) == 1
        profile = ocpp_calls[0][0][2]["custom_profile"]
        limit = profile["chargingSchedule"]["chargingSchedulePeriod"][0]["limit"]

        # Should jump directly to full allocation (16A max), not be smoothed
        assert limit > 1.5, (
            f"Resume from pause should NOT rate-limit, got {limit}A (would be tiny if limited)"
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

    sensor = LoadJugglerDeviceSensor(
        hass, charger_entry, hub_entry, "Test Charger", "test_charger"
    )
    # Simulate: last cycle we sent 16A
    sensor._last_commanded_limit = 16.0

    # But charger is offering 0A (stuck / ignoring us)
    hass.states.async_set(
        "sensor.test_charger_current_offered", "0.0",
        {"device_class": "current", "unit_of_measurement": "A"},
    )

    with patch("homeassistant.core.ServiceRegistry.async_call", new_callable=AsyncMock):
        await _run_site_cycle(hass, hub_entry, sensor)

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

    sensor = LoadJugglerDeviceSensor(
        hass, charger_entry, hub_entry, "Test Charger", "test_charger"
    )
    sensor._mismatch_count = 3  # Simulate prior mismatches
    sensor._last_commanded_limit = 16.0

    # Charger is offering 16A — matches what we sent
    hass.states.async_set(
        "sensor.test_charger_current_offered", "16.0",
        {"device_class": "current", "unit_of_measurement": "A"},
    )

    with patch("homeassistant.core.ServiceRegistry.async_call", new_callable=AsyncMock):
        await _run_site_cycle(hass, hub_entry, sensor)

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

    sensor = LoadJugglerDeviceSensor(
        hass, charger_entry, hub_entry, "Test Charger", "test_charger"
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
        await _run_site_cycle(hass, hub_entry, sensor)

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

    sensor = LoadJugglerDeviceSensor(
        hass, charger_entry, hub_entry, "Test Charger", "test_charger"
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
        await _run_site_cycle(hass, hub_entry, sensor)

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
    from custom_components.dynamic_ocpp_evse.engine.hub_calculation import (
        run_hub_calculation,
    )

    _set_ha_states(hass, hub_entry)

    result = run_hub_calculation(hass, hub_entry)

    # The charger draws 10A on L1 (from entity attributes).
    # Phase A grid reading is 5.0A import.
    # After subtracting: adjusted consumption = max(0, 5.0 - 10.0) = 0.0A
    # The engine should see 25A available on phase A (full breaker rating).
    # Charger target should still be 16A (max) since there's plenty of headroom.
    load_targets = result.get("load_targets", {})
    target = load_targets.get(charger_entry.entry_id, 0)
    assert target == 16.0, (
        f"With feedback loop fix, charger should get full 16A (max), got {target}A"
    )

    # The per-phase remaining current display (grid + inverter share) sums to
    # Site Remaining Power / voltage.
    per_phase_sum = (
        result["available_current_a"]
        + result["available_current_b"]
        + result["available_current_c"]
    )
    assert per_phase_sum == pytest.approx(
        result["total_site_available_power"] / 230, abs=1.0
    )


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
    from custom_components.dynamic_ocpp_evse.engine.hub_calculation import (
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

    result = run_hub_calculation(hass, hub_entry)

    # Phase A: consumption=5.0A, charger_l1=4.0A → adjusted=1.0A → headroom=24.0A
    # Phase B: consumption=4.5A, charger_l2=4.0A → adjusted=0.5A → headroom=24.5A
    # Phase C: consumption=3.8A, charger_l3=4.0A → adjusted=0.0A → headroom=25.0A
    # 3-phase charger gets min(24, 24.5, 25) = 16A (capped at max_current)
    load_targets = result.get("load_targets", {})
    target = load_targets.get(charger_entry.entry_id, 0)
    assert target == 16.0, (
        f"With feedback loop fix, charger should get 16A (max), got {target}A"
    )

    # Per-phase remaining current display (grid + inverter share) sums to
    # Site Remaining Power / voltage.
    per_phase_sum = (
        result["available_current_a"]
        + result["available_current_b"]
        + result["available_current_c"]
    )
    assert per_phase_sum == pytest.approx(
        result["total_site_available_power"] / 230, abs=1.0
    )


async def test_charge_pause_cancelled_on_charging_mode_change(
    hass,
    hub_entry,
    charger_entry,
    setup_domain_data,
):
    """Test that active charge pause is cancelled when user changes operating mode.

    Start in Solar Only mode (no surplus → pause starts), then switch to Standard mode.
    The pause should be cancelled immediately on the mode change.
    """
    _set_ha_states(hass, hub_entry)
    # Start in Solar Only mode — no export surplus → charger gets 0A → pause starts
    hass.data[DOMAIN]["loads"][charger_entry.entry_id]["operating_mode"] = "Solar Only"

    sensor = LoadJugglerDeviceSensor(
        hass, charger_entry, hub_entry, "Test Charger", "test_charger"
    )

    with patch("homeassistant.core.ServiceRegistry.async_call", new_callable=AsyncMock):
        # First update: Solar Only mode, no surplus → pause starts
        await _run_site_cycle(hass, hub_entry, sensor)
        assert sensor._pause_started_at is not None, "Pause should have started in Solar Only mode"
        assert sensor._prev_operating_mode == "Solar Only"

        # Switch to Standard mode
        hass.data[DOMAIN]["loads"][charger_entry.entry_id]["operating_mode"] = "Standard"

        # Second update: mode changed → pause should be cancelled
        await _run_site_cycle(hass, hub_entry, sensor)
        assert sensor._pause_started_at is None, (
            "Pause should be cancelled when operating mode changes from Solar Only to Standard"
        )
        assert sensor._prev_operating_mode == "Standard"


async def test_charge_pause_cancelled_on_distribution_mode_change(
    hass,
    hub_entry,
    charger_entry,
    setup_domain_data,
):
    """Test that active charge pause is cancelled when user changes distribution mode.

    Start in Solar Only mode (triggers pause), then change BOTH distribution mode AND
    operating mode to Standard. The mode change cancels the pause, and Standard
    mode provides enough current to prevent a new pause from starting.
    """
    _set_ha_states(hass, hub_entry)
    # Start in Solar Only mode — charger gets 0A → pause starts
    hass.data[DOMAIN]["loads"][charger_entry.entry_id]["operating_mode"] = "Solar Only"

    sensor = LoadJugglerDeviceSensor(
        hass, charger_entry, hub_entry, "Test Charger", "test_charger"
    )

    with patch("homeassistant.core.ServiceRegistry.async_call", new_callable=AsyncMock):
        # First update: Solar Only mode → pause starts
        await _run_site_cycle(hass, hub_entry, sensor)
        assert sensor._pause_started_at is not None, "Pause should have started"
        assert sensor._prev_distribution_mode == "Priority"

        # Switch distribution mode AND operating mode so charger gets current
        hass.data[DOMAIN]["hubs"][hub_entry.entry_id]["distribution_mode"] = "Shared"
        hass.data[DOMAIN]["loads"][charger_entry.entry_id]["operating_mode"] = "Standard"

        # Second update: distribution mode changed → pause cancelled,
        # Standard mode gives current → no new pause
        await _run_site_cycle(hass, hub_entry, sensor)
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
    hass.data[DOMAIN]["loads"][charger_entry.entry_id]["operating_mode"] = "Solar Only"

    sensor = LoadJugglerDeviceSensor(
        hass, charger_entry, hub_entry, "Test Charger", "test_charger"
    )

    with patch("homeassistant.core.ServiceRegistry.async_call", new_callable=AsyncMock):
        await _run_site_cycle(hass, hub_entry, sensor)

    # Pause should be active with remaining seconds
    attrs = sensor.extra_state_attributes
    assert attrs["pause_active"] is True
    assert attrs["pause_remaining_seconds"] is not None
    assert attrs["pause_remaining_seconds"] > 0
    assert attrs["pause_remaining_seconds"] <= 180  # Default pause duration (3 min = 180s)


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

    sensor = LoadJugglerDeviceSensor(
        hass, charger_entry, hub_entry, "Test Charger", "test_charger"
    )
    sensor._mismatch_count = 10  # Would normally trigger
    sensor._last_commanded_limit = 16.0

    with patch("homeassistant.core.ServiceRegistry.async_call", new_callable=AsyncMock) as mock_call:
        await _run_site_cycle(hass, hub_entry, sensor)

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
    from custom_components.dynamic_ocpp_evse.engine.hub_calculation import (
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
    # Solar Priority mode (was "Eco")
    hass.data[DOMAIN]["loads"][charger_entry.entry_id]["operating_mode"] = "Solar Priority"

    result = run_hub_calculation(hass, hub_entry)

    load_targets = result.get("load_targets", {})
    target = load_targets.get(charger_entry.entry_id, 0)

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

    The hub site cycle runs at the fast site_update_frequency (default 2s),
    but OCPP set_charge_rate commands are only sent when the charger's
    update_frequency (default 15s) has elapsed.
    """
    _set_ha_states(hass, hub_entry)

    sensor = LoadJugglerDeviceSensor(
        hass, charger_entry, hub_entry, "Test Charger", "test_charger"
    )

    with patch("homeassistant.core.ServiceRegistry.async_call", new_callable=AsyncMock) as mock_call:
        # First update: _last_command_time is 0, so command should fire
        await _run_site_cycle(hass, hub_entry, sensor)

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
        await _run_site_cycle(hass, hub_entry, sensor)

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


# ── Watts-profile compliance: phase-count regression (ISSUES.md #9) ─────


def _watts_power_offered_charger(hub_entry):
    """A Watts-unit charger that reports compliance via a power_offered entity.

    charger_id is "test_charger" so the derived connector-status entity matches
    the one _set_ha_states drives to "Charging". No current_offered entity is
    configured, forcing check_profile_compliance down the power_offered (W→A)
    decode path.
    """
    return MockConfigEntry(
        domain=DOMAIN,
        version=2,
        minor_version=2,
        title="Watts Power-Offered Charger",
        data={
            CONF_ENTITY_ID: "test_charger",
            CONF_NAME: "Test Charger",
            ENTRY_TYPE: ENTRY_TYPE_LOAD,
            CONF_CHARGER_ID: "test_charger",
            CONF_OCPP_DEVICE_ID: "test_charger",
            CONF_EVSE_CURRENT_IMPORT_ENTITY_ID: "sensor.test_charger_current_import",
            CONF_EVSE_POWER_OFFERED_ENTITY_ID: "sensor.test_charger_power_offered",
            CONF_HUB_ENTRY_ID: hub_entry.entry_id,
        },
        options={
            CONF_LOAD_PRIORITY: 1,
            CONF_EVSE_MINIMUM_CHARGE_CURRENT: 6,
            CONF_EVSE_MAXIMUM_CHARGE_CURRENT: 16,
            CONF_CHARGE_RATE_UNIT: "W",
            CONF_PROFILE_VALIDITY_MODE: "relative",
            CONF_UPDATE_FREQUENCY: 15,
            CONF_OCPP_PROFILE_TIMEOUT: 120,
            CONF_CHARGE_PAUSE_DURATION: 3,
            CONF_STACK_LEVEL: 3,
        },
    )


async def test_compliance_watts_decode_uses_car_active_phases(
    hass,
    hub_entry,
    setup_domain_data,
):
    """Regression for ISSUES.md #9: a 1-phase car on a 3-phase EVSE must not
    trip a false compliance mismatch.

    The command side (control/ocpp.py) encodes the W limit as
    A x V x _car_active_phases, so 16 A on a 1-phase car => 3680 W. The
    compliance decode must invert with the SAME factor. Decoding with the
    hardware _phases (3) instead yields 3680 / (230 x 3) = 5.3 A, a permanent
    ~10.7 A mismatch that drives the auto-reset loop.
    """
    from custom_components.dynamic_ocpp_evse.control.compliance import (
        check_profile_compliance,
    )

    _set_ha_states(hass, hub_entry)

    charger = _watts_power_offered_charger(hub_entry)
    sensor = LoadJugglerDeviceSensor(
        hass, charger, hub_entry, "Test Charger", "test_charger"
    )
    # 3-phase EVSE hardware, 1-phase car connected.
    sensor._phases = 3
    sensor._car_active_phases = 1
    sensor._last_commanded_limit = 16.0
    sensor._mismatch_count = 0

    # Charger reports back the commanded power: 16 A x 230 V x 1 phase = 3680 W.
    hass.states.async_set(
        "sensor.test_charger_power_offered", "3680",
        {"device_class": "power", "unit_of_measurement": "W"},
    )

    await check_profile_compliance(sensor, 16.0, True)

    assert sensor._mismatch_count == 0, (
        "1-phase car at 16A on a 3-phase EVSE reporting 3680W is compliant; "
        f"got mismatch_count={sensor._mismatch_count} (decode likely used the "
        "hardware phase count instead of _car_active_phases)"
    )


async def test_compliance_watts_decode_detects_real_mismatch(
    hass,
    hub_entry,
    setup_domain_data,
):
    """Positive control for #9: the W decode path is actually exercised and a
    genuine shortfall still increments the mismatch counter.

    Guards against the regression test passing only because an early return
    (connector idle / cooldown) zeroed the counter.
    """
    from custom_components.dynamic_ocpp_evse.control.compliance import (
        check_profile_compliance,
    )

    _set_ha_states(hass, hub_entry)

    charger = _watts_power_offered_charger(hub_entry)
    sensor = LoadJugglerDeviceSensor(
        hass, charger, hub_entry, "Test Charger", "test_charger"
    )
    sensor._phases = 3
    sensor._car_active_phases = 1
    sensor._last_commanded_limit = 16.0
    sensor._mismatch_count = 0

    # Charger only offers 1840 W = 8 A on one phase, half the commanded 16 A.
    hass.states.async_set(
        "sensor.test_charger_power_offered", "1840",
        {"device_class": "power", "unit_of_measurement": "W"},
    )

    await check_profile_compliance(sensor, 16.0, True)

    assert sensor._mismatch_count >= 1, (
        "A real 8A-vs-16A shortfall on the W decode path should increment the "
        f"mismatch counter; got mismatch_count={sensor._mismatch_count}"
    )


# ── PV clipping forecast: hub_runtime ratchet ─────────────────────────


async def test_forecast_max_soc_ratchet(hass):
    """The published max-SOC recommendation rises immediately when the forecast
    improves, but falls only past the FORECAST_SOC_HYSTERESIS band, so forecast
    refreshes don't chatter the advice.

    Fixed clock (freezegun) so the forecast blocks sit inside "the rest of
    today" regardless of when the test runs. Threshold = 5000 W export limit
    + 300 W base consumption = 5300 W; capacity 10 kWh, floor 30 %.
    """
    from freezegun import freeze_time
    from custom_components.dynamic_ocpp_evse.engine.hub_calculation import (
        run_hub_calculation,
    )

    hub = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        minor_version=2,
        title="Forecast Hub",
        data={
            CONF_NAME: "Forecast Hub",
            CONF_ENTITY_ID: "forecast_hub",
            ENTRY_TYPE: ENTRY_TYPE_HUB,
        },
        options={
            CONF_PHASE_A_CURRENT_ENTITY_ID: "sensor.fc_phase_a",
            CONF_MAIN_BREAKER_RATING: 25,
            CONF_PHASE_VOLTAGE: 230,
            CONF_BATTERY_SOC_ENTITY_ID: "sensor.fc_battery_soc",
            CONF_BATTERY_POWER_ENTITY_ID: "sensor.fc_battery_power",
            CONF_BATTERY_MAX_CHARGE_POWER: 5000,
            CONF_BATTERY_MAX_DISCHARGE_POWER: 5000,
            CONF_GRID_EXPORT_LIMIT: 5000,
            CONF_BASE_CONSUMPTION: 300,
            CONF_BATTERY_CAPACITY_KWH: 10,
            CONF_FORECAST_SOC_FLOOR: 30,
            # Legacy direct-sensor key (pre-device-selector) — still honored
            # at runtime, and this test doubles as coverage for that path.
            CONF_SOLAR_FORECAST_ENTITY_IDS: ["sensor.fc_forecast"],
        },
    )
    # Minimal runtime structures — the ratchet state must live in this dict
    # across calls, exactly as it does in production.
    hass.data[DOMAIN] = {
        "hubs": {hub.entry_id: {"loads": []}},
        "loads": {},
        "load_allocations": {},
    }

    hass.states.async_set(
        "sensor.fc_phase_a", "2.0",
        {"device_class": "current", "unit_of_measurement": "A"},
    )
    hass.states.async_set(
        "sensor.fc_battery_soc", "80",
        {"device_class": "battery", "unit_of_measurement": "%"},
    )
    hass.states.async_set(
        "sensor.fc_battery_power", "-500",
        {"device_class": "power", "unit_of_measurement": "W"},
    )

    def set_forecast(watts_by_hour):
        hass.states.async_set(
            "sensor.fc_forecast",
            "1.0",
            {
                "watts": {
                    f"2026-08-14T{10 + i:02d}:00:00+00:00": w
                    for i, w in enumerate(watts_by_hour)
                }
            },
        )

    with freeze_time("2026-08-14 08:00:00+00:00"):
        # 2 h at 10300 W = 5000 W over the threshold → 10 kWh, the whole pack:
        # raw ceiling 0 %, clamped to the 30 % floor.
        set_forecast([10300, 10300, 0])
        result = run_hub_calculation(hass, hub)
        assert result["forecast_clipped_kwh"] == 10.0
        assert result["forecast_battery_max_soc"] == 30

        # Forecast improves to 2.5 kWh → ceiling 75 %: rises immediately.
        set_forecast([7800, 0])
        result = run_hub_calculation(hass, hub)
        assert result["forecast_battery_max_soc"] == 75

        # Slightly worse (raw 74 %) — within the 2 % band, holds at 75.
        set_forecast([7900, 0])
        result = run_hub_calculation(hass, hub)
        assert result["forecast_battery_max_soc"] == 75

        # Clearly worse (raw 60 %) — beyond the band, falls.
        set_forecast([9300, 0])
        result = run_hub_calculation(hass, hub)
        assert result["forecast_battery_max_soc"] == 60


async def test_forecast_charge_limit_latch_round_trips_through_hub_runtime(hass):
    """The charge cap's latch state survives between cycles, so an integer SOC
    tick at the engage threshold cannot flap the published limit.

    Same rig as the ratchet test above: threshold 5300 W, 10 kWh pack, 5 kW
    charge cap. One hour 1000 W over the threshold = 1 kWh absorbable, so the
    ceiling is 90 % — engage at 88, release only below 86.
    """
    from freezegun import freeze_time
    from custom_components.dynamic_ocpp_evse.engine.hub_calculation import (
        run_hub_calculation,
    )

    hub = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        minor_version=2,
        title="Forecast Latch Hub",
        data={
            CONF_NAME: "Forecast Latch Hub",
            CONF_ENTITY_ID: "forecast_latch_hub",
            ENTRY_TYPE: ENTRY_TYPE_HUB,
        },
        options={
            CONF_PHASE_A_CURRENT_ENTITY_ID: "sensor.fl_phase_a",
            CONF_MAIN_BREAKER_RATING: 25,
            CONF_PHASE_VOLTAGE: 230,
            CONF_BATTERY_SOC_ENTITY_ID: "sensor.fl_battery_soc",
            CONF_BATTERY_POWER_ENTITY_ID: "sensor.fl_battery_power",
            CONF_BATTERY_MAX_CHARGE_POWER: 5000,
            CONF_BATTERY_MAX_DISCHARGE_POWER: 5000,
            CONF_GRID_EXPORT_LIMIT: 5000,
            CONF_BASE_CONSUMPTION: 300,
            CONF_BATTERY_CAPACITY_KWH: 10,
            CONF_FORECAST_SOC_FLOOR: 30,
            CONF_SOLAR_FORECAST_ENTITY_IDS: ["sensor.fl_forecast"],
        },
    )
    hub_runtime = {"loads": []}
    hass.data[DOMAIN] = {
        "hubs": {hub.entry_id: hub_runtime},
        "loads": {},
        "load_allocations": {},
    }

    hass.states.async_set(
        "sensor.fl_phase_a", "2.0",
        {"device_class": "current", "unit_of_measurement": "A"},
    )
    hass.states.async_set(
        "sensor.fl_battery_power", "-500",
        {"device_class": "power", "unit_of_measurement": "W"},
    )
    hass.states.async_set(
        "sensor.fl_forecast",
        "1.0",
        {"watts": {"2026-08-14T10:00:00+00:00": 6300, "2026-08-14T11:00:00+00:00": 0}},
    )

    def set_soc(soc):
        hass.states.async_set(
            "sensor.fl_battery_soc", str(soc),
            {"device_class": "battery", "unit_of_measurement": "%"},
        )

    with freeze_time("2026-08-14 08:00:00+00:00"):
        # SOC below the band: released, full rate, no latch.
        set_soc(80)
        result = run_hub_calculation(hass, hub)
        assert result["forecast_battery_max_soc"] == 90
        assert result["forecast_charge_limit_w"] == 5000
        assert hub_runtime["_forecast_charge_limiting"] is False

        # At the engage threshold (88 = 90 − 2): the cap engages, and with no
        # unexportable production the setpoint is 0 W.
        set_soc(88)
        result = run_hub_calculation(hass, hub)
        assert result["forecast_charge_limit_w"] == 0
        assert hub_runtime["_forecast_charge_limiting"] is True

        # The flap: one integer tick down. The single-threshold version released
        # here; the latch carried in hub_runtime holds, because 87 is still
        # inside the band (release is below 86).
        for soc in (87, 88, 87, 88):
            set_soc(soc)
            result = run_hub_calculation(hass, hub)
            assert result["forecast_charge_limit_w"] == 0, f"released at SOC {soc}"
            assert hub_runtime["_forecast_charge_limiting"] is True

        # A full band below the engage threshold — the latch lets go.
        set_soc(85)
        result = run_hub_calculation(hass, hub)
        assert result["forecast_charge_limit_w"] == 5000
        assert hub_runtime["_forecast_charge_limiting"] is False


def _anchor_hub(slug, export_limit, trigger_margin):
    """A forecast hub differing only in its export limit and trigger margin.

    Measured solar (not derived) so the advice is a clean function of the
    reading; 10 kWh pack, 5 kW charge rating, 300 W base consumption.
    """
    return MockConfigEntry(
        domain=DOMAIN,
        version=2,
        minor_version=2,
        title=f"Anchor Hub {slug}",
        data={
            CONF_NAME: f"Anchor Hub {slug}",
            CONF_ENTITY_ID: f"anchor_hub_{slug}",
            ENTRY_TYPE: ENTRY_TYPE_HUB,
        },
        options={
            CONF_PHASE_A_CURRENT_ENTITY_ID: "sensor.an_phase_a",
            CONF_MAIN_BREAKER_RATING: 25,
            CONF_PHASE_VOLTAGE: 230,
            CONF_SOLAR_PRODUCTION_ENTITY_ID: "sensor.an_solar",
            CONF_BATTERY_SOC_ENTITY_ID: "sensor.an_battery_soc",
            CONF_BATTERY_POWER_ENTITY_ID: "sensor.an_battery_power",
            CONF_BATTERY_MAX_CHARGE_POWER: 5000,
            CONF_BATTERY_MAX_DISCHARGE_POWER: 5000,
            CONF_GRID_EXPORT_LIMIT: export_limit,
            CONF_EXCESS_TRIGGER_MARGIN: trigger_margin,
            CONF_BASE_CONSUMPTION: 300,
            CONF_BATTERY_CAPACITY_KWH: 10,
            CONF_FORECAST_SOC_FLOOR: 30,
            CONF_SOLAR_FORECAST_ENTITY_IDS: ["sensor.an_forecast"],
        },
    )


def _set_anchor_states(hass, solar_w, forecast_w, export_w=0.0, battery_w=500.0):
    """The plant this rig reads: measured export, and what the pack is taking.

    ``export_w`` is watts leaving the meter (so the CT reading is negative) and
    ``battery_w`` is watts the pack is absorbing (so the power sensor is
    negative — positive is discharging). Those two figures ARE the engaged
    advice's inputs; the solar reading no longer enters it at all.
    """
    hass.states.async_set(
        "sensor.an_phase_a", str(-export_w / 230.0),
        {"device_class": "current", "unit_of_measurement": "A"},
    )
    hass.states.async_set(
        "sensor.an_solar", str(solar_w),
        {"device_class": "power", "unit_of_measurement": "W"},
    )
    # SOC 88 sits on the engage threshold of the 90 % ceiling below, so the
    # charge cap is engaged in every one of these cases.
    hass.states.async_set(
        "sensor.an_battery_soc", "88",
        {"device_class": "battery", "unit_of_measurement": "%"},
    )
    hass.states.async_set(
        "sensor.an_battery_power", str(-battery_w),
        {"device_class": "power", "unit_of_measurement": "W"},
    )
    hass.states.async_set(
        "sensor.an_forecast",
        "1.0",
        {
            "watts": {
                "2026-08-14T10:00:00+00:00": forecast_w,
                "2026-08-14T11:00:00+00:00": 0,
            }
        },
    )


async def test_forecast_charge_limit_steers_to_a_setpoint_a_margin_under_the_limit(hass):
    """The engaged advice steers export to (export limit − trigger margin),
    while the clipping INTEGRAL stays on the true (export limit + base) energy
    threshold.

    Regression for the masked-site bug: on an inverter that hard-enforces the
    export limit, a controller whose setpoint IS the limit can never see an
    error, so its permit freezes while real kilowatts are curtailed (see
    ``recommended_charge_limit``'s docstring and the closed-loop replays in
    test_forecast_clipping.py). A margin under the limit, the pinned error is
    exactly that margin and the permit creeps out.

    Two hubs identical but for the trigger margin — 0 W (the degenerate
    setpoint, exactly at the limit) and 800 W — decide it, with the meter pinned
    at the 5 kW limit and the pack taking 500 W: the advice must differ by
    EXACTLY the margin, and the clipped-energy figure must not move at all.
    """
    from freezegun import freeze_time
    from custom_components.dynamic_ocpp_evse.engine.hub_calculation import (
        run_hub_calculation,
    )

    at_limit = _anchor_hub("atlimit", 5000, 0)
    shifted = _anchor_hub("shifted", 5000, 800)
    hass.data[DOMAIN] = {
        "hubs": {
            at_limit.entry_id: {"loads": []},
            shifted.entry_id: {"loads": []},
        },
        "loads": {},
        "load_allocations": {},
    }
    # 1 h at 6300 W is 1 kWh above the true 5300 W threshold, so the ceiling is
    # 90 % and SOC 88 engages the cap. Export pinned at the 5 kW wall.
    _set_anchor_states(hass, 6000, 6300, export_w=5000.0, battery_w=500.0)

    with freeze_time("2026-08-14 08:00:00+00:00"):
        at_limit_result = run_hub_calculation(hass, at_limit)
        shifted_result = run_hub_calculation(hass, shifted)

    # The integral is the ENERGY question and is asked at the true threshold —
    # the same 1 kWh whatever the trigger margin, so the reserved headroom is
    # untouched by this setpoint.
    assert at_limit_result["forecast_clipped_kwh"] == 1.0
    assert shifted_result["forecast_clipped_kwh"] == 1.0
    assert at_limit_result["forecast_absorbable_kwh"] == 1.0
    assert shifted_result["forecast_absorbable_kwh"] == 1.0
    assert at_limit_result["forecast_battery_max_soc"] == 90
    assert shifted_result["forecast_battery_max_soc"] == 90

    # The advice is the POWER question, and it is battery + (export − setpoint):
    #   500 + (5000 − (5000 − 0))   =  500 W — the degenerate case, frozen
    #   500 + (5000 − (5000 − 800)) = 1300 W — one margin of escape per cycle
    assert at_limit_result["forecast_charge_limit_w"] == 500
    assert shifted_result["forecast_charge_limit_w"] == 1300
    # The two setpoints diverge by exactly the trigger margin.
    assert (
        shifted_result["forecast_charge_limit_w"]
        - at_limit_result["forecast_charge_limit_w"]
        == 800
    )


async def test_the_export_setpoint_never_goes_below_zero(hass):
    """Edge: a trigger margin larger than the export limit clamps at 0 W.

    The setpoint is ``max(export limit − margin, 0)`` — watts at the meter — so
    a tiny export limit cannot push it negative and inflate the advice by the
    difference. Base consumption is not in this path at all any more.
    """
    from freezegun import freeze_time
    from custom_components.dynamic_ocpp_evse.engine.hub_calculation import (
        run_hub_calculation,
    )

    hub = _anchor_hub("clamped", 400, 500)
    hass.data[DOMAIN] = {
        "hubs": {hub.entry_id: {"loads": []}},
        "loads": {},
        "load_allocations": {},
    }
    # True threshold 700 W; 1 h at 1700 W is 1 kWh clipped → ceiling 90 %.
    _set_anchor_states(hass, 1000, 1700, export_w=400.0, battery_w=500.0)

    with freeze_time("2026-08-14 08:00:00+00:00"):
        result = run_hub_calculation(hass, hub)

    assert result["forecast_clipped_kwh"] == 1.0
    assert result["forecast_battery_max_soc"] == 90
    # Setpoint clamped to 0: 500 + (400 − 0) = 900 W. Unclamped the setpoint
    # would be −100 W and the advice 1000 W.
    assert result["forecast_charge_limit_w"] == 900


# ── PV clipping forecast: the reserve is carved below the destination ──
#
# The battery's destination is the per-inverter "normal SOC ceiling source"
# entity — where that pack ends the day when the forecast says nothing. Carving
# the reserve out of 100 % on a site whose ceiling sits at 95 % reserves the top
# 5 % twice, and the battery meets the peak with 5 % of room instead of the
# reserve. A site that configures no ceiling source anchors at 100 as before.


def _destination_hub(slug, base=300):
    """A forecast hub whose battery (and its ceiling source) is an inverter entry.

    Hub-level: the grid CT, the export limit and base consumption — clipping
    threshold 5300 W — and the forecast source. The battery belongs to the
    inverter entry, because that is where the SOC write-control and its normal
    ceiling source live.
    """
    return MockConfigEntry(
        domain=DOMAIN,
        version=2,
        minor_version=4,
        title=f"Destination Hub {slug}",
        data={
            CONF_NAME: f"Destination Hub {slug}",
            CONF_ENTITY_ID: f"destination_hub_{slug}",
            ENTRY_TYPE: ENTRY_TYPE_HUB,
        },
        options={
            CONF_PHASE_A_CURRENT_ENTITY_ID: "sensor.dst_phase_a",
            CONF_MAIN_BREAKER_RATING: 25,
            CONF_PHASE_VOLTAGE: 230,
            CONF_GRID_EXPORT_LIMIT: 5000,
            CONF_BASE_CONSUMPTION: base,
            CONF_FORECAST_SOC_FLOOR: 30,
            CONF_SOLAR_FORECAST_ENTITY_IDS: ["sensor.dst_forecast"],
        },
    )


def _destination_inverter(hub, slug, normal_entity=None, solar_entity=None):
    """That hub's inverter: a 20 kWh battery, optionally with a ceiling source."""
    options = {
        CONF_BATTERY_SOC_ENTITY_ID: "sensor.dst_battery_soc",
        CONF_BATTERY_POWER_ENTITY_ID: "sensor.dst_battery_power",
        CONF_BATTERY_MAX_CHARGE_POWER: 5000,
        CONF_BATTERY_MAX_DISCHARGE_POWER: 5000,
        CONF_BATTERY_CAPACITY_KWH: 20,
    }
    if normal_entity:
        options[CONF_SOC_LIMIT_NORMAL_ENTITY_ID] = normal_entity
    if solar_entity:
        options[CONF_SOLAR_PRODUCTION_ENTITY_ID] = solar_entity
    return MockConfigEntry(
        domain=DOMAIN,
        version=2,
        minor_version=4,
        title=f"Destination Inverter {slug}",
        data={
            CONF_NAME: f"Destination Inverter {slug}",
            CONF_ENTITY_ID: f"destination_inverter_{slug}",
            ENTRY_TYPE: ENTRY_TYPE_INVERTER,
            CONF_HUB_ENTRY_ID: hub.entry_id,
        },
        options=options,
    )


def _set_destination_states(hass, soc, normal="95"):
    hass.states.async_set(
        "sensor.dst_phase_a", "2.0",
        {"device_class": "current", "unit_of_measurement": "A"},
    )
    hass.states.async_set(
        "sensor.dst_battery_soc", str(soc),
        {"device_class": "battery", "unit_of_measurement": "%"},
    )
    hass.states.async_set(
        "sensor.dst_battery_power", "-500",
        {"device_class": "power", "unit_of_measurement": "W"},
    )
    hass.states.async_set(
        "number.dst_normal", normal, {"unit_of_measurement": "%"}
    )
    # One hour at 7300 W is 2 kWh above the 5300 W threshold, and the 5 kW
    # charge rating can take all of it: 2 kWh of a 20 kWh pack is 10 % of SOC.
    hass.states.async_set(
        "sensor.dst_forecast",
        "1.0",
        {
            "watts": {
                "2026-08-14T10:00:00+00:00": 7300,
                "2026-08-14T11:00:00+00:00": 0,
            }
        },
    )


async def test_forecast_max_soc_is_carved_below_the_configured_destination(hass):
    """Same site, same forecast, one difference: a 95 % ceiling source.

    2 kWh of a 20 kWh pack is 10 points of SOC, so the advice is 85 % where the
    battery is heading for 95 and 90 % where nothing says otherwise.
    """
    from freezegun import freeze_time
    from custom_components.dynamic_ocpp_evse.engine.hub_calculation import (
        run_hub_calculation,
    )

    with_dest = _destination_hub("with")
    without = _destination_hub("without")
    inv_with = _destination_inverter(with_dest, "with", "number.dst_normal")
    inv_without = _destination_inverter(without, "without")
    for entry in (inv_with, inv_without):
        entry.add_to_hass(hass)
    hass.data[DOMAIN] = {
        "hubs": {
            with_dest.entry_id: {"loads": []},
            without.entry_id: {"loads": []},
        },
        "loads": {},
        "load_allocations": {},
        "inverters": {},
    }
    _set_destination_states(hass, soc=70)

    with freeze_time("2026-08-14 08:00:00+00:00"):
        dest_result = run_hub_calculation(hass, with_dest)
        flat_result = run_hub_calculation(hass, without)

    # The ENERGY question is untouched by the anchor — the same 2 kWh either way.
    assert dest_result["forecast_absorbable_kwh"] == 2.0
    assert flat_result["forecast_absorbable_kwh"] == 2.0

    assert dest_result["forecast_battery_max_soc"] == 85
    assert flat_result["forecast_battery_max_soc"] == 90
    # And the per-inverter advice each device drives its own slots from.
    assert (
        dest_result["inverters"][inv_with.entry_id]["forecast_battery_max_soc"] == 85
    )


async def test_forecast_charge_gate_engages_relative_to_the_destination_advice(hass):
    """The gate follows the recommendation, so the destination moves it too.

    SOC 84 against the 85 % destination advice is inside the engage band
    (85 − 2); against the 90 % flat-anchored advice of the identical site it is
    not. Same battery, same forecast, same SOC — only the anchor differs.
    """
    from freezegun import freeze_time
    from custom_components.dynamic_ocpp_evse.engine.hub_calculation import (
        run_hub_calculation,
    )

    with_dest = _destination_hub("gate")
    without = _destination_hub("gateflat")
    inv_with = _destination_inverter(with_dest, "gate", "number.dst_normal")
    inv_without = _destination_inverter(without, "gateflat")
    for entry in (inv_with, inv_without):
        entry.add_to_hass(hass)
    dest_runtime = {"loads": []}
    flat_runtime = {"loads": []}
    hass.data[DOMAIN] = {
        "hubs": {
            with_dest.entry_id: dest_runtime,
            without.entry_id: flat_runtime,
        },
        "loads": {},
        "load_allocations": {},
        "inverters": {},
    }
    _set_destination_states(hass, soc=84)

    with freeze_time("2026-08-14 08:00:00+00:00"):
        dest_result = run_hub_calculation(hass, with_dest)
        flat_result = run_hub_calculation(hass, without)

    # Engaged: production (derived, ~500 W) is far below the advice anchor, so
    # there is nothing unexportable to charge with.
    assert dest_result["forecast_charge_limit_w"] == 0
    assert dest_runtime["_forecast_charge_limiting"] is True
    # Released on the identical site whose battery is heading for 100 %.
    assert flat_result["forecast_charge_limit_w"] == 5000
    assert flat_runtime["_forecast_charge_limiting"] is False


async def test_a_mid_day_destination_change_moves_the_advice_with_it(hass):
    """The user moves their own ceiling at noon; the advice follows.

    The ratchet only absorbs falls SMALLER than its band, so a real change
    lands on the next cycle in both directions. A change inside the band is
    held — and there the write is still the user's own number, because the
    fan-out writes min(normal, recommendation) (see
    test_inverter_control.py::test_advice_above_the_normal_writes_the_normal).
    """
    from freezegun import freeze_time
    from custom_components.dynamic_ocpp_evse.engine.hub_calculation import (
        run_hub_calculation,
    )

    hub = _destination_hub("moving")
    inverter = _destination_inverter(hub, "moving", "number.dst_normal")
    inverter.add_to_hass(hass)
    hass.data[DOMAIN] = {
        "hubs": {hub.entry_id: {"loads": []}},
        "loads": {},
        "load_allocations": {},
        "inverters": {},
    }
    _set_destination_states(hass, soc=70)

    with freeze_time("2026-08-14 08:00:00+00:00"):
        assert run_hub_calculation(hass, hub)["forecast_battery_max_soc"] == 85

        # Lowered to 80 %: the reserve moves down with the destination at once.
        hass.states.async_set("number.dst_normal", "80", {"unit_of_measurement": "%"})
        assert run_hub_calculation(hass, hub)["forecast_battery_max_soc"] == 70

        # Raised back: a rise is never resisted.
        hass.states.async_set("number.dst_normal", "95", {"unit_of_measurement": "%"})
        assert run_hub_calculation(hass, hub)["forecast_battery_max_soc"] == 85

        # A one-point trim is inside the FORECAST_SOC_HYSTERESIS band, so the
        # published advice holds — the write follows the user's 94 regardless.
        hass.states.async_set("number.dst_normal", "94", {"unit_of_measurement": "%"})
        assert run_hub_calculation(hass, hub)["forecast_battery_max_soc"] == 85


async def test_an_unreadable_destination_holds_its_last_known_value(hass):
    """A ceiling source is a SETPOINT, so it is held rather than stale-guarded.

    "The user asked for 95 %" stays true through a dropout. Snapping back to the
    100 % anchor would RAISE the published ceiling, which the ratchet would then
    resist bringing down again for the rest of the day — and the write-control
    defers every write while the entity is unreadable anyway.
    """
    from freezegun import freeze_time
    from custom_components.dynamic_ocpp_evse.engine.hub_calculation import (
        run_hub_calculation,
    )

    hub = _destination_hub("dropout")
    inverter = _destination_inverter(hub, "dropout", "number.dst_normal")
    inverter.add_to_hass(hass)
    hass.data[DOMAIN] = {
        "hubs": {hub.entry_id: {"loads": []}},
        "loads": {},
        "load_allocations": {},
        "inverters": {},
    }
    _set_destination_states(hass, soc=70)

    with freeze_time("2026-08-14 08:00:00+00:00"):
        assert run_hub_calculation(hass, hub)["forecast_battery_max_soc"] == 85

        hass.states.async_set("number.dst_normal", STATE_UNAVAILABLE)
        assert run_hub_calculation(hass, hub)["forecast_battery_max_soc"] == 85

        # Never readable at all (a fresh hub, a typo'd entity) is the one case
        # with nothing better to hold: the 100 % anchor, exactly as before.
        fresh_hub = _destination_hub("fresh")
        fresh_inv = _destination_inverter(fresh_hub, "fresh", "number.dst_missing")
        fresh_inv.add_to_hass(hass)
        hass.data[DOMAIN]["hubs"][fresh_hub.entry_id] = {"loads": []}
        assert run_hub_calculation(hass, fresh_hub)["forecast_battery_max_soc"] == 90


# ── PV clipping forecast: the NEXT clipping window ────────────────────
#
# The reservation is measured against the next clip within the horizon, not the
# remainder of the calendar day. While today still has clip left that IS the
# remainder of today, byte for byte; once today's clip has integrated away the
# window rolls over to tomorrow's peak, and the search never looks further.

# One hour at 7300 W is 2 kWh above the 5300 W threshold — 10 points of the
# 20 kWh pack, so the reserve below the 95 % destination is 85 %.
_TODAY_PEAK = {
    "2026-08-14T10:00:00+00:00": 7300,
    "2026-08-14T11:00:00+00:00": 0,
}
# One hour at 8300 W is 3 kWh — 15 points, so tomorrow's reserve is 80 %.
_TOMORROW_PEAK = {
    "2026-08-15T10:00:00+00:00": 8300,
    "2026-08-15T11:00:00+00:00": 0,
}
_DAY_AFTER_PEAK = {
    "2026-08-16T10:00:00+00:00": 8300,
    "2026-08-16T11:00:00+00:00": 0,
}


def _set_window_states(hass, soc, watts):
    """The destination rig, with an explicit forecast series."""
    _set_destination_states(hass, soc=soc)
    hass.states.async_set("sensor.dst_forecast", "1.0", {"watts": dict(watts)})


async def test_todays_window_is_untouched_while_today_still_clips(hass):
    """Byte-equivalence: at 08:00 with a peak still ahead, tomorrow's forecast
    changes nothing at all. Same clip, same storable, same ceiling, same cap."""
    from freezegun import freeze_time
    from custom_components.dynamic_ocpp_evse.engine.hub_calculation import (
        run_hub_calculation,
    )

    hub = _destination_hub("windownow")
    inverter = _destination_inverter(hub, "windownow", "number.dst_normal")
    inverter.add_to_hass(hass)
    hass.data[DOMAIN] = {
        "hubs": {hub.entry_id: {"loads": []}},
        "loads": {},
        "load_allocations": {},
        "inverters": {},
    }

    keys = (
        "forecast_clipped_kwh",
        "forecast_absorbable_kwh",
        "forecast_battery_max_soc",
        "forecast_headroom_deficit_kwh",
        "forecast_charge_limit_w",
    )
    with freeze_time("2026-08-14 08:00:00+00:00"):
        _set_window_states(hass, soc=78, watts=_TODAY_PEAK)
        hass.data[DOMAIN]["hubs"][hub.entry_id] = {"loads": []}
        today_only = run_hub_calculation(hass, hub)
        today_only = {k: today_only[k] for k in keys}

        _set_window_states(hass, soc=78, watts={**_TODAY_PEAK, **_TOMORROW_PEAK})
        hass.data[DOMAIN]["hubs"][hub.entry_id] = {"loads": []}
        with_tomorrow = run_hub_calculation(hass, hub)
        with_tomorrow = {k: with_tomorrow[k] for k in keys}

    assert today_only == with_tomorrow
    assert with_tomorrow["forecast_clipped_kwh"] == 2.0
    assert with_tomorrow["forecast_battery_max_soc"] == 85


async def test_the_window_rolls_over_to_tomorrow_once_todays_clip_is_spent(hass):
    """18:00, today's peak hours past: the reservation, and every published
    figure with it, is about TOMORROW's peak.

    The charge cap deliberately does not follow — it asks only about today, so
    it stays released overnight and this feature writes no charge register in
    the dark. SOC 78 sits exactly on the engage threshold of tomorrow's 80 %
    ceiling, so a cap that DID follow the window would engage here.
    """
    from freezegun import freeze_time
    from custom_components.dynamic_ocpp_evse.engine.hub_calculation import (
        run_hub_calculation,
    )

    hub = _destination_hub("windownext")
    inverter = _destination_inverter(hub, "windownext", "number.dst_normal")
    inverter.add_to_hass(hass)
    runtime = {"loads": []}
    hass.data[DOMAIN] = {
        "hubs": {hub.entry_id: runtime},
        "loads": {},
        "load_allocations": {},
        "inverters": {},
    }
    _set_window_states(hass, soc=78, watts={**_TODAY_PEAK, **_TOMORROW_PEAK})

    with freeze_time("2026-08-14 08:00:00+00:00"):
        morning = run_hub_calculation(hass, hub)
    assert morning["forecast_battery_max_soc"] == 85

    with freeze_time("2026-08-14 18:00:00+00:00"):
        evening = run_hub_calculation(hass, hub)

    assert evening["forecast_clipped_kwh"] == 3.0
    assert evening["forecast_absorbable_kwh"] == 3.0
    # 15 points below the 95 % destination — and the 5-point fall clears the
    # FORECAST_SOC_HYSTERESIS band, so the ratchet does not fight the handover.
    assert evening["forecast_battery_max_soc"] == 80
    assert evening["inverters"][inverter.entry_id]["forecast_battery_max_soc"] == 80
    # The cap: released, at full rate, nothing latched.
    assert evening["forecast_charge_limit_w"] == 5000
    assert runtime["_forecast_charge_limiting"] is False

    # An overnight forecast improvement heals the recommendation upward at once.
    with freeze_time("2026-08-14 23:00:00+00:00"):
        _set_window_states(
            hass,
            soc=78,
            watts={**_TODAY_PEAK, "2026-08-15T10:00:00+00:00": 6300,
                   "2026-08-15T11:00:00+00:00": 0},
        )
        healed = run_hub_calculation(hass, hub)
    assert healed["forecast_absorbable_kwh"] == 1.0
    assert healed["forecast_battery_max_soc"] == 90


async def test_the_window_search_never_looks_past_tomorrow(hass):
    """A clip the day after tomorrow must not hold the battery low through the
    clear day in between: with nothing today and nothing tomorrow, the
    recommendation rests at the destination."""
    from freezegun import freeze_time
    from custom_components.dynamic_ocpp_evse.engine.hub_calculation import (
        run_hub_calculation,
    )

    hub = _destination_hub("windowcap")
    inverter = _destination_inverter(hub, "windowcap", "number.dst_normal")
    inverter.add_to_hass(hass)
    hass.data[DOMAIN] = {
        "hubs": {hub.entry_id: {"loads": []}},
        "loads": {},
        "load_allocations": {},
        "inverters": {},
    }
    _set_window_states(hass, soc=78, watts=_DAY_AFTER_PEAK)

    with freeze_time("2026-08-14 18:00:00+00:00"):
        result = run_hub_calculation(hass, hub)

    assert result["forecast_clipped_kwh"] == 0.0
    assert result["forecast_battery_max_soc"] == 95

    # The same clip one day nearer IS reserved for — the cap is what differs.
    _set_window_states(hass, soc=78, watts=_TOMORROW_PEAK)
    hass.data[DOMAIN]["hubs"][hub.entry_id] = {"loads": []}
    with freeze_time("2026-08-14 18:00:00+00:00"):
        nearer = run_hub_calculation(hass, hub)
    assert nearer["forecast_battery_max_soc"] == 80


# ── PV clipping forecast: the just-in-time floor drop ─────────────────
#
# The maintainer's worked example, end to end. Destination 95 %, a 20 kWh pack,
# 300 W base consumption, no clip left today and 2 kWh of it tomorrow — so the
# reserve is 85 %. Production overtakes the house at 08:30, the shed is
# 2 kWh / 300 W = 6 h 40 min, and the early-start factor asks for 8 h: the floor
# drops at 00:30 and the evening's house draw comes out of the battery, not the
# grid.

# Tomorrow's shape: pre-dawn dribble below the 300 W house draw, the crossing at
# 08:30, and one hour 2 kW above the 5300 W clipping threshold.
_TOMORROW_DAWN = {
    "2026-08-15T06:00:00+00:00": 50,
    "2026-08-15T07:00:00+00:00": 200,
    "2026-08-15T08:00:00+00:00": 200,
    "2026-08-15T08:30:00+00:00": 400,
    "2026-08-15T09:00:00+00:00": 1000,
    "2026-08-15T10:00:00+00:00": 7300,
    "2026-08-15T11:00:00+00:00": 0,
}


def _jit_rig(hass, slug):
    """The worked example's hub, inverter and runtime."""
    hub = _destination_hub(slug)
    inverter = _destination_inverter(hub, slug, "number.dst_normal")
    inverter.add_to_hass(hass)
    runtime = {"loads": []}
    hass.data[DOMAIN] = {
        "hubs": {hub.entry_id: runtime},
        "loads": {},
        "load_allocations": {},
        "inverters": {},
    }
    return hub, inverter, runtime


def _set_jit_states(hass, soc, watts=None):
    _set_destination_states(hass, soc=soc)
    hass.states.async_set(
        "sensor.dst_forecast", "1.0", {"watts": dict(watts or _TOMORROW_DAWN)}
    )


async def test_the_reserve_is_held_through_the_evening_and_dropped_just_in_time(hass):
    """The recommendation rests at the 95 % destination all evening, then lands
    on tomorrow's 85 % reserve at 00:30 — 8 hours before production starts."""
    from freezegun import freeze_time
    from custom_components.dynamic_ocpp_evse.engine.hub_calculation import (
        run_hub_calculation,
    )

    hub, inverter, runtime = _jit_rig(hass, "jit")
    _set_jit_states(hass, soc=95)

    def at(stamp):
        with freeze_time(stamp):
            return run_hub_calculation(hass, hub)

    # The evening. The published clip is TOMORROW's 2 kWh — the window has
    # rolled over — but the reserve it buys is not applied yet.
    evening = at("2026-08-14 18:00:00+00:00")
    assert evening["forecast_absorbable_kwh"] == 2.0
    assert evening["forecast_battery_max_soc"] == 95
    assert runtime["_forecast_reservation_due"] is False
    # The RESERVATION half of the cap asks only about today, and today is spent
    # — so nothing here is reserving for tomorrow's clip. What holds the pack is
    # the destination it is sitting on: 95 of 95, so the standing ceiling engages
    # and the advice is the floor rather than the BMS's own rate (the live bug of
    # 2026-08-25, where a day with no clip forecast ran the pack to 98 %).
    assert runtime["_forecast_soc_yielding"] is True
    assert evening["forecast_charge_limit_w"] == 0
    assert runtime["_forecast_charge_limiting"] is True
    # And it is the floor for the honest reason: the meter is nowhere near the
    # export setpoint at 18:00, so there is no surplus to permit. Nothing is
    # carried between cycles for a stale correction to live in.
    assert not [k for k in runtime if "trim" in k]

    for stamp in ("2026-08-14 22:00:00+00:00", "2026-08-15 00:00:00+00:00"):
        held = at(stamp)
        assert held["forecast_battery_max_soc"] == 95, f"dropped early at {stamp}"
        assert held["inverters"][inverter.entry_id][
            "forecast_battery_max_soc"
        ] == 95

    # 00:29 — one minute short of the 8 hours the shed is given.
    assert at("2026-08-15 00:29:00+00:00")["forecast_battery_max_soc"] == 95

    # 00:30 — 6 h 40 min of shed × 1.2 = exactly 8 h before 08:30.
    dropped = at("2026-08-15 00:30:00+00:00")
    assert dropped["forecast_battery_max_soc"] == 85
    assert dropped["inverters"][inverter.entry_id]["forecast_battery_max_soc"] == 85
    assert runtime["_forecast_reservation_due"] is True
    # A 10-point fall clears the FORECAST_SOC_HYSTERESIS band in one cycle —
    # the ratchet does not fight the handover.


async def test_nothing_touches_the_charge_register_overnight_below_the_destination(
    hass,
):
    """The same evening with the pack a band below its destination.

    This is where "the cap asks only about today, and today is spent" still
    holds, and it is the half of the old behaviour worth keeping: under the
    ceiling, with tomorrow's clip a night away, the pack is left at full rate and
    this feature writes no charge register in the dark.
    """
    from freezegun import freeze_time
    from custom_components.dynamic_ocpp_evse.engine.hub_calculation import (
        run_hub_calculation,
    )

    hub, _inverter, runtime = _jit_rig(hass, "jitbelow")
    # 92 is a full FORECAST_SOC_HYSTERESIS band below the 95 % destination, so
    # the yield latch cannot be engaged there in either direction.
    _set_jit_states(hass, soc=92)

    with freeze_time("2026-08-14 18:00:00+00:00"):
        evening = run_hub_calculation(hass, hub)

    assert evening["forecast_absorbable_kwh"] == 2.0
    assert evening["forecast_battery_max_soc"] == 95
    assert runtime["_forecast_soc_yielding"] is False
    assert evening["forecast_charge_limit_w"] == 5000
    assert runtime["_forecast_charge_limiting"] is False


async def test_the_drop_holds_against_a_faster_night_and_an_early_finish(hass):
    """Once dropped, dropped. A pack emptying faster than base consumption
    would make the plain arithmetic say "not yet" again; the latch stops the
    recommendation climbing back to the destination in the dark."""
    from freezegun import freeze_time
    from custom_components.dynamic_ocpp_evse.engine.hub_calculation import (
        run_hub_calculation,
    )

    hub, _inverter, runtime = _jit_rig(hass, "jitlatch")
    _set_jit_states(hass, soc=95)

    with freeze_time("2026-08-15 00:30:00+00:00"):
        assert run_hub_calculation(hass, hub)["forecast_battery_max_soc"] == 85

    # 01:30, twice base consumption taken: 7 h left against 1.2 × 5 h 20 min.
    with freeze_time("2026-08-15 01:30:00+00:00"):
        _set_jit_states(hass, soc=93)
        assert run_hub_calculation(hass, hub)["forecast_battery_max_soc"] == 85

    # The shed finishes hours early: the floor holds the pack at the reserve and
    # the house moves to the grid, which is exactly the intent.
    with freeze_time("2026-08-15 04:00:00+00:00"):
        _set_jit_states(hass, soc=85)
        assert run_hub_calculation(hass, hub)["forecast_battery_max_soc"] == 85

    # And an SOC dropout afterwards does not re-raise a floor already acted on.
    with freeze_time("2026-08-15 05:00:00+00:00"):
        hass.states.async_set("sensor.dst_battery_soc", STATE_UNAVAILABLE)
        assert run_hub_calculation(hass, hub)["forecast_battery_max_soc"] == 85
    assert runtime["_forecast_reservation_due"] is True


async def test_an_overnight_forecast_improvement_heals_the_reserve_upward(hass):
    """A dropped reserve still follows the forecast: a smaller clip tomorrow is
    a rise, and rises are never resisted."""
    from freezegun import freeze_time
    from custom_components.dynamic_ocpp_evse.engine.hub_calculation import (
        run_hub_calculation,
    )

    hub, _inverter, _runtime = _jit_rig(hass, "jitheal")
    _set_jit_states(hass, soc=95)

    with freeze_time("2026-08-15 00:30:00+00:00"):
        assert run_hub_calculation(hass, hub)["forecast_battery_max_soc"] == 85

    with freeze_time("2026-08-15 01:00:00+00:00"):
        _set_jit_states(
            hass, soc=94, watts={**_TOMORROW_DAWN, "2026-08-15T10:00:00+00:00": 6300}
        )
        healed = run_hub_calculation(hass, hub)
    assert healed["forecast_absorbable_kwh"] == 1.0
    assert healed["forecast_battery_max_soc"] == 90


async def test_an_unknown_soc_overnight_holds_at_the_destination(hass):
    """No SOC is no basis for evicting a battery — hold, and let the next cycle
    with a reading decide."""
    from freezegun import freeze_time
    from custom_components.dynamic_ocpp_evse.engine.hub_calculation import (
        run_hub_calculation,
    )

    hub, _inverter, runtime = _jit_rig(hass, "jitnosoc")
    _set_jit_states(hass, soc=95)
    hass.states.async_set("sensor.dst_battery_soc", STATE_UNAVAILABLE)

    # 04:00 is well past the 00:30 the drop would otherwise have landed on.
    with freeze_time("2026-08-15 04:00:00+00:00"):
        result = run_hub_calculation(hass, hub)
    assert result["forecast_battery_max_soc"] == 95
    assert runtime["_forecast_reservation_due"] is False


async def test_a_night_too_short_applies_the_reserve_at_once(hass):
    """Today's clip zeroes with too little night left to shed in: no schedule to
    keep, so the reservation lands immediately."""
    from freezegun import freeze_time
    from custom_components.dynamic_ocpp_evse.engine.hub_calculation import (
        run_hub_calculation,
    )

    hub, _inverter, runtime = _jit_rig(hass, "jitshort")
    _set_jit_states(hass, soc=95)

    # 04:00: 4½ h to production against the 8 h the shed is given.
    with freeze_time("2026-08-15 04:00:00+00:00"):
        result = run_hub_calculation(hass, hub)
    assert result["forecast_battery_max_soc"] == 85
    assert runtime["_forecast_reservation_due"] is True


async def test_an_unreadable_base_consumption_falls_back_to_the_plain_drop(hass):
    """Degraded mode is the pre-scheduling behaviour: apply the reservation as
    soon as it is known. It can only make the battery arrive early."""
    from freezegun import freeze_time
    from custom_components.dynamic_ocpp_evse.engine.hub_calculation import (
        run_hub_calculation,
    )

    template = _destination_hub("jitnobase")
    hub = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        minor_version=4,
        title=template.title,
        data=dict(template.data),
        options={**template.options, CONF_BASE_CONSUMPTION: 0},
    )
    inverter = _destination_inverter(hub, "jitnobase", "number.dst_normal")
    inverter.add_to_hass(hass)
    hass.data[DOMAIN] = {
        "hubs": {hub.entry_id: {"loads": []}},
        "loads": {},
        "load_allocations": {},
        "inverters": {},
    }
    _set_jit_states(hass, soc=95)

    with freeze_time("2026-08-14 18:00:00+00:00"):
        result = run_hub_calculation(hass, hub)
    # Threshold 5000 W without base consumption → 2.3 kWh, so 84 % rather than
    # 85 — the point is that it is applied at 18:00 rather than held.
    assert result["forecast_battery_max_soc"] == 84


# ── PV clipping forecast: the engaged advice, through the real cycle ───
#
# The engaged value is memoryless direct feedback — what the pack is absorbing
# plus (RECONSTRUCTED export − the export setpoint) — so these run the real hub
# cycle with a plant on the other side of it and read the loop's own behaviour.
# The site deliberately draws 500 W against a 300 W configured base: under the
# old feedforward + integral-trim design that 200 W error parked export short of
# the Excess trigger until a slow trim walked it back, and here base does not
# enter the instantaneous path at all.

_FB_SOLAR_W = 8000.0     # measured production, honestly under the hard limit
_FB_HOUSE_W = 500.0      # what the house really draws (the hub says 300)
_FB_SETPOINT_W = 4500.0  # export limit 5000 − trigger margin 500
# Where this plant's feedback equilibrium is: production − house − setpoint.
_FB_EQUILIBRIUM_W = _FB_SOLAR_W - _FB_HOUSE_W - _FB_SETPOINT_W  # 3000 W


def _fb_site(hass, inverter, advice_w):
    """Apply an advice to the plant: the battery takes it, the rest exports.

    Also republishes the enforcement the charge control would have written
    (INVERTER_RT_ENFORCED_CHARGE_W), so the Excess verdict's allowance narrows
    to the rate the battery is really permitted — one cycle behind, exactly as
    in production.
    """
    export_w = _FB_SOLAR_W - _FB_HOUSE_W - advice_w
    hass.states.async_set(
        "sensor.dst_phase_a", str(-export_w / 230.0),
        {"device_class": "current", "unit_of_measurement": "A"},
    )
    hass.states.async_set(
        "sensor.dst_battery_power", str(-advice_w),
        {"device_class": "power", "unit_of_measurement": "W"},
    )
    hass.data[DOMAIN]["inverters"][inverter.entry_id] = {
        INVERTER_RT_ENFORCED_CHARGE_W: advice_w
    }


def _fb_rig(hass, slug, soc=88, base=None):
    """A destination hub whose production is measured, ready for a plant loop.

    ``base`` overrides the hub's base consumption, which is what pins that the
    instantaneous path does not read it.
    """
    hub = _destination_hub(slug, base=300 if base is None else base)
    inverter = _destination_inverter(
        hub, slug, "number.dst_normal", solar_entity="sensor.dst_solar"
    )
    inverter.add_to_hass(hass)
    runtime = {"loads": []}
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN].setdefault("hubs", {})[hub.entry_id] = runtime
    hass.data[DOMAIN].setdefault("loads", {})
    hass.data[DOMAIN].setdefault("load_allocations", {})
    hass.data[DOMAIN].setdefault("inverters", {})
    _set_destination_states(hass, soc=soc)
    hass.states.async_set(
        "sensor.dst_solar", str(_FB_SOLAR_W),
        {"device_class": "power", "unit_of_measurement": "W"},
    )
    # Start the plant at the OLD design's equilibrium (production − anchor,
    # anchor = limit − margin + base), which is 200 W of battery too much and
    # 200 W of export too little. One cycle of the new controller corrects it.
    _fb_site(hass, inverter, _FB_SOLAR_W - (_FB_SETPOINT_W + 300))
    return hub, inverter, runtime


async def test_the_advice_puts_the_site_on_the_excess_trigger_at_once(hass):
    """The loop end to end, through the real cycle and the real reconstruction.

    Closed loop: each cycle's published advice becomes the next cycle's battery
    power and CT reading, and the charge control's enforcement narrows the Excess
    allowance as it does in production. The house draws 500 W against the hub's
    300 W base, which under the feedforward + trim design parked export 200 W
    under the trigger and left the site's Excess margin at −200 W — a load's
    width from firing — until twenty minutes of integration had walked it back.

    Direct feedback closes it on the FIRST cycle: the meter says export is
    200 W short, so the battery gives exactly 200 W back, export lands on
    (limit − margin) and the margin closes onto the trigger. base_consumption is
    never consulted here — it goes on being exact where it belongs, in the
    clipping integral.
    """
    from freezegun import freeze_time
    from custom_components.dynamic_ocpp_evse.engine.hub_calculation import (
        run_hub_calculation,
    )

    hub, inverter, runtime = _fb_rig(hass, "feedback")
    old_design = _FB_SOLAR_W - (_FB_SETPOINT_W + 300)   # 3200 W

    with freeze_time("2026-08-14 08:00:00+00:00") as frozen:
        # One cycle, and the clock has not even moved: there is no time constant
        # to elapse, because there is no integrator.
        result = run_hub_calculation(hass, hub)
        advice = result["forecast_charge_limit_w"]
        assert runtime["_forecast_charge_limiting"] is True
        assert advice == _FB_EQUILIBRIUM_W == old_design - 200

        # Applied, the site sits ON the trigger — export exactly (limit − margin)
        # — and it is a fixed point: zero error means "permit what you already
        # take", so the value repeats for as long as the plant holds still.
        for _ in range(20):
            frozen.tick(10)
            _fb_site(hass, inverter, advice)
            result = run_hub_calculation(hass, hub)
            advice = result["forecast_charge_limit_w"]
            # Within the amps↔watts round trip of the CT reading.
            assert advice == pytest.approx(_FB_EQUILIBRIUM_W, abs=2)

        assert _FB_SOLAR_W - _FB_HOUSE_W - advice == pytest.approx(
            _FB_SETPOINT_W, abs=2
        )
        # And the verdict the whole exercise is about fires: export is on
        # (limit − margin), a margin of 0 IS Excess, and the engaged latch then
        # widens the band by the Excess hysteresis — which is what the published
        # margin shows once it has engaged.
        assert result["excess_available"] is True
        assert result["excess_margin_power"] > 0
        # Nothing accumulated anywhere: no trim state to reset, and no stamp.
        assert not [key for key in runtime if "trim" in key]


async def test_a_misconfigured_base_changes_nothing_in_the_instantaneous_path(hass):
    """base_consumption lives in the INTEGRAL only, and this proves the split.

    Two hubs on the same plant, one with a base of 300 W and one with 5000 W —
    wildly wrong, sixteen times the house draw. The clipping integral must
    disagree (it is a different threshold, so a different reserve), and the
    engaged charge advice must be byte-identical, because it is computed from
    the meter and the pack alone.
    """
    from freezegun import freeze_time
    from custom_components.dynamic_ocpp_evse.engine.hub_calculation import (
        run_hub_calculation,
    )

    # SOC 96 so the DESTINATION hold is what engages the gate in both: that
    # boundary is base-independent, while the reservation's ceiling is not (a
    # 5 kW base really does mean less of the day clips, which is the integral
    # doing its job).
    honest_hub, _honest_inv, _ = _fb_rig(hass, "basehonest", soc=96, base=300)
    wrong_hub, _wrong_inv, _ = _fb_rig(hass, "basewrong", soc=96, base=5000)

    with freeze_time("2026-08-14 08:00:00+00:00"):
        honest = run_hub_calculation(hass, honest_hub)
        wrong = run_hub_calculation(hass, wrong_hub)

    # The energy question DOES move — a 5 kW base means the site can place 5 kW
    # more, so less of the forecast peak clips.
    assert honest["forecast_clipped_kwh"] > wrong["forecast_clipped_kwh"] == 0.0
    assert honest["forecast_battery_max_soc"] < wrong["forecast_battery_max_soc"]
    # Both gates are engaged, on the destination the pack is sitting at.
    assert honest["forecast_charge_limit_w"] is not None
    # The power question does not move at all.
    assert honest["forecast_charge_limit_w"] == pytest.approx(
        _FB_EQUILIBRIUM_W, abs=2
    )
    assert wrong["forecast_charge_limit_w"] == honest["forecast_charge_limit_w"]


async def test_the_advice_carries_nothing_out_of_a_cloud(hass):
    """A cloud is two different cycles, and that is the whole handling.

    The integral trim needed a conditional-integration rule here (the advice is
    pinned at its floor, the actuator cannot move, so do not integrate) and its
    correctness rested on that rule firing. Memoryless, the collapse is simply
    what the meter says now, and the recovery is what it says next — with no
    earned value to hold, freeze, or re-converge.
    """
    from freezegun import freeze_time
    from custom_components.dynamic_ocpp_evse.engine.hub_calculation import (
        run_hub_calculation,
    )

    hub, inverter, runtime = _fb_rig(hass, "cloud")

    with freeze_time("2026-08-14 08:00:00+00:00") as frozen:
        advice = None
        for _ in range(10):
            frozen.tick(10)
            _fb_site(hass, inverter, advice or _FB_EQUILIBRIUM_W)
            advice = run_hub_calculation(hass, hub)["forecast_charge_limit_w"]
        assert advice == pytest.approx(_FB_EQUILIBRIUM_W, abs=2)

        # The cloud: production collapses below the house draw, so the battery
        # stops charging and the site imports. A 4.5 kW error, and the honest
        # answer to it is "charge nothing".
        hass.states.async_set(
            "sensor.dst_solar", "300",
            {"device_class": "power", "unit_of_measurement": "W"},
        )
        for cycle in range(60):  # ten minutes of cloud
            frozen.tick(10)
            hass.states.async_set(
                "sensor.dst_phase_a", str(200.0 / 230.0),
                {"device_class": "current", "unit_of_measurement": "A"},
            )
            hass.states.async_set(
                "sensor.dst_battery_power", "0",
                {"device_class": "power", "unit_of_measurement": "W"},
            )
            hass.data[DOMAIN]["inverters"][inverter.entry_id] = {
                INVERTER_RT_ENFORCED_CHARGE_W: 0.0
            }
            result = run_hub_calculation(hass, hub)
            assert runtime["_forecast_charge_limiting"] is True
            if cycle < 20:
                continue  # the input EMAs are still following the sun down
            assert result["forecast_charge_limit_w"] == 0

        # The sun returns to the same plant, and the value comes back to the same
        # equilibrium — no overshoot on the way, which is what a stale positive
        # correction would have shown up as.
        hass.states.async_set(
            "sensor.dst_solar", str(_FB_SOLAR_W),
            {"device_class": "power", "unit_of_measurement": "W"},
        )
        for _ in range(30):
            frozen.tick(10)
            _fb_site(hass, inverter, _FB_EQUILIBRIUM_W)
            after = run_hub_calculation(hass, hub)
            assert after["forecast_charge_limit_w"] <= _FB_EQUILIBRIUM_W + 2
        assert after["forecast_charge_limit_w"] == pytest.approx(
            _FB_EQUILIBRIUM_W, abs=2
        )
        assert not [key for key in runtime if "trim" in key]


async def test_a_selling_pack_needs_no_freeze_rule(hass):
    """A CHARGE limit cannot correct an export error the battery is causing.

    The integral trim froze for this (a discharging pack is taking nothing for a
    charge limit to trim, and integrating it would wind a correction earned
    under a plant that no longer exists). Here the discharge is simply a negative
    term: the pack SELLS 2 kW, so the arithmetic asks for a discharge, and the
    clamp at 0 is the honest answer — for as long as it lasts, and not one cycle
    longer.
    """
    from freezegun import freeze_time
    from custom_components.dynamic_ocpp_evse.engine.hub_calculation import (
        run_hub_calculation,
    )

    hub, inverter, runtime = _fb_rig(hass, "selling")

    with freeze_time("2026-08-14 08:00:00+00:00") as frozen:
        for _ in range(10):
            frozen.tick(10)
            _fb_site(hass, inverter, _FB_EQUILIBRIUM_W)
            advice = run_hub_calculation(hass, hub)["forecast_charge_limit_w"]
        assert advice == pytest.approx(_FB_EQUILIBRIUM_W, abs=2)

        # The pack flips to selling 2 kW: the meter reads 2 kW MORE export than
        # the array is making, and the pack is absorbing nothing.
        def _selling():
            hass.states.async_set(
                "sensor.dst_battery_power", "2000",
                {"device_class": "power", "unit_of_measurement": "W"},
            )
            hass.states.async_set(
                "sensor.dst_phase_a",
                str(-(_FB_SETPOINT_W + 2000.0) / 230.0),
                {"device_class": "current", "unit_of_measurement": "A"},
            )

        for cycle in range(60):
            frozen.tick(10)
            _selling()
            result = run_hub_calculation(hass, hub)
            assert runtime["_forecast_charge_limiting"] is True
            if cycle < 20:
                continue  # the input EMAs are still crossing into discharge
            # −2000 + 2000 = 0: the two terms cancel, which is exactly right —
            # stopping the sale would put those watts back on the meter, so
            # there is nothing here for the battery to be permitted. And it
            # stays 0 for as long as the sale lasts, with nothing winding up
            # behind it.
            assert result["forecast_charge_limit_w"] == pytest.approx(0, abs=5)

        # Charging resumes, and so does the permit — from the plant, not from a
        # value earned before the sale, and never past the equilibrium on the
        # way (which is what carried state would have looked like).
        for _ in range(30):
            frozen.tick(10)
            _fb_site(hass, inverter, _FB_EQUILIBRIUM_W)
            result = run_hub_calculation(hass, hub)
            assert result["forecast_charge_limit_w"] <= _FB_EQUILIBRIUM_W + 2
        assert result["forecast_charge_limit_w"] == pytest.approx(
            _FB_EQUILIBRIUM_W, abs=2
        )


async def test_the_battery_yields_to_an_engaged_excess_load_above_its_destination(hass):
    """Above the destination the battery is the absorber of LAST RESORT.

    An engaged Excess EVSE drawing 2300 W displaces battery charging watt for
    watt above the 95 % destination; below it, the battery is served first and
    the same car changes nothing. The draw arrives through the real
    reconstruction, so the verdict that engaged the car does not move either.
    """
    from freezegun import freeze_time
    from custom_components.dynamic_ocpp_evse.engine.hub_calculation import (
        run_hub_calculation,
    )

    hub = _destination_hub("yield")
    inverter = _destination_inverter(
        hub, "yield", "number.dst_normal", solar_entity="sensor.dst_solar"
    )
    load = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        minor_version=4,
        title="Excess EVSE",
        data={
            CONF_NAME: "Excess EVSE",
            CONF_ENTITY_ID: "excess_evse",
            ENTRY_TYPE: ENTRY_TYPE_LOAD,
            CONF_CHARGER_ID: "excess_evse",
            CONF_EVSE_CURRENT_IMPORT_ENTITY_ID: "sensor.dst_evse_current",
            CONF_HUB_ENTRY_ID: hub.entry_id,
        },
        options={
            CONF_LOAD_PRIORITY: 1,
            CONF_EVSE_MINIMUM_CHARGE_CURRENT: 6,
            CONF_EVSE_MAXIMUM_CHARGE_CURRENT: 16,
            CONF_PHASES: 1,
        },
    )
    inverter.add_to_hass(hass)
    load.add_to_hass(hass)
    hass.data[DOMAIN] = {
        "hubs": {hub.entry_id: {"loads": [load.entry_id]}},
        "loads": {
            load.entry_id: {
                "entry": load,
                "hub_entry_id": hub.entry_id,
                "operating_mode": "Excess",
                "dynamic_control": True,
            }
        },
        "load_allocations": {load.entry_id: 0},
        "inverters": {},
    }
    _set_destination_states(hass, soc=96)
    hass.states.async_set(
        "sensor.dst_solar", "8000",
        {"device_class": "power", "unit_of_measurement": "W"},
    )
    # The pack is absorbing 3 kW and the meter is pinned at the 5 kW wall, so
    # the engaged value is 3000 + (5000 − 4500) = 3500 W of permit.
    hass.states.async_set(
        "sensor.dst_battery_power", "-3000",
        {"device_class": "power", "unit_of_measurement": "W"},
    )

    def cycle(evse_amps, repeats=20):
        """Hold one physical state until the EMAs have followed it, then read."""
        draw_w = evse_amps * 230.0
        # What the charge control is enforcing, which production always
        # publishes while the cap is engaged: with the pack sitting ON its
        # enforced rate it has no headroom, so the reconstruction leaves the
        # car's freed power on the EXPORT side instead of handing it to the
        # battery — see ``_reconstruct_placement``.
        hass.data[DOMAIN]["inverters"][inverter.entry_id] = {
            INVERTER_RT_ENFORCED_CHARGE_W: 3000.0
        }
        for _ in range(repeats):
            # Physics: the car's draw comes off the meter, and the engine's
            # feedback loop plus reconstruction put it back — which is what
            # makes the reconstruction safe to steer on.
            hass.states.async_set(
                "sensor.dst_phase_a", str(-(5000.0 - draw_w) / 230.0),
                {"device_class": "current", "unit_of_measurement": "A"},
            )
            hass.states.async_set(
                "sensor.dst_evse_current", str(evse_amps),
                {"device_class": "current", "unit_of_measurement": "A"},
            )
            result = run_hub_calculation(hass, hub)
        return result

    with freeze_time("2026-08-14 08:00:00+00:00"):
        idle = cycle(0.0)
        assert idle["forecast_charge_limit_w"] == pytest.approx(3500, abs=2)

        drawing = cycle(10.0)  # 10 A on one phase = 2300 W
        assert drawing["forecast_charge_limit_w"] == pytest.approx(
            3500 - 2300, abs=2
        )
        # Draw-invariant verdict: the same answer with the car running as
        # without it — the reconstruction is what makes the yield safe.
        assert drawing["excess_available"] == idle["excess_available"]

        # Below the destination the battery comes first: same car, no yield.
        hass.states.async_set(
            "sensor.dst_battery_soc", "88",
            {"device_class": "battery", "unit_of_measurement": "%"},
        )
        below = cycle(10.0)
        assert below["forecast_charge_limit_w"] == pytest.approx(3500, abs=2)


async def test_the_verdict_does_not_flap_while_the_battery_yields_above_target(hass):
    """The interaction the yield could have broken: allowance narrowing.

    Above the destination an engaged Excess load takes the surplus and the
    battery's advice drops by its draw; the charge control then enforces that
    lower rate, which narrows the Excess allowance in turn. The worry is a loop
    — narrower allowance, smaller margin, load dropped, advice back up. It
    cannot happen, because both halves of the verdict are load-invariant: the
    draw is credited back on the export side, and a battery sitting on an
    enforced limit has no headroom to be handed anything. The margin only ever
    grows as the battery yields.
    """
    from freezegun import freeze_time
    from custom_components.dynamic_ocpp_evse.engine.hub_calculation import (
        run_hub_calculation,
    )

    hub = _destination_hub("noflap")
    inverter = _destination_inverter(
        hub, "noflap", "number.dst_normal", solar_entity="sensor.dst_solar"
    )
    load = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        minor_version=4,
        title="Excess EVSE",
        data={
            CONF_NAME: "Excess EVSE",
            CONF_ENTITY_ID: "excess_evse_noflap",
            ENTRY_TYPE: ENTRY_TYPE_LOAD,
            CONF_CHARGER_ID: "excess_evse_noflap",
            CONF_EVSE_CURRENT_IMPORT_ENTITY_ID: "sensor.dst_evse_current",
            CONF_HUB_ENTRY_ID: hub.entry_id,
        },
        options={
            CONF_LOAD_PRIORITY: 1,
            CONF_EVSE_MINIMUM_CHARGE_CURRENT: 6,
            CONF_EVSE_MAXIMUM_CHARGE_CURRENT: 16,
            CONF_PHASES: 1,
        },
    )
    inverter.add_to_hass(hass)
    load.add_to_hass(hass)
    runtime = {"loads": [load.entry_id]}
    hass.data[DOMAIN] = {
        "hubs": {hub.entry_id: runtime},
        "loads": {
            load.entry_id: {
                "entry": load,
                "hub_entry_id": hub.entry_id,
                "operating_mode": "Excess",
                "dynamic_control": True,
            }
        },
        "load_allocations": {load.entry_id: 0},
        "inverters": {},
    }
    _set_destination_states(hass, soc=96)
    hass.states.async_set(
        "sensor.dst_solar", "8000",
        {"device_class": "power", "unit_of_measurement": "W"},
    )
    def stage(export_w, battery_w, enforced_w, evse_amps, repeats=20):
        """Hold one physical state until the EMAs have followed it, then read."""
        hass.data[DOMAIN]["inverters"][inverter.entry_id] = {
            INVERTER_RT_ENFORCED_CHARGE_W: enforced_w
        }
        for _ in range(repeats):
            hass.states.async_set(
                "sensor.dst_phase_a", str(-export_w / 230.0),
                {"device_class": "current", "unit_of_measurement": "A"},
            )
            hass.states.async_set(
                "sensor.dst_battery_power", str(-battery_w),
                {"device_class": "power", "unit_of_measurement": "W"},
            )
            hass.states.async_set(
                "sensor.dst_evse_current", str(evse_amps),
                {"device_class": "current", "unit_of_measurement": "A"},
            )
            result = run_hub_calculation(hass, hub)
        return result

    with freeze_time("2026-08-14 08:00:00+00:00"):
        # 1. Clipping window, car idle: 8000 W produced, 500 W house, the
        #    battery on its enforced 3000 W, 4500 W leaving — export exactly on
        #    the trigger, and Excess on precisely because the allowance is the
        #    enforced rate rather than the 5 kW nameplate.
        idle = stage(4500.0, 3000.0, 3000.0, 0.0)
        assert idle["excess_available"] is True
        # Export ON the setpoint: zero error, so the permit is what the pack
        # already takes.
        assert idle["forecast_charge_limit_w"] == pytest.approx(3000, abs=2)

        # 2. The car engages on the surplus, taking 2300 W of what was being
        #    exported. The verdict must not move — and the battery yields.
        engaged = stage(2200.0, 3000.0, 3000.0, 10.0)
        assert engaged["excess_available"] is True
        assert engaged["excess_margin_power"] >= idle["excess_margin_power"]
        assert engaged["forecast_charge_limit_w"] == pytest.approx(
            3000 - 2300, abs=2
        )

        # 3. The control writes that lower rate, so the battery really does take
        #    700 W and the allowance narrows with it. The margin only widens:
        #    nothing here can drop the load that earned it.
        yielded = stage(4500.0, 700.0, 700.0, 10.0)
        assert yielded["excess_available"] is True
        assert yielded["excess_margin_power"] > engaged["excess_margin_power"]
        assert yielded["forecast_charge_limit_w"] == pytest.approx(
            3000 - 2300, abs=2
        )


# ── PV clipping forecast: the destination is a STANDING ceiling ────────
#
# The live event of 2026-08-25. Destination 95 %, a 20 kWh pack, and a day the
# forecast said would clip NOTHING: the charge advice's first test was
# "absorbable_kwh <= 0 → full rate", so the pack crossed 95 % at 08:55 UTC and
# ran to 98 % at the BMS's own 80 A while the maintainer watched. The 5 % above
# the destination is the buffer for a day that beats the forecast, and it must
# not be spent by a forecast that under-read the day.

# A clear day whose every block is far below the 5300 W clipping threshold:
# nothing to reserve for, all day, which is the state the bug needed.
_NO_CLIP_DAY = {
    "2026-08-25T08:00:00+00:00": 3000,
    "2026-08-25T09:00:00+00:00": 4000,
    "2026-08-25T10:00:00+00:00": 4000,
    "2026-08-25T11:00:00+00:00": 0,
}


def _no_clip_rig(hass, slug, soc, solar_w=4000.0, normal_entity="number.dst_normal"):
    """The destination rig on a day with nothing forecast to clip."""
    hub = _destination_hub(slug)
    inverter = _destination_inverter(
        hub, slug, normal_entity, solar_entity="sensor.dst_solar"
    )
    inverter.add_to_hass(hass)
    runtime = {"loads": []}
    hass.data[DOMAIN] = {
        "hubs": {hub.entry_id: runtime},
        "loads": {},
        "load_allocations": {},
        "inverters": {},
    }
    _set_destination_states(hass, soc=soc)
    hass.states.async_set("sensor.dst_forecast", "1.0", {"watts": dict(_NO_CLIP_DAY)})
    hass.states.async_set(
        "sensor.dst_solar", str(solar_w),
        {"device_class": "power", "unit_of_measurement": "W"},
    )
    return hub, inverter, runtime


async def test_the_destination_stops_the_charge_with_nothing_forecast_to_clip(hass):
    """The live event, replayed: SOC 93 → 94 → 95 → 96 → 97, then back down.

    Full rate below the destination — under the ceiling, with nothing reserved,
    refilling is right — and the floor from 95 on, with the latch engaged. Before
    the reorder every one of these cycles published the full 5 kW.
    """
    from freezegun import freeze_time
    from custom_components.dynamic_ocpp_evse.engine.hub_calculation import (
        run_hub_calculation,
    )

    hub, inverter, runtime = _no_clip_rig(hass, "standing", soc=93)

    def at_soc(soc):
        hass.states.async_set(
            "sensor.dst_battery_soc", str(soc),
            {"device_class": "battery", "unit_of_measurement": "%"},
        )
        with freeze_time("2026-08-25 09:00:00+00:00"):
            return run_hub_calculation(hass, hub)

    # Nothing is reserved anywhere: the ceiling IS the destination all morning.
    first = at_soc(93)
    assert first["forecast_absorbable_kwh"] == 0.0
    assert first["forecast_battery_max_soc"] == 95

    for soc in (93, 94):
        result = at_soc(soc)
        assert result["forecast_charge_limit_w"] == 5000, f"held at SOC {soc}"
        assert runtime["_forecast_charge_limiting"] is False
        assert runtime["_forecast_soc_yielding"] is False

    # 08:55 UTC on the maintainer's site: the crossing. Production is under the
    # export limit, so there is no overshoot to charge with and the advice is the
    # floor — the pack parks at its destination instead of running to 98 %.
    for soc in (95, 96, 97):
        result = at_soc(soc)
        assert result["forecast_charge_limit_w"] == 0, f"ran on at SOC {soc}"
        assert runtime["_forecast_charge_limiting"] is True
        assert runtime["_forecast_soc_yielding"] is True
        # The per-inverter advice the charge control actually drives.
        assert result["inverters"][inverter.entry_id]["forecast_charge_limit_w"] == 0
        # And the ceiling is still the destination — this is a POWER hold, not a
        # reservation: nothing was carved out below 95.
        assert result["forecast_battery_max_soc"] == 95

    # A tick back inside the band still holds (the yield latch releases a full
    # FORECAST_SOC_HYSTERESIS below the destination, not on the first tick).
    for soc in (94, 93):
        assert at_soc(soc)["forecast_charge_limit_w"] == 0, f"released at SOC {soc}"

    # The evening: the house takes the pack down a full band and the hold lets
    # go. Below the ceiling, full rate is the right answer again.
    released = at_soc(92)
    assert released["forecast_charge_limit_w"] == 5000
    assert runtime["_forecast_charge_limiting"] is False
    assert runtime["_forecast_soc_yielding"] is False


async def test_a_parked_pack_accumulates_nothing_at_all(hass):
    """An afternoon parked on the floor leaves nothing behind to kick.

    A parked pack reports ``limiting`` with its advice pinned at 0 against a
    standing 4.5 kW export error — the regime the deleted integral trim needed
    its freeze-at-floor rule for, because without it an afternoon of these
    cycles integrated to the clamp and the first real surplus arrived with a
    kilowatt of stale correction on top of it. Memoryless, there is nothing to
    freeze and nothing carried: every cycle is the same recomputation, and the
    hub runtime never grows a value for it.
    """
    from freezegun import freeze_time
    from custom_components.dynamic_ocpp_evse.engine.hub_calculation import (
        run_hub_calculation,
    )

    # Production far below the export setpoint and the site importing, so the
    # standing error is the biggest it ever gets.
    hub, _inverter, runtime = _no_clip_rig(hass, "parkedtrim", soc=95, solar_w=1000.0)

    with freeze_time("2026-08-25 09:00:00+00:00") as frozen:
        for _ in range(180):  # half an hour at the 10 s cycle
            frozen.tick(10)
            result = run_hub_calculation(hass, hub)
            assert result["forecast_charge_limit_w"] == 0
            assert runtime["_forecast_charge_limiting"] is True
        # No integrator, no stamp, no accumulated correction — the only state
        # the ADVICE carries is the three latches and the ceiling ratchet.
        advice_state = [
            "_forecast_charge_limiting",
            "_forecast_max_soc",
            "_forecast_parse_memo",
            "_forecast_reservation_due",
            "_forecast_soc_yielding",
        ]
        # The observers DO accumulate, and deliberately: measuring forecast
        # accuracy and peakiness is what they are for. Excluded by name rather
        # than folded into the list above, so the ADVICE's statelessness stays
        # exactly the assertion it was — an integrator sneaking back into the
        # advice still fails here — and so the list does not depend on whether
        # a given rig configures its forecast per inverter or hub-wide (the
        # gain observer is per inverter, and only runs where devices are).
        observer_state = {
            "_forecast_clipped_observer",
            "_forecast_gain_observer",
            "_forecast_obs_mono",
            "_forecast_peak_observer",
        }
        assert sorted(
            k
            for k in runtime
            if k.startswith("_forecast") and k not in observer_state
        ) == advice_state


async def test_a_site_with_no_ceiling_source_is_untouched_by_the_hold(hass):
    """No ceiling source anywhere: the destination is 100 %, so the gate cannot
    engage below it and such a site behaves exactly as it did before.

    The whole SOC range on the same clear day, against the old rule's answer.
    """
    from freezegun import freeze_time
    from custom_components.dynamic_ocpp_evse.engine.hub_calculation import (
        run_hub_calculation,
    )

    hub, _inverter, runtime = _no_clip_rig(
        hass, "nosource", soc=90, normal_entity=None
    )

    def at_soc(soc):
        hass.states.async_set(
            "sensor.dst_battery_soc", str(soc),
            {"device_class": "battery", "unit_of_measurement": "%"},
        )
        with freeze_time("2026-08-25 09:00:00+00:00"):
            return run_hub_calculation(hass, hub)

    assert at_soc(90)["forecast_battery_max_soc"] == 100
    for soc in range(0, 100, 7):
        result = at_soc(soc)
        assert result["forecast_charge_limit_w"] == 5000, f"held at SOC {soc}"
        assert runtime["_forecast_charge_limiting"] is False
        assert runtime["_forecast_soc_yielding"] is False
    for soc in (96, 97, 98, 99):
        assert at_soc(soc)["forecast_charge_limit_w"] == 5000, f"held at SOC {soc}"

    # At 100 the pack IS at its destination, and a full battery held on the
    # floor is what a standing ceiling means — it cannot charge either way.
    assert at_soc(100)["forecast_charge_limit_w"] == 0
    assert runtime["_forecast_soc_yielding"] is True


async def test_the_parked_battery_hands_the_surplus_to_the_excess_verdict(hass):
    """Why the hold must report ``limiting`` — the other half of the fix.

    A held advice sends the control down its LIMITING branch, which publishes
    what the register really permits (INVERTER_RT_ENFORCED_CHARGE_W). That is
    what narrows the Excess verdict's battery allowance to the floor, and it is
    load-bearing: it is how the surplus a parked battery refuses reaches the
    Excess loads that exist to soak it up.

    The plant: 4800 W produced (exactly the advice anchor, so no overshoot), a
    300 W house, the battery parked and taking nothing, and 4500 W leaving — the
    export limit less the trigger margin, saturated.
    """
    from freezegun import freeze_time
    from custom_components.dynamic_ocpp_evse.engine.hub_calculation import (
        run_hub_calculation,
    )

    hub, inverter, runtime = _no_clip_rig(hass, "parked", soc=95, solar_w=4800.0)

    def cycle(enforced_w, repeats=6):
        """Hold one physical state until the EMAs have followed it."""
        hass.data[DOMAIN]["inverters"][inverter.entry_id] = {
            INVERTER_RT_ENFORCED_CHARGE_W: enforced_w
        }
        for _ in range(repeats):
            hass.states.async_set(
                "sensor.dst_phase_a", str(-4500.0 / 230.0),
                {"device_class": "current", "unit_of_measurement": "A"},
            )
            hass.states.async_set(
                "sensor.dst_battery_power", "0",
                {"device_class": "power", "unit_of_measurement": "W"},
            )
            result = run_hub_calculation(hass, hub)
        return result

    with freeze_time("2026-08-25 09:00:00+00:00"):
        # What the control has written: the floor, because the pack is parked at
        # its destination with no overshoot to admit.
        parked = cycle(0.0)
        assert parked["forecast_charge_limit_w"] == pytest.approx(0, abs=2)
        assert runtime["_forecast_charge_limiting"] is True
        # Allowance = the export limit less the trigger margin, and nothing at
        # all for a battery that may take nothing — so the 4500 W leaving the
        # site sits exactly on it and Excess fires. The reading is +500 rather
        # than 0 because the verdict has latched on and its release band
        # (DEFAULT_EXCESS_HYSTERESIS) widens the margin from the second cycle.
        assert parked["excess_available"] is True
        assert parked["excess_margin_power"] == pytest.approx(500.0, abs=1.0)

        # The bite: without the narrowing the same site reads 5 kW short of
        # Excess, because the nameplate rate counts as somewhere to put power
        # the battery is in fact refusing.
        unnarrowed = cycle(None)
        assert unnarrowed["excess_margin_power"] == pytest.approx(-5000.0, abs=1.0)
        assert unnarrowed["excess_available"] is False


async def test_grid_phases_in_watts_are_converted_to_amps(
    hass,
    hub_entry,
    charger_entry,
    setup_domain_data,
):
    """A grid CT configured as a POWER sensor must be converted to amps.

    Meters commonly publish an unsigned current entity and a signed power
    entity, and only the signed one can show export — so watts are a valid
    choice for these fields. Without conversion the watt value was read as
    amps and then multiplied by voltage again: 1.3 kW of import surfaced as
    ~300 kW of grid power.
    """
    from custom_components.dynamic_ocpp_evse.engine.hub_calculation import (
        run_hub_calculation,
    )

    _set_ha_states(hass, hub_entry)
    # 1150 W per phase at 230 V = 5 A per phase — the same site state the
    # amps-based fixture sets up, expressed the other way.
    for entity in (
        "sensor.inverter_phase_a",
        "sensor.inverter_phase_b",
        "sensor.inverter_phase_c",
    ):
        hass.states.async_set(
            entity, "1150", {"device_class": "power", "unit_of_measurement": "W"}
        )

    result = run_hub_calculation(hass, hub_entry)

    # 3 × 1150 W = 3450 W, not 3 × 1150 A × 230 V
    assert result["grid_power"] == pytest.approx(3450, abs=50)


async def test_grid_phase_export_keeps_its_sign(
    hass,
    hub_entry,
    charger_entry,
    setup_domain_data,
):
    """A negative (exporting) power reading must stay negative through the
    conversion — the sign is the only thing that distinguishes export."""
    from custom_components.dynamic_ocpp_evse.engine.hub_calculation import (
        run_hub_calculation,
    )

    _set_ha_states(hass, hub_entry)
    for entity in (
        "sensor.inverter_phase_a",
        "sensor.inverter_phase_b",
        "sensor.inverter_phase_c",
    ):
        hass.states.async_set(
            entity, "-2300", {"device_class": "power", "unit_of_measurement": "W"}
        )

    result = run_hub_calculation(hass, hub_entry)

    # 3 × −2300 W of export, so the published grid power is negative
    assert result["grid_power"] == pytest.approx(-6900, abs=50)


# ── Inverter charge control: driven by the site cycle, not by a poll ───
#
# This sensor was the last platform-polled entity in the integration: its update
# AWAITS a Modbus register write, which a coordinator listener (a synchronous
# callback) cannot do. It now joins the cycle as a site-cycle *worker* — awaited
# by the coordinator after the result is published — and the platform's
# SCAN_INTERVAL is gone. What these pin is the drive mechanism: registration,
# the write happening through the real cycle, the opt-in gate, the pacing across
# cycles, and async_update no longer writing anything.
#
# The sensor's PUBLISHED VALUE is a measurement of the target register — the
# number the inverter's charge-limit entity holds, in that register's own unit —
# and our own standing ("off"/"idle"/"limiting") is the ``control_state``
# attribute beside it. So each of these also pins what the cycle publishes:
# a numeric state that keeps moving with the register whether or not this cycle
# wrote anything, which is what makes the sensor graphable and gives it long-term
# statistics. The register read-back is taken BEFORE the write, so the value is
# what the inverter last reported rather than an echo of our own intention.
#
# The pacing/deadband/release contract itself is tested in
# dev/tests/test_inverter_control.py, which also runs in the pure tier.

CHARGE_TARGET = "number.deye_max_charge_current"


@pytest.fixture
def inverter_entry(hub_entry: MockConfigEntry) -> MockConfigEntry:
    """An inverter entry on the test hub that writes a charge-limit register."""
    return MockConfigEntry(
        domain=DOMAIN,
        version=2,
        minor_version=2,
        title="Deye Hybrid",
        data={
            CONF_NAME: "Deye Hybrid",
            CONF_ENTITY_ID: "deye_hybrid",
            ENTRY_TYPE: ENTRY_TYPE_INVERTER,
            CONF_HUB_ENTRY_ID: hub_entry.entry_id,
        },
        options={
            CONF_CHARGE_LIMIT_ENTITY_ID: CHARGE_TARGET,
            CONF_BATTERY_NOMINAL_VOLTAGE: 51.2,
            CONF_CHARGE_CONTROL_INTERVAL: 300,
        },
    )


@pytest.fixture
def inverter_entry_floored(hub_entry: MockConfigEntry) -> MockConfigEntry:
    """The same inverter, with a 2 A floor under the engaged limit."""
    return MockConfigEntry(
        domain=DOMAIN,
        version=2,
        minor_version=2,
        title="Deye Hybrid",
        data={
            CONF_NAME: "Deye Hybrid",
            CONF_ENTITY_ID: "deye_hybrid",
            ENTRY_TYPE: ENTRY_TYPE_INVERTER,
            CONF_HUB_ENTRY_ID: hub_entry.entry_id,
        },
        options={
            CONF_CHARGE_LIMIT_ENTITY_ID: CHARGE_TARGET,
            CONF_BATTERY_NOMINAL_VOLTAGE: 51.2,
            CONF_CHARGE_CONTROL_INTERVAL: 300,
            CONF_CHARGE_LIMIT_MINIMUM: 2,
        },
    )


@pytest.fixture
def inverter_entry_watts(hub_entry: MockConfigEntry) -> MockConfigEntry:
    """The same, on an inverter whose register counts watts instead of DC amps."""
    return MockConfigEntry(
        domain=DOMAIN,
        version=2,
        minor_version=2,
        title="Watt Hybrid",
        data={
            CONF_NAME: "Watt Hybrid",
            CONF_ENTITY_ID: "watt_hybrid",
            ENTRY_TYPE: ENTRY_TYPE_INVERTER,
            CONF_HUB_ENTRY_ID: hub_entry.entry_id,
        },
        options={
            CONF_CHARGE_LIMIT_ENTITY_ID: CHARGE_TARGET,
            CONF_CHARGE_LIMIT_UNIT: CHARGE_LIMIT_UNIT_WATTS,
            CONF_CHARGE_CONTROL_INTERVAL: 300,
        },
    )


async def _add_charge_control(hass, inverter_entry, *, armed=True, register="100"):
    """Create the charge-control sensor and let it join its hub's site cycle.

    Registration goes through async_added_to_hass — the production path, which
    HA calls when it adds the entity — rather than by poking the bucket, so the
    registration itself is under test. Note there is no hub coordinator in these
    tests at all: registration, not a coordinator reference, is the whole link.

    The register starts at its 100 A maximum, which is also the value a release
    restores (no configured normal), making the deadband 5 % of 100 A. That
    starting value is what the sensor reports until the inverter moves it.
    """
    hass.states.async_set(CHARGE_TARGET, register, {"max": float(register)})
    hass.data.setdefault(DOMAIN, {}).setdefault("inverters", {})[
        inverter_entry.entry_id
    ] = {INVERTER_RT_CONTROL_ENABLED: armed}
    sensor = LoadJugglerInverterChargeControlSensor(hass, inverter_entry, "deye_hybrid")
    await sensor.async_added_to_hass()
    return sensor


def _advice_cycle(inverter_entry, advice_w):
    """Run the cycle with one crafted piece of forecast advice.

    The engine is patched out on purpose: producing this number for real needs a
    whole configured clipping forecast, and these tests are about who performs
    the write and when, not about how the advice is computed. ``None`` is the
    release signal — the forecast having nothing to say.
    """
    return patch(
        "custom_components.dynamic_ocpp_evse.sensor.run_hub_calculation",
        return_value={
            "inverters": {
                inverter_entry.entry_id: {"forecast_charge_limit_w": advice_w}
            },
        },
    )


def _register_writes(mock_call):
    """The number.set_value calls among everything the cycle called."""
    return [
        c for c in mock_call.call_args_list
        if c[0][0] == "number" and c[0][1] == "set_value"
    ]


def _accepting_register(hass, maximum=100):
    """Patch the service registry with an inverter that ACCEPTS what we write.

    A bare AsyncMock swallows the write, so the register would sit at its
    starting value forever — and the register is what this sensor now reports.
    This applies ``number.set_value`` to the state machine the way a real number
    entity would, which is what lets the next cycle read our own write back.
    Calls are still recorded, so ``_register_writes`` works unchanged.
    """

    async def _apply(domain, service, data, *args, **kwargs):
        # Patched on the class, so there is no self argument (see _register_writes
        # indexing the same positional triple).
        if (domain, service) == ("number", "set_value"):
            hass.states.async_set(
                str(data["entity_id"]), str(data["value"]), {"max": maximum}
            )

    return patch(
        "homeassistant.core.ServiceRegistry.async_call",
        new_callable=AsyncMock,
        side_effect=_apply,
    )


async def test_charge_control_registers_as_a_site_cycle_worker(
    hass, hub_entry, inverter_entry
):
    sensor = await _add_charge_control(hass, inverter_entry)

    workers = hass.data[DOMAIN][SITE_CYCLE_WORKERS][hub_entry.entry_id]
    assert list(workers.values()) == [sensor]
    # A poll would be a second caller of the write that nothing serializes.
    assert sensor.should_poll is False
    # Unconditionally available — deliberately, even though the value is now a
    # reading: a charge-limit register only changes when something writes it, so
    # the last value read stays true, and the reading's own failure (unreadable)
    # is reported as unknown rather than by blanking the entity.
    assert sensor.available is True
    # No cycle has read the register yet, so there is no value — unknown, not 0,
    # which would claim a real limit of zero.
    assert sensor.native_value is None
    # The standing is an attribute now, and before the first cycle it is "off" —
    # the control has recorded nothing and has written nothing.
    assert sensor.extra_state_attributes["control_state"] == CONTROL_STATE_OFF
    assert sensor.extra_state_attributes["seconds_since_write"] is None


async def test_the_site_cycle_performs_the_charge_limit_write(
    hass, hub_entry, inverter_entry
):
    """The write rides the coordinator's cycle — no poll involved.

    And the sensor reports the register through it: 100 A while that is still what
    the inverter holds, 50 A once it has taken the write.
    """
    sensor = await _add_charge_control(hass, inverter_entry)

    with _accepting_register(hass) as mock_call, _advice_cycle(
        inverter_entry, 2560.0
    ):
        await _run_site_cycle(hass, hub_entry)

        writes = _register_writes(mock_call)
        assert len(writes) == 1, writes
        # 2560 W at 51.2 V = 50 A, half the register's 100 A
        assert writes[0][0][2] == {"entity_id": CHARGE_TARGET, "value": 50.0}
        # The read-back precedes the write, so this cycle still reports the value
        # the inverter had. The state is a measurement of the register, never an
        # echo of what we asked for.
        assert sensor.native_value == 100.0
        attributes = sensor.extra_state_attributes
        assert attributes["control_state"] == CONTROL_STATE_LIMITING
        assert attributes["recommended_value"] == 50.0
        assert attributes["applied_value"] == 50.0
        assert attributes["normal_value"] == 100.0
        assert sensor.native_unit_of_measurement == CHARGE_LIMIT_UNIT_AMPS

        # The inverter took the 50 A. The next cycle writes nothing (paced out)
        # and still reports the new value — the read-back does not depend on a
        # write having happened, which is what makes this a graph.
        await _run_site_cycle(hass, hub_entry)

        assert len(_register_writes(mock_call)) == 1
        assert sensor.native_value == 50.0
        assert sensor.extra_state_attributes["control_state"] == CONTROL_STATE_LIMITING

    inverter_rt = hass.data[DOMAIN]["inverters"][inverter_entry.entry_id]
    assert inverter_rt[INVERTER_RT_APPLIED] == 50.0


async def test_the_sensor_reports_the_floor_the_cycle_wrote(
    hass, hub_entry, inverter_entry_floored
):
    """A 0 W advice under a 2 A floor: the register goes to 2 A, and the sensor
    reports 2 A because it measures the register rather than the advice.

    This is the sensor half of the floor — no separate publication and no extra
    attribute, just the read-back moving to where we actually put it.
    """
    sensor = await _add_charge_control(hass, inverter_entry_floored)

    with _accepting_register(hass) as mock_call, _advice_cycle(
        inverter_entry_floored, 0.0
    ):
        await _run_site_cycle(hass, hub_entry)

        writes = _register_writes(mock_call)
        assert len(writes) == 1, writes
        assert writes[0][0][2] == {"entity_id": CHARGE_TARGET, "value": 2.0}
        # The read-back still precedes the write, so this cycle reports the 100 A
        # the inverter held; the next one reports what we wrote.
        assert sensor.native_value == 100.0
        assert sensor.extra_state_attributes["recommended_value"] == 2.0

        await _run_site_cycle(hass, hub_entry)

        assert len(_register_writes(mock_call)) == 1
        assert sensor.native_value == 2.0
        assert sensor.extra_state_attributes["control_state"] == CONTROL_STATE_LIMITING


async def test_nothing_is_written_while_the_switch_is_off(
    hass, hub_entry, inverter_entry
):
    """The opt-in gate is per call, so a faster cadence cannot leak a write."""
    sensor = await _add_charge_control(hass, inverter_entry, armed=False)

    with _accepting_register(hass) as mock_call, _advice_cycle(inverter_entry, 2560.0):
        for _ in range(5):
            await _run_site_cycle(hass, hub_entry)

    assert _register_writes(mock_call) == []
    assert sensor.extra_state_attributes["control_state"] == CONTROL_STATE_OFF
    # An unarmed control still reports the register: the value is the inverter's,
    # not ours, so the graph runs continuously through the periods we do nothing.
    assert sensor.native_value == 100.0
    assert sensor.available is True


async def test_repeated_cycles_write_once_inside_the_interval(
    hass, hub_entry, inverter_entry
):
    """The cadence change's whole risk, at the entity level.

    The check now runs every site cycle (2 s by default) instead of every 10 s
    platform poll. The min-interval is wall-clock, so five cycles back to back
    are still one write.
    """
    await _add_charge_control(hass, inverter_entry)

    with patch(
        "homeassistant.core.ServiceRegistry.async_call", new_callable=AsyncMock
    ) as mock_call, _advice_cycle(inverter_entry, 2560.0):
        for _ in range(5):
            await _run_site_cycle(hass, hub_entry)

    assert len(_register_writes(mock_call)) == 1


async def test_the_cycle_ramps_the_release_and_then_stops(
    hass, hub_entry, inverter_entry
):
    """Advice stopping walks the register back UP to the normal value one Excess
    trigger margin per write, and then stops for good.

    Two failures in one test, because the fix for the first caused the second:
    the release must not hand the battery full rate in a single step (it would
    drink the clipping reserve out of exportable power in minutes), and it must
    not be rewritten every 2 s all night either. The ramp arithmetic itself is
    pinned in test_inverter_control.py; this is the same contract through the
    real cycle, with the register accepting every write.
    """
    sensor = await _add_charge_control(hass, inverter_entry)

    with patch(
        "homeassistant.core.ServiceRegistry.async_call", new_callable=AsyncMock
    ), _advice_cycle(inverter_entry, 2560.0):
        await _run_site_cycle(hass, hub_entry)

    inverter_rt = hass.data[DOMAIN]["inverters"][inverter_entry.entry_id]
    # The inverter took the 50 A we wrote; now the forecast releases.
    hass.states.async_set(CHARGE_TARGET, "50", {"max": 100})
    with _accepting_register(hass) as mock_call, _advice_cycle(inverter_entry, None):
        # Four back-to-back cycles inside the 300 s write window: the release is
        # paced like every other write, so none of them writes anything.
        for _ in range(4):
            await _run_site_cycle(hass, hub_entry)
        assert _register_writes(mock_call) == []
        assert inverter_rt[INVERTER_RT_APPLIED] == 50.0

        # Now open the write window repeatedly — backdating our own pacing marker
        # rather than patching the clock — and let the ramp run to its end.
        for _ in range(10):
            inverter_rt[INVERTER_RT_LAST_WRITE] -= 400
            await _run_site_cycle(hass, hub_entry)
            if inverter_rt[INVERTER_RT_APPLIED] is None:
                break

    # 500 W of margin at 51.2 V is a 9.8 A step. The last 1 A of the climb is
    # inside the 5 A deadband, so the ramp ends by clearing the marker rather
    # than by spending a write on it.
    written = [call[0][2]["value"] for call in _register_writes(mock_call)]
    assert written == [59.8, 69.6, 79.4, 89.2, 99.0]
    assert inverter_rt[INVERTER_RT_APPLIED] is None
    assert sensor.extra_state_attributes["control_state"] == CONTROL_STATE_IDLE
    # And the climb is visible in the value the sensor graphs, read from the
    # register the later cycles saw.
    assert sensor.native_value == 99.0


async def test_the_ramp_step_comes_from_the_hubs_trigger_margin(
    hass, hub_entry, inverter_entry
):
    """The slew step is a SITE-level setting, so it has to travel from the hub
    entry to a control that is handed the inverter's own entry. This is that
    wiring, end to end: change the hub's Excess trigger margin and the size of
    the release step changes with it."""
    hub_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        hub_entry,
        options={**hub_entry.options, CONF_EXCESS_TRIGGER_MARGIN: 1024},
    )
    await _add_charge_control(hass, inverter_entry)

    with patch(
        "homeassistant.core.ServiceRegistry.async_call", new_callable=AsyncMock
    ), _advice_cycle(inverter_entry, 2560.0):
        await _run_site_cycle(hass, hub_entry)

    inverter_rt = hass.data[DOMAIN]["inverters"][inverter_entry.entry_id]
    hass.states.async_set(CHARGE_TARGET, "50", {"max": 100})
    with _accepting_register(hass) as mock_call, _advice_cycle(inverter_entry, None):
        inverter_rt[INVERTER_RT_LAST_WRITE] -= 400
        await _run_site_cycle(hass, hub_entry)

    # 1024 W at 51.2 V is a 20 A step, not the 9.8 A the 500 W default gives.
    assert [c[0][2]["value"] for c in _register_writes(mock_call)] == [70.0]


async def test_update_entity_refreshes_the_status_without_writing(
    hass, hub_entry, inverter_entry
):
    """``homeassistant.update_entity`` is a service any automation can call at
    any rate. It must only re-read what the last cycle recorded: writing here
    would be a second writer nothing serializes, able to overlap the cycle's own
    write and to spend the min-interval budget outside it."""
    sensor = await _add_charge_control(hass, inverter_entry)

    with _accepting_register(hass), _advice_cycle(inverter_entry, 2560.0):
        await _run_site_cycle(hass, hub_entry)

    with patch(
        "homeassistant.core.ServiceRegistry.async_call", new_callable=AsyncMock
    ) as mock_call:
        for _ in range(10):
            await sensor.async_update()

    assert _register_writes(mock_call) == []
    # The register is at 50 A by now (the inverter took the write), but the last
    # cycle read it back at 100 A before writing — and re-reading what the cycle
    # recorded is exactly all this does. It reads no register of its own: that
    # would be a second reader on a device the control loop owns, running at
    # whatever rate an automation calls the service.
    assert sensor.native_value == 100.0
    assert sensor.extra_state_attributes["control_state"] == CONTROL_STATE_LIMITING


async def test_the_unit_and_device_class_follow_the_configured_register_unit(
    hass, inverter_entry, inverter_entry_watts
):
    """The register's unit is a per-entry choice — a Deye counts DC amps, other
    hybrids watts — so the sensor's unit, device class and precision follow the
    entry rather than being fixed. Getting this wrong is not cosmetic: HA rejects
    a unit its device class does not recognise, and the sensor would have no
    statistics at all.
    """
    amps = await _add_charge_control(hass, inverter_entry)
    watts = await _add_charge_control(hass, inverter_entry_watts)

    assert amps.native_unit_of_measurement == CHARGE_LIMIT_UNIT_AMPS
    assert amps.device_class == SensorDeviceClass.CURRENT
    assert amps.suggested_display_precision == 1

    assert watts.native_unit_of_measurement == CHARGE_LIMIT_UNIT_WATTS
    assert watts.device_class == SensorDeviceClass.POWER
    assert watts.suggested_display_precision == 0

    # Either way it is a measurement — that is what earns long-term statistics,
    # and what the text state it replaced could never have.
    for sensor in (amps, watts):
        assert sensor.state_class == SensorStateClass.MEASUREMENT
    # Amps is the default: an entry that never chose (the fixture's own case is
    # explicit only for watts) still gets a coherent unit/class pair.
    assert CONF_CHARGE_LIMIT_UNIT not in inverter_entry.options


async def test_a_watts_register_is_reported_in_watts(
    hass, hub_entry, inverter_entry_watts
):
    """The advice is computed in watts, so a watts register takes it unconverted —
    and the value graphs in watts with no battery voltage involved anywhere."""
    sensor = await _add_charge_control(hass, inverter_entry_watts, register="5000")

    with _accepting_register(hass, maximum=5000) as mock_call, _advice_cycle(
        inverter_entry_watts, 2560.0
    ):
        await _run_site_cycle(hass, hub_entry)
        assert _register_writes(mock_call)[0][0][2]["value"] == 2560.0
        # Second cycle: the inverter has taken it, nothing new is written.
        await _run_site_cycle(hass, hub_entry)

    assert len(_register_writes(mock_call)) == 1
    assert sensor.native_value == 2560.0
    assert sensor.native_unit_of_measurement == CHARGE_LIMIT_UNIT_WATTS
    assert sensor.extra_state_attributes["recommended_value"] == 2560.0
    assert sensor.extra_state_attributes["normal_value"] == 5000.0


async def test_an_unreadable_register_reports_unknown(
    hass, hub_entry, inverter_entry
):
    """No read-back, no value: None (unknown), never a held number and never 0 —
    a 0 A charge limit is a real and very different claim.

    The entity stays available through it. That is the deliberate half of the
    availability decision: the thing that failed is the reading, and the reading
    says so itself, while ``control_state`` still reports what our control is
    doing about it.
    """
    sensor = await _add_charge_control(hass, inverter_entry)

    with _accepting_register(hass), _advice_cycle(inverter_entry, 2560.0):
        await _run_site_cycle(hass, hub_entry)
        assert sensor.native_value == 100.0

        # The Modbus link drops: the number entity goes unavailable.
        hass.states.async_set(CHARGE_TARGET, STATE_UNAVAILABLE)
        await _run_site_cycle(hass, hub_entry)

    assert sensor.native_value is None
    assert sensor.available is True
    assert sensor.extra_state_attributes["control_state"] == CONTROL_STATE_LIMITING


async def test_removing_the_entity_releases_its_worker_slot(
    hass, hub_entry, inverter_entry
):
    """An unloaded inverter entry must stop being driven — otherwise a removed
    entity keeps writing to a register nobody is watching."""
    sensor = await _add_charge_control(hass, inverter_entry)
    workers = hass.data[DOMAIN][SITE_CYCLE_WORKERS][hub_entry.entry_id]
    assert list(workers.values()) == [sensor]

    # What HA's Entity.async_remove() does with the callbacks async_on_remove
    # collected. Reproduced here because this entity was never added to an
    # entity platform, so the real removal path has no platform state to unwind.
    assert sensor._on_remove, "the worker registered no removal callback"
    while sensor._on_remove:
        sensor._on_remove.pop()()

    assert workers == {}
    with patch(
        "homeassistant.core.ServiceRegistry.async_call", new_callable=AsyncMock
    ) as mock_call, _advice_cycle(inverter_entry, 2560.0):
        await _run_site_cycle(hass, hub_entry)

    assert _register_writes(mock_call) == []


# ── Inverter SOC control: one ceiling, several time-of-use slots ───────
#
# The SOC twin of the block above, and the half these tests exist for is the
# fan-out: on a Deye the "charge up to %" ceiling is not one register but one
# `number` per time-of-use slot, so this control drives a LIST of entities from
# a single recommendation. What is pinned here is the entity-level half — the
# sensor and switch appearing only when slots are configured, the writes going
# out through the real coordinator cycle, and the sensor reporting the ceiling
# being enforced with the per-slot read-backs beside it.
#
# The min()/deadband/pacing contract itself is in dev/tests/test_inverter_control.py,
# which also runs in the pure tier.

SOC_SLOTS = ["number.deye_tou_soc_1", "number.deye_tou_soc_2"]
SOC_NORMAL_ENTITY = "input_number.battery_ceiling"


@pytest.fixture
def soc_inverter_entry(hub_entry: MockConfigEntry) -> MockConfigEntry:
    """An inverter entry that drives two SOC slots and no charge register.

    Deliberately no CONF_CHARGE_LIMIT_ENTITY_ID: this is the configuration that
    proves the SOC control does not depend on the charge-rate one being set up,
    which is why it is a site-cycle worker in its own right.
    """
    return MockConfigEntry(
        domain=DOMAIN,
        version=2,
        minor_version=2,
        title="Deye TOU",
        data={
            CONF_NAME: "Deye TOU",
            CONF_ENTITY_ID: "deye_tou",
            ENTRY_TYPE: ENTRY_TYPE_INVERTER,
            CONF_HUB_ENTRY_ID: hub_entry.entry_id,
        },
        options={
            CONF_SOC_LIMIT_ENTITY_IDS: list(SOC_SLOTS),
            CONF_CHARGE_CONTROL_INTERVAL: 300,
        },
    )


@pytest.fixture
def soc_inverter_entry_with_normal(hub_entry: MockConfigEntry) -> MockConfigEntry:
    """The same, plus a live normal-ceiling entity the user's automations own."""
    return MockConfigEntry(
        domain=DOMAIN,
        version=2,
        minor_version=2,
        title="Deye TOU",
        data={
            CONF_NAME: "Deye TOU",
            CONF_ENTITY_ID: "deye_tou",
            ENTRY_TYPE: ENTRY_TYPE_INVERTER,
            CONF_HUB_ENTRY_ID: hub_entry.entry_id,
        },
        options={
            CONF_SOC_LIMIT_ENTITY_IDS: list(SOC_SLOTS),
            CONF_SOC_LIMIT_NORMAL_ENTITY_ID: SOC_NORMAL_ENTITY,
            CONF_CHARGE_CONTROL_INTERVAL: 300,
        },
    )


@pytest.fixture
def dual_control_inverter_entry(hub_entry: MockConfigEntry) -> MockConfigEntry:
    """An inverter running BOTH write-controls — the Deye case in full."""
    return MockConfigEntry(
        domain=DOMAIN,
        version=2,
        minor_version=2,
        title="Deye Both",
        data={
            CONF_NAME: "Deye Both",
            CONF_ENTITY_ID: "deye_both",
            ENTRY_TYPE: ENTRY_TYPE_INVERTER,
            CONF_HUB_ENTRY_ID: hub_entry.entry_id,
        },
        options={
            CONF_CHARGE_LIMIT_ENTITY_ID: CHARGE_TARGET,
            CONF_SOC_LIMIT_ENTITY_IDS: list(SOC_SLOTS),
            CONF_CHARGE_CONTROL_INTERVAL: 300,
        },
    )


async def _add_soc_control(hass, inverter_entry, *, armed=True, slots=100, normal=None):
    """Create the SOC-control sensor and let it join its hub's site cycle.

    Registration goes through async_added_to_hass, the production path, so the
    registration itself is under test — as with the charge-control sensor, there
    is no hub coordinator in these tests at all.
    """
    for entity_id in soc_targets(inverter_entry):
        hass.states.async_set(entity_id, str(slots), {"max": 100})
    if normal is not None:
        hass.states.async_set(SOC_NORMAL_ENTITY, str(normal))
    hass.data.setdefault(DOMAIN, {}).setdefault("inverters", {})[
        inverter_entry.entry_id
    ] = {INVERTER_RT_SOC_CONTROL_ENABLED: armed}
    sensor = LoadJugglerInverterSocControlSensor(hass, inverter_entry, "deye_tou")
    await sensor.async_added_to_hass()
    return sensor


def _soc_advice_cycle(inverter_entry, advice_soc):
    """Run the cycle with one crafted recommended max SOC.

    The engine is patched out for the same reason as in the charge-limit block:
    these tests are about who writes the slots and when, not about how the
    clipping forecast arrives at a ceiling.
    """
    return patch(
        "custom_components.dynamic_ocpp_evse.sensor.run_hub_calculation",
        return_value={
            "inverters": {
                inverter_entry.entry_id: {"forecast_battery_max_soc": advice_soc}
            },
        },
    )


def _slot_writes(mock_call):
    """(entity_id, value) of every set_value call the cycle made, any domain."""
    return [
        (c[0][2]["entity_id"], c[0][2]["value"])
        for c in mock_call.call_args_list
        if c[0][1] == "set_value"
    ]


def _accepting_slots(hass):
    """Patch the service registry with slots that ACCEPT what we write.

    Without this a bare AsyncMock swallows the write and the slots sit at their
    starting value forever, so the per-slot deadband could never be observed.
    """

    async def _apply(domain, service, data, *args, **kwargs):
        if service == "set_value":
            hass.states.async_set(str(data["entity_id"]), str(data["value"]),
                                  {"max": 100})

    return patch(
        "homeassistant.core.ServiceRegistry.async_call",
        new_callable=AsyncMock,
        side_effect=_apply,
    )


async def test_soc_control_registers_as_its_own_site_cycle_worker(
    hass, hub_entry, soc_inverter_entry
):
    """It has to be its own worker: this entry configures no charge-limit
    register, so there is no charge-control sensor here to ride."""
    sensor = await _add_soc_control(hass, soc_inverter_entry)

    workers = hass.data[DOMAIN][SITE_CYCLE_WORKERS][hub_entry.entry_id]
    assert list(workers.values()) == [sensor]
    assert sensor.should_poll is False
    # Nothing enforced yet, so no value — unknown, not 100, which would be a
    # claim about the slots we are not making.
    assert sensor.native_value is None
    assert sensor.extra_state_attributes["control_state"] == CONTROL_STATE_OFF
    assert sensor.extra_state_attributes["seconds_since_write"] is None
    # Its unit and device class match the Recommended Battery Max SOC sensor's,
    # so the recommendation and what was enforced share one axis.
    assert sensor.native_unit_of_measurement == "%"
    assert sensor.device_class == SensorDeviceClass.BATTERY
    assert sensor.state_class == SensorStateClass.MEASUREMENT


async def test_the_site_cycle_writes_every_configured_slot(
    hass, hub_entry, soc_inverter_entry
):
    """The fan-out through the real cycle: one recommendation, both slots."""
    sensor = await _add_soc_control(hass, soc_inverter_entry)

    with _accepting_slots(hass) as mock_call, _soc_advice_cycle(
        soc_inverter_entry, 70.0
    ):
        await _run_site_cycle(hass, hub_entry)

        assert _slot_writes(mock_call) == [(eid, 70.0) for eid in SOC_SLOTS]
        # The state is what is being enforced — the min() of the recommendation
        # and the normal ceiling, which here defaults to 100.
        assert sensor.native_value == 70.0
        attributes = sensor.extra_state_attributes
        assert attributes["control_state"] == CONTROL_STATE_LIMITING
        assert attributes["recommended_value"] == 70.0
        assert attributes["normal_value"] == 100.0
        # Read BEFORE the write, so these are what the inverter last reported.
        assert attributes["slot_values"] == {eid: 100.0 for eid in SOC_SLOTS}
        assert attributes["seconds_since_write"] == 0

        # The slots took the 70. The next cycle is paced out AND every slot is
        # inside the deadband, so nothing is written and the read-backs move.
        await _run_site_cycle(hass, hub_entry)

    assert _slot_writes(mock_call) == [(eid, 70.0) for eid in SOC_SLOTS]
    assert sensor.extra_state_attributes["slot_values"] == {
        eid: 70.0 for eid in SOC_SLOTS
    }
    assert sensor.native_value == 70.0


async def test_nothing_is_written_while_the_soc_switch_is_off(
    hass, hub_entry, soc_inverter_entry
):
    """Default off, checked per call — a faster cadence cannot leak a write."""
    sensor = await _add_soc_control(hass, soc_inverter_entry, armed=False)

    with _accepting_slots(hass) as mock_call, _soc_advice_cycle(
        soc_inverter_entry, 70.0
    ):
        for _ in range(5):
            await _run_site_cycle(hass, hub_entry)

    assert _slot_writes(mock_call) == []
    assert sensor.native_value is None
    assert sensor.extra_state_attributes["control_state"] == CONTROL_STATE_OFF
    # The slots are still read while disarmed, so the attribute keeps reporting
    # what their owner has them at.
    assert sensor.extra_state_attributes["slot_values"] == {
        eid: 100.0 for eid in SOC_SLOTS
    }


async def test_the_normal_entity_owns_the_ceiling_and_the_forecast_only_lowers_it(
    hass, hub_entry, soc_inverter_entry_with_normal
):
    """min() at the entity level: an owner asking for 80 gets 80 while the
    forecast is happy with 95."""
    soc_inverter_entry = soc_inverter_entry_with_normal
    sensor = await _add_soc_control(hass, soc_inverter_entry, normal=80)

    with _accepting_slots(hass) as mock_call, _soc_advice_cycle(
        soc_inverter_entry, 95.0
    ):
        await _run_site_cycle(hass, hub_entry)

    assert _slot_writes(mock_call) == [(eid, 80.0) for eid in SOC_SLOTS]
    assert sensor.native_value == 80.0
    # Holding the owner's own ceiling is idle, not limiting.
    assert sensor.extra_state_attributes["control_state"] == CONTROL_STATE_IDLE
    assert sensor.extra_state_attributes["normal_entity"] == SOC_NORMAL_ENTITY


async def test_an_unreadable_normal_entity_defers_the_writes(
    hass, hub_entry, soc_inverter_entry_with_normal
):
    """We never invent somebody else's setting: with the normal ceiling
    unavailable the cycle writes nothing and reports no enforced value."""
    soc_inverter_entry = soc_inverter_entry_with_normal
    sensor = await _add_soc_control(hass, soc_inverter_entry, normal=80)
    hass.states.async_set(SOC_NORMAL_ENTITY, STATE_UNAVAILABLE)

    with _accepting_slots(hass) as mock_call, _soc_advice_cycle(
        soc_inverter_entry, 70.0
    ):
        await _run_site_cycle(hass, hub_entry)

    assert _slot_writes(mock_call) == []
    assert sensor.native_value is None
    assert sensor.extra_state_attributes["normal_value"] is None
    # The slots themselves are still reported — only the ceiling is unknown.
    assert sensor.extra_state_attributes["slot_values"] == {
        eid: 100.0 for eid in SOC_SLOTS
    }


async def test_an_unreadable_slot_is_skipped_and_its_sibling_is_written(
    hass, hub_entry, soc_inverter_entry
):
    """One dead slot degrades this to partial control, not to none."""
    sensor = await _add_soc_control(hass, soc_inverter_entry)
    hass.states.async_set(SOC_SLOTS[0], STATE_UNAVAILABLE)

    with _accepting_slots(hass) as mock_call, _soc_advice_cycle(
        soc_inverter_entry, 70.0
    ):
        await _run_site_cycle(hass, hub_entry)

    assert _slot_writes(mock_call) == [(SOC_SLOTS[1], 70.0)]
    # And the attribute says which one is missing, so it is diagnosable without
    # reading the log.
    assert sensor.extra_state_attributes["slot_values"] == {
        SOC_SLOTS[0]: None,
        SOC_SLOTS[1]: 100.0,
    }


async def test_the_soc_sensor_goes_unavailable_with_a_dead_site_cycle(
    hass, hub_entry, soc_inverter_entry
):
    """Unlike the charge-control sensor beside it. That one measures a device
    register, which stays true while nobody writes it; this one reports our own
    intention, which only exists while the cycle computing it runs."""
    sensor = await _add_soc_control(hass, soc_inverter_entry)

    # Before any cycle there is no publication to be fresh.
    assert sensor.available is False

    with _accepting_slots(hass), _soc_advice_cycle(soc_inverter_entry, 70.0):
        await _run_site_cycle(hass, hub_entry)

    assert sensor.available is True


async def test_soc_update_entity_refreshes_without_writing(
    hass, hub_entry, soc_inverter_entry
):
    """``homeassistant.update_entity`` must not become a second writer on the
    slots, at whatever rate an automation calls it."""
    sensor = await _add_soc_control(hass, soc_inverter_entry)

    with _accepting_slots(hass), _soc_advice_cycle(soc_inverter_entry, 70.0):
        await _run_site_cycle(hass, hub_entry)

    with patch(
        "homeassistant.core.ServiceRegistry.async_call", new_callable=AsyncMock
    ) as mock_call:
        for _ in range(10):
            await sensor.async_update()

    assert _slot_writes(mock_call) == []
    assert sensor.native_value == 70.0


async def test_removing_the_soc_sensor_releases_its_worker_slot(
    hass, hub_entry, soc_inverter_entry
):
    """An unloaded inverter entry must stop being driven."""
    sensor = await _add_soc_control(hass, soc_inverter_entry)
    workers = hass.data[DOMAIN][SITE_CYCLE_WORKERS][hub_entry.entry_id]
    assert list(workers.values()) == [sensor]

    assert sensor._on_remove, "the worker registered no removal callback"
    while sensor._on_remove:
        sensor._on_remove.pop()()

    assert workers == {}
    with _accepting_slots(hass) as mock_call, _soc_advice_cycle(
        soc_inverter_entry, 70.0
    ):
        await _run_site_cycle(hass, hub_entry)

    assert _slot_writes(mock_call) == []


# ── Platform gating: each control's entities need its own target ───────


async def test_the_soc_entities_exist_only_when_slots_are_configured(
    hass, hub_entry, inverter_entry, soc_inverter_entry, dual_control_inverter_entry
):
    """The two write-controls gate independently. The charge-rate entry gets a
    Charge Control sensor and no SOC one; the TOU entry the reverse; an inverter
    running both gets both, each driving its own control."""
    from custom_components.dynamic_ocpp_evse.sensor import (
        async_setup_entry as sensor_setup,
    )

    async def _created(entry):
        added = []
        await sensor_setup(hass, entry, lambda entities, *a, **kw: added.extend(entities))
        return {type(e).__name__ for e in added}

    charge_side = await _created(inverter_entry)
    assert "LoadJugglerInverterChargeControlSensor" in charge_side
    assert "LoadJugglerInverterSocControlSensor" not in charge_side

    soc_side = await _created(soc_inverter_entry)
    assert "LoadJugglerInverterSocControlSensor" in soc_side
    assert "LoadJugglerInverterChargeControlSensor" not in soc_side

    both = await _created(dual_control_inverter_entry)
    assert {
        "LoadJugglerInverterChargeControlSensor",
        "LoadJugglerInverterSocControlSensor",
    } <= both


async def test_the_soc_switch_appears_only_when_slots_are_configured(
    hass, hub_entry, inverter_entry, soc_inverter_entry, dual_control_inverter_entry
):
    """Same gate on the switch platform — an opt-in with nothing to write to
    would be a lie, and the two switches are independent."""
    from custom_components.dynamic_ocpp_evse.switch import (
        async_setup_entry as switch_setup,
    )

    async def _created(entry):
        added = []
        await switch_setup(hass, entry, lambda entities, *a, **kw: added.extend(entities))
        return {type(e).__name__ for e in added}

    assert await _created(inverter_entry) == {"BatteryChargeControlSwitch"}
    assert await _created(soc_inverter_entry) == {"BatterySocControlSwitch"}
    # Both configured on one inverter: both switches, armed separately.
    assert await _created(dual_control_inverter_entry) == {
        "BatteryChargeControlSwitch",
        "BatterySocControlSwitch",
    }


# ── The connector-status sensor is resolved, not composed ──────────────


def _renamed_status_sensor(hass, charge_point_id, object_id):
    """An ocpp charge point whose status sensor was renamed by the user."""
    ocpp_entry = MockConfigEntry(domain="ocpp", title="OCPP")
    ocpp_entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=ocpp_entry.entry_id,
        identifiers={("ocpp", charge_point_id)},
        name=charge_point_id,
    )
    return (
        er.async_get(hass)
        .async_get_or_create(
            "sensor",
            "ocpp",
            f"ocpp.{charge_point_id}.status_connector.sensor",
            suggested_object_id=object_id,
            original_name="Status Connector",
            config_entry=ocpp_entry,
            device_id=device.id,
        )
        .entity_id
    )


async def test_engine_reads_the_resolved_connector_status_entity(
    hass, hub_entry, charger_entry, setup_domain_data
):
    """The whole point of the fix, end to end through the load builder.

    The composed ``sensor.test_charger_status_connector`` exists and says
    "Available"; the charger's REAL status sensor was renamed and says
    "Charging". Reading the wrong one strands a charging car at 0 A.
    """
    from custom_components.dynamic_ocpp_evse.engine.load_builders import (
        _build_evse_load,
    )

    charger_entry.add_to_hass(hass)
    renamed = _renamed_status_sensor(hass, "ocpp_device_1", "garage_wallbox_state")
    hass.states.async_set(renamed, "Charging")
    hass.states.async_set("sensor.test_charger_status_connector", "Available")

    load = _build_evse_load(hass, charger_entry, 230, "test_charger", 1)

    assert load.connector_status == "Charging"

    # And the entity the command layer checks (control/ocpp.py, compliance)
    # is the same one, off the same cached resolution.
    sensor = LoadJugglerDeviceSensor(
        hass, charger_entry, hub_entry, "Test Charger", "test_charger"
    )
    assert sensor._connector_status_entity == renamed == "sensor.garage_wallbox_state"


async def test_composed_status_name_still_used_without_a_registry_entry(
    hass, hub_entry, charger_entry, setup_domain_data
):
    """Template-sensor sites keep working — nothing to classify, so guess."""
    from custom_components.dynamic_ocpp_evse.engine.load_builders import (
        _build_evse_load,
    )

    charger_entry.add_to_hass(hass)
    hass.states.async_set("sensor.test_charger_status_connector", "SuspendedEV")

    load = _build_evse_load(hass, charger_entry, 230, "test_charger", 1)

    assert load.connector_status == "SuspendedEV"


# ── The Excess verdict counts only the rate the battery MAY take ───────
#
# The engine half of the narrowing. The charge control publishes what it is
# holding a member's register at (INVERTER_RT_ENFORCED_CHARGE_W, written by
# control/inverter.py and covered in dev/tests/test_inverter_control.py); the
# fleet read picks it up per member, and the Excess verdict's battery allowance
# becomes the sum of the PERMITTED rates instead of the nameplate ones.
#
# One cycle behind by nature: the register write is a site-cycle worker that runs
# after the result is published, so a cycle can only know what the previous
# cycle's write enforced. That is what these drive — the runtime dict as the
# hand-off, and the published excess_margin_power as the visible consequence.

CLIP_EXPORT_LIMIT = 9200.0  # W — the site's hard export limit
CLIP_EXPORT_A = 40.0  # A on phase A = 9200 W leaving the site
CLIP_THRESHOLD = CLIP_EXPORT_LIMIT - 500.0  # the default trigger margin below it
ENFORCING_NAMEPLATE = 10000.0
ADVICE_ONLY_NAMEPLATE = 4000.0
ENFORCED_RATE = 6500.0


@pytest.fixture
def clipping_hub() -> MockConfigEntry:
    """A one-phase hub with an export limit and no battery of its own.

    No hub-level battery fields on purpose: every battery belongs to an inverter
    entry here, so the fleet is exactly the two members below and each one's
    share of the allowance is attributable.
    """
    return MockConfigEntry(
        domain=DOMAIN,
        version=2,
        minor_version=2,
        title="Clipping Hub",
        data={
            CONF_NAME: "Clipping Hub",
            CONF_ENTITY_ID: "clipping_hub",
            ENTRY_TYPE: ENTRY_TYPE_HUB,
        },
        options={
            CONF_PHASE_A_CURRENT_ENTITY_ID: "sensor.clip_phase_a",
            CONF_MAIN_BREAKER_RATING: 63,
            CONF_PHASE_VOLTAGE: 230,
            CONF_GRID_EXPORT_LIMIT: CLIP_EXPORT_LIMIT,
        },
    )


def _battery_inverter(hub, title, entity_id, charge_cap):
    return MockConfigEntry(
        domain=DOMAIN,
        version=2,
        minor_version=2,
        title=title,
        data={
            CONF_NAME: title,
            CONF_ENTITY_ID: entity_id,
            ENTRY_TYPE: ENTRY_TYPE_INVERTER,
            CONF_HUB_ENTRY_ID: hub.entry_id,
        },
        options={
            CONF_BATTERY_SOC_ENTITY_ID: f"sensor.{entity_id}_soc",
            CONF_BATTERY_POWER_ENTITY_ID: f"sensor.{entity_id}_power",
            CONF_BATTERY_MAX_CHARGE_POWER: charge_cap,
            CONF_BATTERY_MAX_DISCHARGE_POWER: charge_cap,
        },
    )


def _clipping_site_states(hass, enforcing, advice_only):
    """Midday: 9.2 kW leaving the site, both batteries pinned at their own rate.

    The enforcing member is charging at the 6.5 kW its register is being held to,
    the advice-only one at its full 4 kW plate — so the site is placing every watt
    it can, which is exactly the state the verdict has to recognise.
    """
    hass.states.async_set(
        "sensor.clip_phase_a", str(-CLIP_EXPORT_A),
        {"device_class": "current", "unit_of_measurement": "A"},
    )
    for entry, power in ((enforcing, -ENFORCED_RATE), (advice_only, -ADVICE_ONLY_NAMEPLATE)):
        name = entry.data[CONF_ENTITY_ID]
        hass.states.async_set(
            f"sensor.{name}_soc", "70",
            {"device_class": "battery", "unit_of_measurement": "%"},
        )
        hass.states.async_set(
            f"sensor.{name}_power", str(power),
            {"device_class": "power", "unit_of_measurement": "W"},
        )


@pytest.fixture
def clipping_fleet(hass, clipping_hub):
    """The hub, its two battery inverters and the runtime buckets, wired up."""
    enforcing = _battery_inverter(
        clipping_hub, "Enforcing Hybrid", "enforcing", ENFORCING_NAMEPLATE
    )
    advice_only = _battery_inverter(
        clipping_hub, "Advice Only Hybrid", "advice_only", ADVICE_ONLY_NAMEPLATE
    )
    for entry in (clipping_hub, enforcing, advice_only):
        entry.add_to_hass(hass)
    hass.data[DOMAIN] = {
        "hubs": {clipping_hub.entry_id: {"entry": clipping_hub, "loads": []}},
        "loads": {},
        "load_allocations": {},
        "inverters": {enforcing.entry_id: {}, advice_only.entry_id: {}},
    }
    _clipping_site_states(hass, enforcing, advice_only)
    return clipping_hub, enforcing, advice_only


async def test_the_enforced_rate_round_trips_from_the_runtime_into_the_verdict(
    hass, clipping_fleet
):
    """The hand-off, end to end.

    Cycle one: nothing is being held back, so the allowance is the two nameplate
    rates (14 kW) and the site — placing 9.2 kW of export plus 10.5 kW of
    charging — reads 3 kW short of Excess. That is the bug: a clipping window
    reported as a site with room to spare.

    Cycle two: the charge control has written its limit and recorded the 6.5 kW
    it is holding the enforcing member to. The allowance becomes the 10.5 kW the
    two batteries may actually take, and the same readings read +500 W — the
    watts the site is genuinely placing beyond the Excess threshold.
    """
    from custom_components.dynamic_ocpp_evse.engine.hub_calculation import (
        run_hub_calculation,
    )

    hub, enforcing, _advice_only = clipping_fleet

    nameplate = run_hub_calculation(hass, hub)["excess_margin_power"]
    assert nameplate == pytest.approx(
        CLIP_EXPORT_LIMIT
        + ENFORCED_RATE
        + ADVICE_ONLY_NAMEPLATE
        - (CLIP_THRESHOLD + ENFORCING_NAMEPLATE + ADVICE_ONLY_NAMEPLATE),
        abs=1.0,
    )
    assert nameplate < 0

    hass.data[DOMAIN]["inverters"][enforcing.entry_id][
        INVERTER_RT_ENFORCED_CHARGE_W
    ] = ENFORCED_RATE

    enforced = run_hub_calculation(hass, hub)["excess_margin_power"]

    # Only the enforcing member's share narrowed: the whole difference is the
    # 3.5 kW its register is forbidding, and the advice-only member's 4 kW plate
    # is still counted in full.
    assert enforced - nameplate == pytest.approx(
        ENFORCING_NAMEPLATE - ENFORCED_RATE, abs=1.0
    )
    assert enforced == pytest.approx(500.0, abs=1.0)
    assert enforced > 0


async def test_an_advice_only_fleet_keeps_its_nameplate_allowance(
    hass, clipping_fleet
):
    """Neither switch armed: nothing is written to either inverter, so both
    batteries really do still charge at their plate rate. Narrowing on the mere
    existence of a forecast advice would report an allowance the site does not
    have and engage Excess against a battery still free to absorb."""
    from custom_components.dynamic_ocpp_evse.engine.hub_calculation import (
        run_hub_calculation,
    )

    hub, enforcing, advice_only = clipping_fleet
    # What an unarmed control leaves behind: the runtime bucket exists (its
    # sensor is running) and says nothing is being held back.
    for entry in (enforcing, advice_only):
        hass.data[DOMAIN]["inverters"][entry.entry_id][
            INVERTER_RT_ENFORCED_CHARGE_W
        ] = None

    result = run_hub_calculation(hass, hub)

    assert result["excess_margin_power"] == pytest.approx(-3000.0, abs=1.0)


async def test_a_released_limit_hands_the_nameplate_allowance_back(
    hass, clipping_fleet
):
    """Evening: the forecast releases, the control restores full rate and clears
    what it was enforcing. The allowance must widen again in the same cycle the
    battery is free — a narrowing that outlived the limit would hold Excess on
    against a battery with real headroom."""
    from custom_components.dynamic_ocpp_evse.engine.hub_calculation import (
        run_hub_calculation,
    )

    hub, enforcing, _advice_only = clipping_fleet
    rt = hass.data[DOMAIN]["inverters"][enforcing.entry_id]

    rt[INVERTER_RT_ENFORCED_CHARGE_W] = ENFORCED_RATE
    assert run_hub_calculation(hass, hub)["excess_margin_power"] > 0

    rt[INVERTER_RT_ENFORCED_CHARGE_W] = None
    assert run_hub_calculation(hass, hub)["excess_margin_power"] < 0


# --- The observers run on a PER-INVERTER forecast -----------------------------
#
# Every other forecast rig here configures the source hub-level
# (CONF_SOLAR_FORECAST_ENTITY_IDS, the legacy field), which leaves
# ``FleetMember.forecast_device_ids`` empty — so the gain observer's loop, which
# keys on exactly that, never executed in any test. It shipped a NameError to a
# live site (2026-08-31: merge_forecast_series used in hub_result and never
# imported there). This rig is the one that walks that loop.


def _forecast_device(hass, slug, watts):
    """A per-array Open-Meteo device with a watts-bearing sensor, as the
    integration creates them. Returns the device id."""
    from homeassistant.helpers import device_registry as dr, entity_registry as er

    source = MockConfigEntry(domain="open_meteo_solar_forecast", title=slug)
    source.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=source.entry_id,
        identifiers={("open_meteo_solar_forecast", slug)},
        name=slug,
    )
    reg = er.async_get(hass).async_get_or_create(
        "sensor",
        "open_meteo_solar_forecast",
        f"{slug}_energy_production_today",
        device_id=device.id,
        config_entry=source,
        suggested_object_id=f"{slug}_energy_production_today",
    )
    hass.states.async_set(
        reg.entity_id, "12.5", {"unit_of_measurement": "kWh", "watts": dict(watts)}
    )
    return device.id


async def test_a_per_inverter_forecast_drives_the_observers(hass: HomeAssistant):
    """The cycle completes and the gain observation reaches the inverter.

    Without the per-inverter source this loop is skipped entirely, which is how
    a missing import reached a real site: every existing rig configures the
    forecast hub-level.
    """
    from freezegun import freeze_time
    from custom_components.dynamic_ocpp_evse.const import (
        CONF_SOLAR_FORECAST_DEVICE_IDS,
    )
    from custom_components.dynamic_ocpp_evse.engine.hub_calculation import (
        run_hub_calculation,
    )

    hub, inverter, _runtime = _no_clip_rig(hass, "perinv", soc=95, solar_w=4000.0)
    device_id = _forecast_device(hass, "perinv_array", _NO_CLIP_DAY)
    # Move the source onto the inverter, which is where a current install has
    # it — the hub keeps none, so only the per-inverter path can supply a series.
    hass.config_entries.async_update_entry(
        inverter, options={**inverter.options,
                           CONF_SOLAR_FORECAST_DEVICE_IDS: [device_id]}
    )

    with freeze_time("2026-08-25 09:30:00+00:00"):
        result = run_hub_calculation(hass, hub)
        # Two cycles: the first only stamps the monotonic clock, so the second
        # is the one that actually accumulates a sample.
        result = run_hub_calculation(hass, hub)

    assert result is not None, "the cycle must not raise"
    own = result["inverters"][inverter.entry_id]
    # The observation is published even before a day has closed: the running
    # gain starts at 1.0 with no days behind it.
    assert own["forecast_gain"] == 1.0
    assert own["forecast_gain_days"] == 0
    assert "forecast_accuracy_pct" in own
    # And the hub carries the site-level observers.
    assert "forecast_peakiness_pct" in result
    assert "forecast_clipped_actual_kwh" in result


# --- Inverter sensor definitions --------------------------------------------
#
# The hub table has had a round-trip test since it existed; the inverter one had
# none at all, so a definition could name a data_key nothing publishes, or miss
# a translation, and only a real install would notice. Same shape of hole as the
# unreachable observer loop above, one layer up.


def _inverter_defn_sensors(hass, inverter_entry):
    from custom_components.dynamic_ocpp_evse.entities.inverter import (
        INVERTER_SENSOR_DEFINITIONS,
        LoadJugglerInverterDataSensor,
    )

    return INVERTER_SENSOR_DEFINITIONS, [
        LoadJugglerInverterDataSensor(hass, inverter_entry, "test_inv", defn)
        for defn in INVERTER_SENSOR_DEFINITIONS
    ]


async def test_inverter_data_sensors_initialize(hass: HomeAssistant):
    """Every definition builds a sensor whose properties come from it.

    Constructing them is itself the assertion for a definition missing a key
    the constructor indexes — which is exactly how a `%s` sensor with no
    device_class would have failed before that lookup became optional.
    """
    hub, inverter, _rt = _no_clip_rig(hass, "invdefn", soc=90)
    defns, sensors = _inverter_defn_sensors(hass, inverter)

    assert len(sensors) == len(defns)
    for sensor, defn in zip(sensors, defns):
        assert sensor.native_value is None  # nothing published yet
        assert sensor.native_unit_of_measurement == defn["unit"]
        assert sensor.device_class == defn.get("device_class")
        assert sensor.state_class == defn.get(
            "state_class", SensorStateClass.MEASUREMENT
        )
        # unique_id_suffix doubles as the translation key, so it must be the
        # stable identifier in both places.
        assert sensor.unique_id == f"test_inv_{defn['unique_id_suffix']}"
        assert sensor.translation_key == defn["unique_id_suffix"]


async def test_every_inverter_sensor_has_a_name_translation(hass: HomeAssistant):
    """A sensor whose translation key is missing shows the raw key as its name
    in every language — cosmetic, invisible in tests, and permanent."""
    import json
    from pathlib import Path

    root = Path("custom_components/dynamic_ocpp_evse")
    hub, inverter, _rt = _no_clip_rig(hass, "invtrans", soc=90)
    defns, _sensors = _inverter_defn_sensors(hass, inverter)

    for name in ("strings.json", "translations/en.json", "translations/sl.json"):
        names = json.loads((root / name).read_text())["entity"]["sensor"]
        for defn in defns:
            key = defn["unique_id_suffix"]
            assert key in names, f"{name} has no entity.sensor.{key}"
            assert names[key].get("name"), f"{name}: {key} has no name"


async def test_battery_less_array_gets_the_accuracy_sensor(hass: HomeAssistant):
    """Forecast accuracy follows the array, not the battery.

    The engine's observer loop measures actual ÷ forecast for every member that
    OWNS a forecast device (hub_result gates on forecast_device_ids alone), so
    a pure AC-coupled PV inverter — no battery, no advice — computes a value
    every cycle. The setup gate used to inherit the advice sensors' battery
    requirement and silently dropped it. Both shapes through the real platform
    setup: the battery-less array gets accuracy and no battery/advice sensors;
    the battery inverter with no forecast device of its own gets the advice
    sensors and no accuracy.
    """
    from custom_components.dynamic_ocpp_evse import sensor as sensor_platform
    from custom_components.dynamic_ocpp_evse.const import (
        CONF_SOLAR_FORECAST_DEVICE_IDS,
    )

    hub = _destination_hub("acarray")
    hub.add_to_hass(hass)
    # The fleet's battery (capacity 20 kWh) — enables the site forecast.
    battery_inverter = _destination_inverter(hub, "acarray", "number.dst_normal")
    battery_inverter.add_to_hass(hass)
    # The battery-less AC-coupled array, owning its own forecast device.
    array = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        minor_version=4,
        title="AC Array",
        data={
            CONF_NAME: "AC Array",
            CONF_ENTITY_ID: "ac_array",
            ENTRY_TYPE: ENTRY_TYPE_INVERTER,
            CONF_HUB_ENTRY_ID: hub.entry_id,
        },
        options={
            CONF_SOLAR_PRODUCTION_ENTITY_ID: "sensor.ac_solar",
            CONF_SOLAR_FORECAST_DEVICE_IDS: ["ac-array-forecast-device"],
        },
    )
    array.add_to_hass(hass)
    hass.data[DOMAIN] = {
        "hubs": {hub.entry_id: {"loads": []}},
        "loads": {},
        "load_allocations": {},
        "inverters": {},
    }

    async def suffixes(entry):
        captured = []
        await sensor_platform.async_setup_entry(
            hass, entry, lambda new, **kw: captured.extend(new)
        )
        return {
            defn["unique_id_suffix"]
            for s in captured
            for defn in [getattr(s, "_defn", None)]
            if defn is not None
        }

    array_sensors = await suffixes(array)
    assert "forecast_accuracy" in array_sensors
    assert "solar_production" in array_sensors
    assert not array_sensors & {
        "battery_soc",
        "battery_power",
        "forecast_battery_max_soc",
        "forecast_charge_limit",
    }

    battery_sensors = await suffixes(battery_inverter)
    assert "forecast_battery_max_soc" in battery_sensors
    assert "forecast_charge_limit" in battery_sensors
    # No forecast device of its own → nothing for the observer to measure.
    assert "forecast_accuracy" not in battery_sensors


async def test_every_inverter_sensor_key_is_published_by_the_engine(
    hass: HomeAssistant,
):
    """A definition naming a data_key the engine never publishes reads unknown
    for ever — the sensor exists, is available, and says nothing.

    Run against the per-inverter forecast rig, so the forecast-gated keys are
    genuinely produced rather than skipped.
    """
    from freezegun import freeze_time
    from custom_components.dynamic_ocpp_evse.const import (
        CONF_SOLAR_FORECAST_DEVICE_IDS,
    )
    from custom_components.dynamic_ocpp_evse.engine.hub_calculation import (
        run_hub_calculation,
    )

    hub, inverter, _rt = _no_clip_rig(hass, "invkeys", soc=95, solar_w=4000.0)
    device_id = _forecast_device(hass, "invkeys_array", _NO_CLIP_DAY)
    hass.config_entries.async_update_entry(
        inverter,
        options={**inverter.options, CONF_SOLAR_FORECAST_DEVICE_IDS: [device_id]},
    )

    with freeze_time("2026-08-25 09:30:00+00:00"):
        run_hub_calculation(hass, hub)
        result = run_hub_calculation(hass, hub)

    own = result["inverters"][inverter.entry_id]
    defns, _sensors = _inverter_defn_sensors(hass, inverter)
    missing = [d["data_key"] for d in defns if d["data_key"] not in own]
    assert not missing, f"defined but never published: {missing}"


async def test_every_hub_sensor_key_is_published_by_the_engine(hass: HomeAssistant):
    """The same contract for the hub table. It had a properties round-trip but
    nothing tying a definition to a key the producer actually emits."""
    from freezegun import freeze_time
    from custom_components.dynamic_ocpp_evse.const import (
        CONF_SOLAR_FORECAST_DEVICE_IDS,
    )
    from custom_components.dynamic_ocpp_evse.engine.hub_calculation import (
        run_hub_calculation,
    )

    hub, inverter, _rt = _no_clip_rig(hass, "hubkeys", soc=95, solar_w=4000.0)
    device_id = _forecast_device(hass, "hubkeys_array", _NO_CLIP_DAY)
    hass.config_entries.async_update_entry(
        inverter,
        options={**inverter.options, CONF_SOLAR_FORECAST_DEVICE_IDS: [device_id]},
    )

    with freeze_time("2026-08-25 09:30:00+00:00"):
        run_hub_calculation(hass, hub)
        result = run_hub_calculation(hass, hub)

    missing = [
        d["hub_data_key"]
        for d in HUB_SENSOR_DEFINITIONS
        if d["hub_data_key"] not in result
    ]
    assert not missing, f"defined but never published: {missing}"


# --- SOC limit semantics: a write-side flag that never disturbs the read ------
#
# The flag says what the fan-out may WRITE into the slot registers (a floor
# register must never receive a lowered ceiling — the inverter would read it as
# a grid-charge target). It says nothing about where the pack should go: a Deye
# whose slot value doubles as the owner's charge target points the ceiling
# source at the slot, the reserve is carved below that number, and the band
# above it stays the export-holding buffer the engaged feedback fills. A floor
# whose value is NOT the target leaves the source unset — anchoring at 100% is
# that knob's job, not this flag's.


async def test_floor_semantics_does_not_disturb_the_destination_read(
    hass: HomeAssistant,
):
    """Declaring the entities a FLOOR changes nothing about the read side: the
    configured source is still the destination, the reserve is carved below its
    value, and a pack above it yields — identical to ceiling semantics."""
    from freezegun import freeze_time
    from custom_components.dynamic_ocpp_evse.const import (
        CONF_SOC_LIMIT_SEMANTICS,
        SOC_LIMIT_SEMANTICS_FLOOR,
    )
    from custom_components.dynamic_ocpp_evse.engine.hub_calculation import (
        run_hub_calculation,
    )

    hub, inverter, _rt = _no_clip_rig(hass, "floorsem", soc=93)
    # The source reads 90 — on this inverter a floor whose value is also the
    # owner's charge target, so it is the destination all the same.
    hass.states.async_set("number.dst_normal", "90", {"unit_of_measurement": "%"})
    hass.config_entries.async_update_entry(
        inverter,
        options={**inverter.options,
                 CONF_SOC_LIMIT_SEMANTICS: SOC_LIMIT_SEMANTICS_FLOOR},
    )

    with freeze_time("2026-08-25 09:30:00+00:00"):
        result = run_hub_calculation(hass, hub)

    own = result["inverters"][inverter.entry_id]
    # Same numbers the ceiling test below asserts: the flag is invisible here.
    assert own["forecast_battery_max_soc"] == 90
    assert own["forecast_charge_limiting"] is True


async def test_ceiling_semantics_still_reads_the_destination(hass: HomeAssistant):
    """The default is unchanged: an inverter that really does stop charging at
    the configured number keeps its destination, and a pack above it yields."""
    from freezegun import freeze_time
    from custom_components.dynamic_ocpp_evse.engine.hub_calculation import (
        run_hub_calculation,
    )

    hub, inverter, _rt = _no_clip_rig(hass, "ceilsem", soc=93)
    hass.states.async_set("number.dst_normal", "90", {"unit_of_measurement": "%"})

    with freeze_time("2026-08-25 09:30:00+00:00"):
        result = run_hub_calculation(hass, hub)

    own = result["inverters"][inverter.entry_id]
    assert own["forecast_battery_max_soc"] == 90
    assert own["forecast_charge_limiting"] is True


# --- A PV-only inverter carries no battery, whatever its options say ----------
#
# The inverter form saves *Battery max charge power* at its 5000 W default even
# on an entry with no battery entity. Live (2026-09-03), that phantom took 53 %
# of the charge-limit advice — 4500 W Deye against a 5000 W SolarEdge default —
# so the register sat at 5 A while the inverter curtailed 500 W, and the same
# 5 kW widened the Excess allowance. The reader now hands the fleet None for
# every battery figure of a member without a battery entity.


def _pv_only_inverter(hub, slug):
    """A string inverter with a PV sensor and the form's battery defaults left
    behind in its options — no SOC, no battery power."""
    return MockConfigEntry(
        domain=DOMAIN,
        version=2,
        minor_version=4,
        title=f"PV Only {slug}",
        data={
            CONF_NAME: f"PV Only {slug}",
            CONF_ENTITY_ID: f"pv_only_{slug}",
            ENTRY_TYPE: ENTRY_TYPE_INVERTER,
            CONF_HUB_ENTRY_ID: hub.entry_id,
        },
        options={
            CONF_SOLAR_PRODUCTION_ENTITY_ID: "sensor.pv_only_production",
            CONF_BATTERY_MAX_CHARGE_POWER: 5000,
            CONF_BATTERY_MAX_DISCHARGE_POWER: 5000,
        },
    )


async def test_a_pv_only_inverter_takes_no_share_of_the_battery_advice(hass):
    """Two inverters on one hub: a 5 kW hybrid parked at its 95 % destination
    and a PV-only array whose options still carry the 5000 W battery defaults.

    Meter pinned at the 5 kW export limit with the pack taking 500 W, so the
    engaged feedback asks for 500 + (5000 − 4500) = 1000 W. Split by charge cap
    over a fleet that wrongly counts the array, the hybrid was advised 500 W;
    it must be advised the whole 1000 W. And the Excess allowance is the
    hybrid's 5 kW nameplate alone (advice-only, nothing enforced): export 5000
    + charging 500 against the 4500 W trigger plus 5000 W of battery is
    −4000 W of margin, not the −9000 W a phantom second battery reads.
    """
    from freezegun import freeze_time
    from custom_components.dynamic_ocpp_evse.engine.hub_calculation import (
        run_hub_calculation,
    )

    hub = _destination_hub("pvonly")
    hybrid = _destination_inverter(hub, "pvonly", "number.dst_normal")
    array = _pv_only_inverter(hub, "pvonly")
    for entry in (hybrid, array):
        entry.add_to_hass(hass)
    hass.data[DOMAIN] = {
        "hubs": {hub.entry_id: {"loads": []}},
        "loads": {},
        "load_allocations": {},
        "inverters": {},
    }
    _set_destination_states(hass, soc=95, normal="95")
    # Export pinned at the 5 kW limit: 5000 / 230 A leaving on the one phase.
    hass.states.async_set(
        "sensor.dst_phase_a", str(-5000.0 / 230.0),
        {"device_class": "current", "unit_of_measurement": "A"},
    )
    hass.states.async_set(
        "sensor.pv_only_production", "3000",
        {"device_class": "power", "unit_of_measurement": "W"},
    )

    with freeze_time("2026-08-14 08:00:00+00:00"):
        result = run_hub_calculation(hass, hub)

    own = result["inverters"][hybrid.entry_id]
    assert own["forecast_charge_limiting"] is True
    assert result["forecast_charge_limit_w"] == 1000
    assert own["forecast_charge_limit_w"] == 1000
    assert "forecast_charge_limit_w" not in result["inverters"].get(array.entry_id, {})
    assert result["excess_margin_power"] == pytest.approx(-4000.0, abs=1.0)
