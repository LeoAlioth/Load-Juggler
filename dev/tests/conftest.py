"""Shared fixtures for Dynamic OCPP EVSE integration tests."""

from unittest.mock import patch, AsyncMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntries
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
    CONF_BATTERY_SOC_TARGET_ENTITY_ID,
    CONF_ALLOW_GRID_CHARGING_ENTITY_ID,
    CONF_POWER_BUFFER_ENTITY_ID,
)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable custom integrations for all tests."""
    yield


@pytest.fixture
def mock_hub_entry() -> MockConfigEntry:
    """Create a mock hub config entry."""
    return MockConfigEntry(
        domain=DOMAIN,
        version=2,
        minor_version=2,
        title="Dynamic OCPP EVSE",
        data={
            CONF_NAME: "Dynamic OCPP EVSE",
            CONF_ENTITY_ID: "dynamic_ocpp_evse",
            ENTRY_TYPE: ENTRY_TYPE_HUB,
            CONF_BATTERY_SOC_TARGET_ENTITY_ID: "number.dynamic_ocpp_evse_home_battery_soc_target",
            CONF_ALLOW_GRID_CHARGING_ENTITY_ID: "switch.dynamic_ocpp_evse_allow_grid_charging",
            CONF_POWER_BUFFER_ENTITY_ID: "number.dynamic_ocpp_evse_power_buffer",
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
def mock_charger_entry(mock_hub_entry: MockConfigEntry) -> MockConfigEntry:
    """Create a mock charger config entry linked to the hub."""
    return MockConfigEntry(
        domain=DOMAIN,
        version=2,
        minor_version=2,
        title="Wallbox Charger",
        data={
            CONF_ENTITY_ID: "wallbox_1",
            CONF_NAME: "Wallbox",
            ENTRY_TYPE: ENTRY_TYPE_CHARGER,
            CONF_CHARGER_ID: "wallbox_1",
            CONF_OCPP_DEVICE_ID: "device_wallbox_1",
            CONF_EVSE_CURRENT_IMPORT_ENTITY_ID: "sensor.wallbox_1_current_import",
            CONF_EVSE_CURRENT_OFFERED_ENTITY_ID: "sensor.wallbox_1_current_offered",
            CONF_HUB_ENTRY_ID: mock_hub_entry.entry_id,
        },
        options={
            CONF_CHARGER_PRIORITY: 1,
            CONF_EVSE_MINIMUM_CHARGE_CURRENT: 6,
            CONF_EVSE_MAXIMUM_CHARGE_CURRENT: 16,
            CONF_CHARGER_L1_PHASE: "A",
            CONF_CHARGER_L2_PHASE: "B",
            CONF_CHARGER_L3_PHASE: "C",
            CONF_CHARGE_RATE_UNIT: "A",
            CONF_PROFILE_VALIDITY_MODE: "relative",
            CONF_UPDATE_FREQUENCY: 15,
            CONF_OCPP_PROFILE_TIMEOUT: 120,
            CONF_CHARGE_PAUSE_DURATION: 3,
            CONF_STACK_LEVEL: 3,
        },
    )


@pytest.fixture
def mock_setup(hass: HomeAssistant):
    """Patch platform forwarding and discovery to isolate __init__.py tests."""
    with (
        patch.object(
            ConfigEntries,
            "async_forward_entry_setups",
            new_callable=AsyncMock,
        ) as mock_forward,
        patch.object(
            ConfigEntries,
            "async_forward_entry_unload",
            new_callable=AsyncMock,
            return_value=True,
        ) as mock_unload,
        patch(
            "custom_components.dynamic_ocpp_evse._discover_and_notify_chargers",
            new_callable=AsyncMock,
        ) as mock_discover,
        patch(
            "custom_components.dynamic_ocpp_evse._migrate_hub_entities_if_needed",
            new_callable=AsyncMock,
        ) as mock_migrate,
    ):
        yield {
            "forward": mock_forward,
            "unload": mock_unload,
            "discover": mock_discover,
            "migrate": mock_migrate,
        }
