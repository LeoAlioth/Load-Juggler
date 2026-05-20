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
    LoadJugglerPhaseMaskSensor,
)
from .hub import (
    LoadJugglerHubSensor,
    LoadJugglerHubStatusSensor,
    LoadJugglerHubDataSensor,
    HUB_SENSOR_DEFINITIONS,
)
from .circuit_group import LoadJugglerCircuitGroupSensor
from . import get_hub_for_charger

DynamicOcppEvseChargerSensor = LoadJugglerDeviceSensor
DynamicOcppEvseHubSensor = LoadJugglerHubSensor
DynamicOcppEvseHubDataSensor = LoadJugglerHubDataSensor

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
    entities = [sensor, allocated_sensor, status_sensor]

    # Phase mask sensor — only for 3-phase EVSEs (L1/L2/L3 mapped to 3 distinct
    # site phases). For 1-/2-phase loads the mask is trivial, so it is omitted.
    device_type = config_entry.data.get(CONF_DEVICE_TYPE, DEVICE_TYPE_EVSE)
    l1 = get_entry_value(config_entry, CONF_CHARGER_L1_PHASE, "A")
    l2 = get_entry_value(config_entry, CONF_CHARGER_L2_PHASE, "B")
    l3 = get_entry_value(config_entry, CONF_CHARGER_L3_PHASE, "C")
    if device_type == DEVICE_TYPE_EVSE and len({l1, l2, l3}) == 3:
        entities.append(
            LoadJugglerPhaseMaskSensor(hass, config_entry, hub_entry, name, entity_id)
        )

    async_add_entities(entities)

    await coordinator.async_config_entry_first_refresh()

    # No per-charger options-update listener is registered here. Option changes
    # are handled centrally by _async_options_updated (in __init__.py), which
    # does a clean full reload of the entry — and, for a hub, of its chargers —
    # so site_update_frequency changes are picked up by rebuilding the
    # coordinator from scratch. A second listener that swapped the coordinator
    # in place raced with that reload and leaked the old coordinator's timer.
