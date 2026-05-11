import logging
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from datetime import timedelta
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from .const import *
from .helpers import get_entry_value
from .load import LoadJugglerDeviceSensor
from .load_sensors import (
    LoadJugglerAllocatedCurrentSensor,
    LoadJugglerDeviceStatusSensor,
)
from .hub import (
    LoadJugglerHubSensor,
    LoadJugglerHubStatusSensor,
    LoadJugglerHubDataSensor,
    HUB_SENSOR_DEFINITIONS,
)
from .circuit_group import LoadJugglerCircuitGroupSensor
from . import get_hub_for_charger

_LOGGER = logging.getLogger(__name__)
SCAN_INTERVAL = timedelta(seconds=10)


async def async_setup_entry(
    hass: HomeAssistant, config_entry: ConfigEntry, async_add_entities
):
    """Set up the Load Juggler Sensor from a config entry."""
    entry_type = config_entry.data.get(ENTRY_TYPE)

    if entry_type == ENTRY_TYPE_HUB:
        name = config_entry.data.get(CONF_NAME, "Site Load Management")
        entity_id = config_entry.data.get(CONF_ENTITY_ID, "site_load_management")

        has_battery = bool(
            get_entry_value(config_entry, CONF_BATTERY_SOC_ENTITY_ID, None)
        )
        has_phase_b = bool(
            get_entry_value(config_entry, CONF_PHASE_B_CURRENT_ENTITY_ID, None)
        )
        has_phase_c = bool(
            get_entry_value(config_entry, CONF_PHASE_C_CURRENT_ENTITY_ID, None)
        )

        entities = [
            LoadJugglerHubSensor(hass, config_entry, name, entity_id),
            LoadJugglerHubStatusSensor(hass, config_entry, name, entity_id),
        ]
        for defn in HUB_SENSOR_DEFINITIONS:
            if defn.get("requires_battery") and not has_battery:
                continue
            if defn.get("requires_phase") == "B" and not has_phase_b:
                continue
            if defn.get("requires_phase") == "C" and not has_phase_c:
                continue
            entities.append(
                LoadJugglerHubDataSensor(hass, config_entry, name, entity_id, defn)
            )

        async_add_entities(entities)
        phases = "A" + ("B" if has_phase_b else "") + ("C" if has_phase_c else "")
        _LOGGER.info(
            f"Setting up hub sensors for {name} (battery={'yes' if has_battery else 'no'}, phases={phases})"
        )
        return

    if entry_type == ENTRY_TYPE_GROUP:
        name = config_entry.data.get(CONF_NAME, "Circuit Group")
        entity_id = config_entry.data.get(CONF_ENTITY_ID, "circuit_group")
        hub_entry_id = config_entry.data.get(CONF_HUB_ENTRY_ID)
        sensor = LoadJugglerCircuitGroupSensor(
            hass, config_entry, name, entity_id, hub_entry_id
        )
        async_add_entities([sensor])
        _LOGGER.info("Setting up circuit group sensor for %s", name)
        return

    if entry_type != ENTRY_TYPE_CHARGER:
        _LOGGER.debug(
            "Skipping sensor setup for unknown entry type: %s", config_entry.title
        )
        return

    name = config_entry.data[CONF_NAME]
    entity_id = config_entry.data[CONF_ENTITY_ID]
    charger_entry_id = config_entry.entry_id

    hub_entry = get_hub_for_charger(hass, charger_entry_id)
    if not hub_entry:
        _LOGGER.error("No hub found for charger: %s", name)
        return

    site_update_frequency = get_entry_value(
        hub_entry, CONF_SITE_UPDATE_FREQUENCY, DEFAULT_SITE_UPDATE_FREQUENCY
    )
    _LOGGER.info(
        f"Initial site update frequency for {name}: {site_update_frequency}s (charger command rate: {get_entry_value(config_entry, CONF_UPDATE_FREQUENCY, DEFAULT_UPDATE_FREQUENCY)}s)"
    )

    sensor = LoadJugglerDeviceSensor(
        hass, config_entry, hub_entry, name, entity_id, None
    )

    async def async_update_data():
        """Fetch data for the coordinator using the persistent sensor instance."""
        await sensor.async_update()
        return {
            CONF_TOTAL_ALLOCATED_CURRENT: sensor._state,
            CONF_PHASES: sensor._phases,
            "calc_used": sensor._calc_used,
            "allocated_current": sensor._allocated_current,
        }

    coordinator = DataUpdateCoordinator(
        hass,
        _LOGGER,
        name=f"Load Juggler Coordinator - {name}",
        update_method=async_update_data,
        update_interval=timedelta(seconds=site_update_frequency),
    )
    sensor.coordinator = coordinator
    allocated_sensor = LoadJugglerAllocatedCurrentSensor(
        hass, config_entry, hub_entry, name, entity_id
    )
    status_sensor = LoadJugglerDeviceStatusSensor(
        hass, config_entry, hub_entry, name, entity_id
    )
    async_add_entities([sensor, allocated_sensor, status_sensor])

    await coordinator.async_config_entry_first_refresh()

    async def async_update_listener(hass: HomeAssistant, entry: ConfigEntry):
        """Handle options update."""
        nonlocal site_update_frequency
        _LOGGER.debug("async_update_listener triggered for %s", name)
        current_hub = get_hub_for_charger(hass, entry.entry_id)
        new_site_freq = (
            get_entry_value(
                current_hub, CONF_SITE_UPDATE_FREQUENCY, DEFAULT_SITE_UPDATE_FREQUENCY
            )
            if current_hub
            else site_update_frequency
        )
        if new_site_freq != site_update_frequency:
            _LOGGER.info(
                f"Updating site_update_frequency to {new_site_freq}s for {name}"
            )
            nonlocal coordinator
            coordinator = DataUpdateCoordinator(
                hass,
                _LOGGER,
                name=f"Load Juggler Coordinator - {name}",
                update_method=async_update_data,
                update_interval=timedelta(seconds=new_site_freq),
            )
            site_update_frequency = new_site_freq
            await coordinator.async_config_entry_first_refresh()
            sensor.coordinator = coordinator

    _LOGGER.debug("Registering async_on_update listener for %s", name)
    config_entry.async_on_unload(
        config_entry.add_update_listener(async_update_listener)
    )
