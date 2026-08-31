"""Tests for Dynamic OCPP EVSE integration setup, unload, and migration."""

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.dynamic_ocpp_evse.const import (
    DOMAIN,
    ENTRY_TYPE,
    ENTRY_TYPE_HUB,
    ENTRY_TYPE_LOAD,
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
    assert hub_data["loads"] == []


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
    assert mock_charger_entry.entry_id in hass.data[DOMAIN]["loads"]
    charger_data = hass.data[DOMAIN]["loads"][mock_charger_entry.entry_id]
    assert charger_data["hub_entry_id"] == mock_hub_entry.entry_id

    # Charger should be linked to hub
    assert mock_charger_entry.entry_id in hass.data[DOMAIN]["hubs"][mock_hub_entry.entry_id]["loads"]

    # Allocation should be initialized to 0
    assert hass.data[DOMAIN]["load_allocations"][mock_charger_entry.entry_id] == 0


async def test_charger_setup_without_hub(
    hass: HomeAssistant,
    mock_charger_entry: MockConfigEntry,
    mock_setup,
):
    """Test that a charger entry fails gracefully when hub is missing."""
    mock_charger_entry.add_to_hass(hass)

    # Initialize domain data so async_setup_entry doesn't create default structure
    # without a hub present
    hass.data.setdefault(DOMAIN, {"hubs": {}, "loads": {}, "load_allocations": {}})

    await hass.config_entries.async_setup(mock_charger_entry.entry_id)
    await hass.async_block_till_done()

    # Charger should NOT be in the chargers dict since hub doesn't exist
    assert mock_charger_entry.entry_id not in hass.data[DOMAIN]["loads"]


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

    assert mock_charger_entry.entry_id in hass.data[DOMAIN]["hubs"][mock_hub_entry.entry_id]["loads"]

    await hass.config_entries.async_unload(mock_charger_entry.entry_id)
    await hass.async_block_till_done()

    # Charger should be removed from hub's list and from chargers dict
    assert mock_charger_entry.entry_id not in hass.data[DOMAIN]["hubs"][mock_hub_entry.entry_id]["loads"]
    assert mock_charger_entry.entry_id not in hass.data[DOMAIN]["loads"]
    assert mock_charger_entry.entry_id not in hass.data[DOMAIN]["load_allocations"]


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
    )
    legacy_entry.add_to_hass(hass)

    await hass.config_entries.async_setup(legacy_entry.entry_id)
    await hass.async_block_till_done()

    # After migration, entry should be v2
    assert legacy_entry.version == 2

    # Should be marked as hub
    assert legacy_entry.data[ENTRY_TYPE] == ENTRY_TYPE_HUB

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
    )
    v2_entry.add_to_hass(hass)

    await hass.config_entries.async_setup(v2_entry.entry_id)
    await hass.async_block_till_done()

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
    )
    no_type_entry.add_to_hass(hass)

    await hass.config_entries.async_setup(no_type_entry.entry_id)
    await hass.async_block_till_done()

    # Should be treated as hub and entry_type should be added
    assert no_type_entry.data[ENTRY_TYPE] == ENTRY_TYPE_HUB
    assert no_type_entry.entry_id in hass.data[DOMAIN]["hubs"]


async def test_fleet_survives_a_hub_reload(
    hass: HomeAssistant,
    mock_hub_entry: MockConfigEntry,
    mock_setup,
):
    """Reloading a hub must not drop its inverters (or chargers) from the site.

    Regression: the hub's runtime child lists are rebuilt empty on every hub
    setup, and children that are already loaded never re-register themselves.
    Adding an inverter schedules a hub reload, so the fleet could come back
    missing inverters — silently taking their capacity out of the site limit —
    until the next Home Assistant restart.
    """
    from custom_components.dynamic_ocpp_evse import get_inverters_for_hub
    from custom_components.dynamic_ocpp_evse.const import (
        ENTRY_TYPE_INVERTER,
        DEVICE_TYPE_INVERTER,
        CONF_DEVICE_TYPE,
        CONF_NAME,
        CONF_INVERTER_MAX_POWER,
    )

    mock_hub_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_hub_entry.entry_id)
    await hass.async_block_till_done()

    inverter = MockConfigEntry(
        domain=DOMAIN,
        # Current version: a v1 entry would be migrated into a hub on setup
        # ("legacy entries become hubs"), which is not what this tests.
        version=2,
        minor_version=4,
        title="SolarEdge",
        data={
            CONF_NAME: "SolarEdge",
            CONF_ENTITY_ID: "lj_solaredge",
            ENTRY_TYPE: ENTRY_TYPE_INVERTER,
            CONF_DEVICE_TYPE: DEVICE_TYPE_INVERTER,
            CONF_HUB_ENTRY_ID: mock_hub_entry.entry_id,
        },
        options={CONF_INVERTER_MAX_POWER: 10000},
    )
    inverter.add_to_hass(hass)
    await hass.config_entries.async_setup(inverter.entry_id)
    await hass.async_block_till_done()

    # The hub fixture's legacy battery fields auto-import into an inverter
    # entry of their own, so compare sets rather than assuming a lone member.
    before = {e.entry_id for e in get_inverters_for_hub(hass, mock_hub_entry.entry_id)}
    assert inverter.entry_id in before

    # The inverter stays loaded; only the hub reloads (what adding a second
    # inverter does).
    await hass.config_entries.async_reload(mock_hub_entry.entry_id)
    await hass.async_block_till_done()

    after = {e.entry_id for e in get_inverters_for_hub(hass, mock_hub_entry.entry_id)}
    assert after == before, "a hub reload must not drop inverters from the fleet"


async def test_chargers_are_readopted_after_a_hub_reload(
    hass: HomeAssistant,
    mock_hub_entry: MockConfigEntry,
    mock_charger_entry: MockConfigEntry,
    mock_setup,
):
    """Same regression for chargers, which keep a runtime list because their
    allocation state lives beside it — the hub re-adopts them on setup."""
    from custom_components.dynamic_ocpp_evse import get_loads_for_hub

    mock_hub_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_hub_entry.entry_id)
    await hass.async_block_till_done()
    mock_charger_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_charger_entry.entry_id)
    await hass.async_block_till_done()

    assert [e.entry_id for e in get_loads_for_hub(hass, mock_hub_entry.entry_id)] == [
        mock_charger_entry.entry_id
    ]

    await hass.config_entries.async_reload(mock_hub_entry.entry_id)
    await hass.async_block_till_done()

    assert [e.entry_id for e in get_loads_for_hub(hass, mock_hub_entry.entry_id)] == [
        mock_charger_entry.entry_id
    ]
