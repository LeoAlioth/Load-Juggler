"""Tests for Dynamic OCPP EVSE integration setup, unload, and migration."""

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.dynamic_ocpp_evse.const import (
    DOMAIN,
    ENTRY_TYPE,
    ENTRY_TYPE_HUB,
    ENTRY_TYPE_CHARGER,
    CONF_ENTITY_ID,
    CONF_HUB_ENTRY_ID,
    CONF_EVSE_MINIMUM_CHARGE_CURRENT,
    CONF_EVSE_MAXIMUM_CHARGE_CURRENT,
    CONF_UPDATE_FREQUENCY,
    CONF_OCPP_PROFILE_TIMEOUT,
    CONF_CHARGE_PAUSE_DURATION,
    CONF_STACK_LEVEL,
    CONF_CHARGE_RATE_UNIT,
    CONF_PROFILE_VALIDITY_MODE,
    CONF_BATTERY_SOC_ENTITY_ID,
    CONF_BATTERY_POWER_ENTITY_ID,
    CONF_BATTERY_MAX_CHARGE_POWER,
    CONF_BATTERY_MAX_DISCHARGE_POWER,
    CONF_BATTERY_SOC_HYSTERESIS,
    CONF_CHARGING_MODE_ENTITY_ID,
    DEFAULT_MIN_CHARGE_CURRENT,
    DEFAULT_MAX_CHARGE_CURRENT,
    DEFAULT_UPDATE_FREQUENCY,
    DEFAULT_OCPP_PROFILE_TIMEOUT,
    DEFAULT_CHARGE_PAUSE_DURATION,
    DEFAULT_STACK_LEVEL,
    DEFAULT_CHARGE_RATE_UNIT,
    DEFAULT_PROFILE_VALIDITY_MODE,
    DEFAULT_BATTERY_MAX_POWER,
    DEFAULT_BATTERY_SOC_HYSTERESIS,
)


async def test_hub_setup(
    hass: HomeAssistant,
    mock_hub_entry: MockConfigEntry,
    mock_setup,
):
    """Test that a hub entry sets up correctly."""
    mock_hub_entry.add_to_hass(hass)

    await hass.config_entries.async_setup(mock_hub_entry.entry_id)
    await hass.async_block_till_done()

    assert DOMAIN in hass.data
    assert mock_hub_entry.entry_id in hass.data[DOMAIN]["hubs"]
    hub_data = hass.data[DOMAIN]["hubs"][mock_hub_entry.entry_id]
    assert hub_data["entry"] is mock_hub_entry
    assert hub_data["chargers"] == []


async def test_charger_setup(
    hass: HomeAssistant,
    mock_hub_entry: MockConfigEntry,
    mock_charger_entry: MockConfigEntry,
    mock_setup,
):
    """Test that a charger entry sets up and links to its hub."""
    mock_hub_entry.add_to_hass(hass)

    # Set up hub first so charger can link to it
    await hass.config_entries.async_setup(mock_hub_entry.entry_id)
    await hass.async_block_till_done()

    # Add and set up charger
    mock_charger_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_charger_entry.entry_id)
    await hass.async_block_till_done()

    # Charger should be registered
    assert mock_charger_entry.entry_id in hass.data[DOMAIN]["chargers"]
    charger_data = hass.data[DOMAIN]["chargers"][mock_charger_entry.entry_id]
    assert charger_data["hub_entry_id"] == mock_hub_entry.entry_id

    # Charger should be linked to hub
    assert mock_charger_entry.entry_id in hass.data[DOMAIN]["hubs"][mock_hub_entry.entry_id]["chargers"]

    # Allocation should be initialized to 0
    assert hass.data[DOMAIN]["charger_allocations"][mock_charger_entry.entry_id] == 0


async def test_charger_setup_without_hub(
    hass: HomeAssistant,
    mock_charger_entry: MockConfigEntry,
    mock_setup,
):
    """Test that a charger entry fails gracefully when hub is missing."""
    mock_charger_entry.add_to_hass(hass)

    # Initialize domain data so async_setup_entry doesn't create default structure
    # without a hub present
    hass.data.setdefault(DOMAIN, {"hubs": {}, "chargers": {}, "charger_allocations": {}})

    await hass.config_entries.async_setup(mock_charger_entry.entry_id)
    await hass.async_block_till_done()

    # Charger should NOT be in the chargers dict since hub doesn't exist
    assert mock_charger_entry.entry_id not in hass.data[DOMAIN]["chargers"]


async def test_hub_unload(
    hass: HomeAssistant,
    mock_hub_entry: MockConfigEntry,
    mock_setup,
):
    """Test that unloading a hub cleans up hass.data."""
    mock_hub_entry.add_to_hass(hass)

    await hass.config_entries.async_setup(mock_hub_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_hub_entry.entry_id in hass.data[DOMAIN]["hubs"]

    await hass.config_entries.async_unload(mock_hub_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_hub_entry.entry_id not in hass.data[DOMAIN]["hubs"]


async def test_charger_unload(
    hass: HomeAssistant,
    mock_hub_entry: MockConfigEntry,
    mock_charger_entry: MockConfigEntry,
    mock_setup,
):
    """Test that unloading a charger removes it from the hub's charger list."""
    mock_hub_entry.add_to_hass(hass)

    await hass.config_entries.async_setup(mock_hub_entry.entry_id)
    await hass.async_block_till_done()

    mock_charger_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_charger_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_charger_entry.entry_id in hass.data[DOMAIN]["hubs"][mock_hub_entry.entry_id]["chargers"]

    await hass.config_entries.async_unload(mock_charger_entry.entry_id)
    await hass.async_block_till_done()

    # Charger should be removed from hub's list and from chargers dict
    assert mock_charger_entry.entry_id not in hass.data[DOMAIN]["hubs"][mock_hub_entry.entry_id]["chargers"]
    assert mock_charger_entry.entry_id not in hass.data[DOMAIN]["chargers"]
    assert mock_charger_entry.entry_id not in hass.data[DOMAIN]["charger_allocations"]


async def test_migration_v1_to_v2(
    hass: HomeAssistant,
    mock_setup,
):
    """Test migration from v1 to v2 hub architecture."""
    legacy_entry = MockConfigEntry(
        domain=DOMAIN,
        version=1,
        title="Legacy EVSE",
        data={
            CONF_ENTITY_ID: "legacy_evse",
            CONF_EVSE_MINIMUM_CHARGE_CURRENT: 8,
            CONF_EVSE_MAXIMUM_CHARGE_CURRENT: 32,
        },
        options={},
    )
    legacy_entry.add_to_hass(hass)

    await hass.config_entries.async_setup(legacy_entry.entry_id)
    await hass.async_block_till_done()

    # After migration, entry should be v2.2
    assert legacy_entry.version == 2
    assert legacy_entry.minor_version == 2

    # Should be marked as hub
    assert legacy_entry.data[ENTRY_TYPE] == ENTRY_TYPE_HUB

    # Generated entity IDs should be present
    assert legacy_entry.data[CONF_CHARGING_MODE_ENTITY_ID] == "select.legacy_evse_charging_mode"

    # Mutable settings should be seeded into options
    assert legacy_entry.options[CONF_EVSE_MINIMUM_CHARGE_CURRENT] == 8
    assert legacy_entry.options[CONF_EVSE_MAXIMUM_CHARGE_CURRENT] == 32
    assert legacy_entry.options[CONF_BATTERY_SOC_HYSTERESIS] == DEFAULT_BATTERY_SOC_HYSTERESIS


async def test_migration_v2_minor_update(
    hass: HomeAssistant,
    mock_setup,
):
    """Test migration from v2.0 to v2.2 (minor version update)."""
    v2_entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        minor_version=0,
        title="V2 Hub",
        data={
            CONF_ENTITY_ID: "test_hub",
            ENTRY_TYPE: ENTRY_TYPE_HUB,
            CONF_EVSE_MINIMUM_CHARGE_CURRENT: 6,
        },
        options={},
    )
    v2_entry.add_to_hass(hass)

    await hass.config_entries.async_setup(v2_entry.entry_id)
    await hass.async_block_till_done()

    assert v2_entry.minor_version == 2
    # Options should be seeded from data with defaults
    assert v2_entry.options[CONF_EVSE_MINIMUM_CHARGE_CURRENT] == 6
    assert v2_entry.options[CONF_CHARGE_RATE_UNIT] == DEFAULT_CHARGE_RATE_UNIT


async def test_legacy_entry_without_type(
    hass: HomeAssistant,
    mock_setup,
):
    """Test that entries without entry_type are treated as hubs."""
    no_type_entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        minor_version=2,
        title="No Type Entry",
        data={
            CONF_ENTITY_ID: "no_type",
            # Deliberately missing ENTRY_TYPE
        },
        options={},
    )
    no_type_entry.add_to_hass(hass)

    await hass.config_entries.async_setup(no_type_entry.entry_id)
    await hass.async_block_till_done()

    # Should be treated as hub and entry_type should be added
    assert no_type_entry.data[ENTRY_TYPE] == ENTRY_TYPE_HUB
    assert no_type_entry.entry_id in hass.data[DOMAIN]["hubs"]
