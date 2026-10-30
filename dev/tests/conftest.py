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
    ENTRY_TYPE_LOAD,
    ENTRY_TYPE_INVERTER,
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
    CONF_GRID_EXPORT_LIMIT,
    CONF_INVERTER_INPUT_IS_GRID,
    CONF_INVERTER_INPUT_PHASE_A_ENTITY_ID,
    CONF_INVERTER_INPUT_PHASE_B_ENTITY_ID,
    CONF_INVERTER_INPUT_PHASE_C_ENTITY_ID,
    CONF_BATTERY_SOC_ENTITY_ID,
    CONF_BATTERY_POWER_ENTITY_ID,
    CONF_BATTERY_MAX_CHARGE_POWER,
    CONF_BATTERY_MAX_DISCHARGE_POWER,
    CONF_BATTERY_SOC_HYSTERESIS,
    CONF_LOAD_PRIORITY,
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
)
from custom_components.dynamic_ocpp_evse.helpers import (
    HUB_INVERTER_IMPORT_FIELDS,
    INVERTER_FEATURE_KEYS,
)


# Every setting an inverter entry owns: the keys an old hub's fields are
# imported under, plus the per-feature keys added since. The engine reads
# none of them off the hub, so a test's site carries them on an inverter.
INVERTER_KEYS = frozenset(HUB_INVERTER_IMPORT_FIELDS).union(
    *INVERTER_FEATURE_KEYS.values()
) | {
    CONF_INVERTER_INPUT_IS_GRID,
    CONF_INVERTER_INPUT_PHASE_A_ENTITY_ID,
    CONF_INVERTER_INPUT_PHASE_B_ENTITY_ID,
    CONF_INVERTER_INPUT_PHASE_C_ENTITY_ID,
}

# How the closed-loop tests name an inverter's input setting. Two arrangements
# stand for what the old Series / Parallel wiring pair did to the household:
# INPUT_GRID - the grid flows through the inverter, straight from the grid
# meter (the tick); INPUT_NONE - no input. They are labels for parametrize ids
# and slugs; ``input_options`` turns one into the inverter's stored keys.
INPUT_GRID = "gridtick"
INPUT_NONE = "noinput"


def input_options(arrangement) -> dict:
    """The inverter-entry options for an ``INPUT_*`` arrangement."""
    return {CONF_INVERTER_INPUT_IS_GRID: True} if arrangement == INPUT_GRID else {}


def add_inverter(hass, hub_entry, options, slug="inverter") -> MockConfigEntry:
    """Register an inverter entry holding ``options`` under ``hub_entry``."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        minor_version=2,
        title=slug,
        data={
            CONF_NAME: slug,
            CONF_ENTITY_ID: slug,
            ENTRY_TYPE: ENTRY_TYPE_INVERTER,
            CONF_HUB_ENTRY_ID: hub_entry.entry_id,
        },
        options=options,
    )
    entry.add_to_hass(hass)
    return entry


def with_inverter(hass, hub_entry, slug="inverter") -> MockConfigEntry:
    """A hub written as one site, split the way the import splits an old hub:
    its inverter settings onto an inverter entry registered with hass, the
    rest onto the returned hub (same entry_id; ``hub_entry`` must not be in
    hass yet)."""
    options = dict(hub_entry.options)
    inverter_options = {k: options.pop(k) for k in INVERTER_KEYS if k in options}
    if inverter_options:
        add_inverter(hass, hub_entry, inverter_options, slug)
    return MockConfigEntry(
        entry_id=hub_entry.entry_id,
        domain=hub_entry.domain,
        version=hub_entry.version,
        minor_version=hub_entry.minor_version,
        title=hub_entry.title,
        data=dict(hub_entry.data),
        options=options,
    )


def forecast_device(hass, entity_id: str) -> str:
    """Register ``entity_id`` as the watts-bearing sensor of its own
    Open-Meteo forecast device (once per entity) and return the device id -
    what an inverter's forecast setting names. The test sets the state."""
    from homeassistant.helpers import device_registry as dr, entity_registry as er

    entities = er.async_get(hass)
    known = entities.async_get(entity_id)
    if known is not None:
        return known.device_id
    slug = entity_id.split(".", 1)[1]
    source = MockConfigEntry(domain="open_meteo_solar_forecast", title=slug)
    source.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=source.entry_id,
        identifiers={("open_meteo_solar_forecast", slug)},
        name=slug,
    )
    entities.async_get_or_create(
        "sensor",
        "open_meteo_solar_forecast",
        slug,
        device_id=device.id,
        config_entry=source,
        suggested_object_id=slug,
    )
    return device.id


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
            CONF_BATTERY_SOC_HYSTERESIS: 3,
        },
    )


@pytest.fixture
def mock_inverter_entry(mock_hub_entry: MockConfigEntry) -> MockConfigEntry:
    """The hub's battery inverter (not yet added to hass)."""
    return MockConfigEntry(
        domain=DOMAIN,
        version=2,
        minor_version=2,
        title="Inverter",
        data={
            CONF_NAME: "Inverter",
            CONF_ENTITY_ID: "inverter",
            ENTRY_TYPE: ENTRY_TYPE_INVERTER,
            CONF_HUB_ENTRY_ID: mock_hub_entry.entry_id,
        },
        options={
            CONF_BATTERY_SOC_ENTITY_ID: "sensor.battery_soc",
            CONF_BATTERY_POWER_ENTITY_ID: "sensor.battery_power",
            CONF_BATTERY_MAX_CHARGE_POWER: 5000,
            CONF_BATTERY_MAX_DISCHARGE_POWER: 5000,
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
            ENTRY_TYPE: ENTRY_TYPE_LOAD,
            CONF_CHARGER_ID: "wallbox_1",
            CONF_OCPP_DEVICE_ID: "device_wallbox_1",
            CONF_EVSE_CURRENT_IMPORT_ENTITY_ID: "sensor.wallbox_1_current_import",
            CONF_EVSE_CURRENT_OFFERED_ENTITY_ID: "sensor.wallbox_1_current_offered",
            CONF_HUB_ENTRY_ID: mock_hub_entry.entry_id,
        },
        options={
            CONF_LOAD_PRIORITY: 1,
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
