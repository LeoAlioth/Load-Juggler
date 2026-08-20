"""Tests for the site update cycle.

These tests create actual sensor entity instances with mocked HA states and
drive one hub site cycle (`_run_site_cycle`, the hub coordinator's own update
function) to verify the data flow from HA entities through the calculation
engine to the sensor state and the device commands.
"""

from unittest.mock import patch, AsyncMock, MagicMock
from datetime import datetime, timedelta, timezone

import pytest
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import STATE_UNAVAILABLE
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
    DEFAULT_MIN_CHARGE_CURRENT,
    DEFAULT_MAX_CHARGE_CURRENT,
    DEFAULT_PHASE_VOLTAGE,
    DEFAULT_MAIN_BREAKER_RATING,
    DEFAULT_BATTERY_MAX_POWER,
    DEFAULT_BATTERY_SOC_HYSTERESIS,
    DEFAULT_CHARGE_PAUSE_DURATION,
    CONF_GRID_EXPORT_LIMIT,
    CONF_SOLAR_FORECAST_ENTITY_IDS,
    CONF_BASE_CONSUMPTION,
    CONF_BATTERY_CAPACITY_KWH,
    CONF_FORECAST_SOC_FLOOR,
    ENTRY_TYPE_INVERTER,
    CONF_CHARGE_LIMIT_ENTITY_ID,
    CONF_CHARGE_LIMIT_UNIT,
    CONF_CHARGE_CONTROL_INTERVAL,
    CONF_BATTERY_NOMINAL_VOLTAGE,
    CHARGE_LIMIT_UNIT_AMPS,
    CHARGE_LIMIT_UNIT_WATTS,
    INVERTER_RT_APPLIED,
    INVERTER_RT_CONTROL_ENABLED,
    CONF_SOC_LIMIT_ENTITY_IDS,
    CONF_SOC_LIMIT_NORMAL_ENTITY_ID,
    INVERTER_RT_SOC_CONTROL_ENABLED,
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
        assert sensor.device_class == defn["device_class"]
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
            ENTRY_TYPE: ENTRY_TYPE_CHARGER,
            CONF_CHARGER_ID: f"charger_{suffix}",
            CONF_OCPP_DEVICE_ID: f"ocpp_device_{suffix}",
            CONF_EVSE_CURRENT_IMPORT_ENTITY_ID: "sensor.test_charger_current_import",
            CONF_EVSE_CURRENT_OFFERED_ENTITY_ID: "sensor.test_charger_current_offered",
            CONF_HUB_ENTRY_ID: hub_entry.entry_id,
        },
        options={
            CONF_CHARGER_PRIORITY: 2,
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
            ENTRY_TYPE: ENTRY_TYPE_CHARGER,
            CONF_CHARGER_ID: "test_charger",
            CONF_OCPP_DEVICE_ID: "test_charger",
            CONF_EVSE_CURRENT_IMPORT_ENTITY_ID: "sensor.test_charger_current_import",
            CONF_EVSE_POWER_OFFERED_ENTITY_ID: "sensor.test_charger_power_offered",
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


async def test_the_cycle_releases_the_limit_exactly_once(
    hass, hub_entry, inverter_entry
):
    """Advice stopping restores the normal value, and only on the first cycle
    that sees it — a release must not be rewritten every 2 s all night."""
    sensor = await _add_charge_control(hass, inverter_entry)

    with patch(
        "homeassistant.core.ServiceRegistry.async_call", new_callable=AsyncMock
    ), _advice_cycle(inverter_entry, 2560.0):
        await _run_site_cycle(hass, hub_entry)

    # The inverter took the 50 A we wrote; now the forecast releases.
    hass.states.async_set(CHARGE_TARGET, "50", {"max": 100})
    with _accepting_register(hass) as mock_call, _advice_cycle(inverter_entry, None):
        for _ in range(4):
            await _run_site_cycle(hass, hub_entry)

    writes = _register_writes(mock_call)
    assert len(writes) == 1, writes
    assert writes[0][0][2]["value"] == 100.0
    assert sensor.extra_state_attributes["control_state"] == CONTROL_STATE_IDLE
    # And the restore is visible in the value the sensor graphs: back up at the
    # normal limit, read from the register the later cycles saw.
    assert sensor.native_value == 100.0
    inverter_rt = hass.data[DOMAIN]["inverters"][inverter_entry.entry_id]
    assert inverter_rt[INVERTER_RT_APPLIED] is None


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
