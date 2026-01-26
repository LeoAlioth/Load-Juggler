from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry, SOURCE_INTEGRATION_DISCOVERY
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.service import async_register_admin_service
from homeassistant.helpers.script import Script
from homeassistant.components.button import ButtonEntity
from homeassistant.helpers.entity_registry import async_get as async_get_entity_registry
from homeassistant.helpers.device_registry import async_get as async_get_device_registry
import logging
from .const import *

_LOGGER = logging.getLogger(__name__)

# Define the config schema
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

# Integration version for entity migration
INTEGRATION_VERSION = "2.0.0"


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate old entry to new version."""
    _LOGGER.info("Migrating from version %s.%s to version 2.1", 
                 entry.version, 
                 getattr(entry, 'minor_version', 0))

    if entry.version < 2:
        # Migrate from V1 (single config) to V2 (hub + charger architecture)
        new_data = dict(entry.data)
        
        # Mark this as a hub entry (legacy entries become hubs)
        new_data[ENTRY_TYPE] = ENTRY_TYPE_HUB
        
        # Generate entity IDs for hub-created entities if not present
        entity_id = new_data.get(CONF_ENTITY_ID, "dynamic_ocpp_evse")
        if CONF_CHARGIN_MODE_ENTITY_ID not in new_data:
            new_data[CONF_CHARGIN_MODE_ENTITY_ID] = f"select.{entity_id}_charging_mode"
        if CONF_BATTERY_SOC_TARGET_ENTITY_ID not in new_data:
            new_data[CONF_BATTERY_SOC_TARGET_ENTITY_ID] = f"number.{entity_id}_home_battery_soc_target"
        if CONF_ALLOW_GRID_CHARGING_ENTITY_ID not in new_data:
            new_data[CONF_ALLOW_GRID_CHARGING_ENTITY_ID] = f"switch.{entity_id}_allow_grid_charging"
        if CONF_POWER_BUFFER_ENTITY_ID not in new_data:
            new_data[CONF_POWER_BUFFER_ENTITY_ID] = f"number.{entity_id}_power_buffer"
        
        # Update the config entry with new version
        hass.config_entries.async_update_entry(
            entry,
            data=new_data,
            version=2,
            minor_version=1
        )
        
        _LOGGER.info(
            "Migration to version 2.1 successful. Legacy entry converted to hub. "
            "You will need to add chargers separately after migration."
        )
        
        return True
    
    # Handle minor version updates if version is already 2
    if entry.version == 2 and getattr(entry, 'minor_version', 0) < 1:
        hass.config_entries.async_update_entry(
            entry,
            minor_version=1
        )
        _LOGGER.info("Updated minor version to 1")
        return True
    
    return True


async def async_setup(hass: HomeAssistant, config: dict):
    """Set up the Dynamic OCPP EVSE component."""
    
    async def handle_reset_service(call):
        """Handle the reset service call."""
        entry_id = call.data.get("entry_id")
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry is None:
            return

        evse_minimum_charge_current = entry.data.get(CONF_EVSE_MINIMUM_CHARGE_CURRENT, DEFAULT_MIN_CHARGE_CURRENT)

        sequence = [
            {"service": "ocpp.clear_profile", "data": {}},
            {"delay": {"seconds": 30}},
            {
                "service": "ocpp.set_charge_rate",
                "data": {
                    "custom_profile": {
                        "chargingProfileId": 10,
                        "stackLevel": 2,
                        "chargingProfileKind": "Relative",
                        "chargingProfilePurpose": "TxDefaultProfile",
                        "chargingSchedule": {
                            "chargingRateUnit": "A",
                            "chargingSchedulePeriod": [
                                {"startPeriod": 0, "limit": evse_minimum_charge_current}
                            ]
                        }
                    }
                }
            }
        ]
        script = Script(hass, sequence, "Reset OCPP EVSE")
        await script.async_run()

    hass.services.async_register(DOMAIN, "reset_ocpp_evse", handle_reset_service)

    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Set up Dynamic OCPP EVSE from a config entry."""
    hass.data.setdefault(DOMAIN, {
        "hubs": {},
        "chargers": {},
        "charger_allocations": {},  # Stores current allocation for each charger
    })
    
    entry_type = entry.data.get(ENTRY_TYPE)
    
    # Handle legacy entries (without entry_type) - treat as hub
    if not entry_type:
        _LOGGER.info("Migrating legacy config entry to hub type")
        new_data = dict(entry.data)
        new_data[ENTRY_TYPE] = ENTRY_TYPE_HUB
        hass.config_entries.async_update_entry(entry, data=new_data)
        entry_type = ENTRY_TYPE_HUB
    
    if entry_type == ENTRY_TYPE_HUB:
        await _setup_hub_entry(hass, entry)
    elif entry_type == ENTRY_TYPE_CHARGER:
        await _setup_charger_entry(hass, entry)
    
    return True


async def _setup_hub_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Set up a hub config entry."""
    _LOGGER.info("Setting up hub entry: %s", entry.title)
    
    # Store hub data
    hass.data[DOMAIN]["hubs"][entry.entry_id] = {
        "entry": entry,
        "chargers": [],  # List of charger entry_ids linked to this hub
    }
    
    # Check if entities need migration
    await _migrate_hub_entities_if_needed(hass, entry)
    
    # Forward setup to hub platforms (select, number, switch for hub-level entities)
    await hass.config_entries.async_forward_entry_setups(entry, ["select", "number", "switch"])
    
    # Trigger discovery for unconfigured OCPP chargers
    await _discover_and_notify_chargers(hass, entry.entry_id)
    
    return True


async def _setup_charger_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Set up a charger config entry."""
    _LOGGER.info("Setting up charger entry: %s", entry.title)
    
    hub_entry_id = entry.data.get(CONF_HUB_ENTRY_ID)
    
    # Verify hub exists
    if hub_entry_id not in hass.data[DOMAIN]["hubs"]:
        _LOGGER.error("Hub entry %s not found for charger %s", hub_entry_id, entry.title)
        return False
    
    # Store charger data
    hass.data[DOMAIN]["chargers"][entry.entry_id] = {
        "entry": entry,
        "hub_entry_id": hub_entry_id,
    }
    
    # Link charger to hub
    hass.data[DOMAIN]["hubs"][hub_entry_id]["chargers"].append(entry.entry_id)
    
    # Initialize charger allocation
    hass.data[DOMAIN]["charger_allocations"][entry.entry_id] = 0
    
    # Forward setup to charger platforms (sensor, number, button for charger-specific entities)
    await hass.config_entries.async_forward_entry_setups(entry, ["sensor", "number", "button"])
    
    return True


async def _discover_and_notify_chargers(hass: HomeAssistant, hub_entry_id: str):
    """Discover unconfigured OCPP chargers and create discovery flows."""
    entity_registry = async_get_entity_registry(hass)
    device_registry = async_get_device_registry(hass)
    
    # Get already configured charger entity IDs
    configured_charger_imports = set()
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.data.get(ENTRY_TYPE) == ENTRY_TYPE_CHARGER:
            configured_charger_imports.add(entry.data.get(CONF_EVSE_CURRENT_IMPORT_ENTITY_ID))
    
    # Find unconfigured OCPP chargers
    discovered_chargers = []
    for entity_id, entity in entity_registry.entities.items():
        if entity_id.endswith(OCPP_ENTITY_SUFFIX_CURRENT_IMPORT) and entity_id.startswith("sensor."):
            # Skip already configured chargers
            if entity_id in configured_charger_imports:
                continue
            
            # Extract charger base name
            base_name = entity_id.replace("sensor.", "").replace(OCPP_ENTITY_SUFFIX_CURRENT_IMPORT, "")
            
            # Check if corresponding current_offered entity exists
            current_offered_id = f"sensor.{base_name}{OCPP_ENTITY_SUFFIX_CURRENT_OFFERED}"
            if current_offered_id in entity_registry.entities:
                # Get device info if available
                device_name = base_name.replace("_", " ").title()
                device_id = None
                
                if entity.device_id:
                    device = device_registry.async_get(entity.device_id)
                    if device:
                        device_name = device.name or device_name
                        device_id = device.id
                
                discovered_chargers.append({
                    "id": base_name,
                    "name": device_name,
                    "device_id": device_id,
                    "current_import_entity": entity_id,
                    "current_offered_entity": current_offered_id,
                })
    
    # Create discovery flows for each unconfigured charger
    for charger in discovered_chargers:
        _LOGGER.info("Discovered OCPP charger: %s (%s)", charger["name"], charger["id"])
        
        # Create a discovery flow
        await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_INTEGRATION_DISCOVERY},
            data={
                "charger_id": charger["id"],
                "charger_name": charger["name"],
                "device_id": charger["device_id"],
                "current_import_entity": charger["current_import_entity"],
                "current_offered_entity": charger["current_offered_entity"],
                "hub_entry_id": hub_entry_id,
            },
        )


async def _migrate_hub_entities_if_needed(hass: HomeAssistant, entry: ConfigEntry):
    """Check if entities need to be migrated to the new hub architecture."""
    entity_registry = async_get_entity_registry(hass)
    entity_id = entry.data.get(CONF_ENTITY_ID)
    
    if not entity_id:
        _LOGGER.warning("No entity_id found in hub config entry, skipping entity migration")
        return
    
    # Define expected hub entities with their unique_ids
    expected_entities = {
        f"number.{entity_id}_home_battery_soc_target": f"{entity_id}_home_battery_soc_target",
        f"number.{entity_id}_power_buffer": f"{entity_id}_power_buffer",
        f"select.{entity_id}_charging_mode": f"{entity_id}_charging_mode",
        f"switch.{entity_id}_allow_grid_charging": f"{entity_id}_allow_grid_charging"
    }
    
    # Check and update existing entities to be associated with this config entry
    for entity_entity_id, unique_id in expected_entities.items():
        # Try to find entity by entity_id
        entity_entry = entity_registry.entities.get(entity_entity_id)
        
        if entity_entry:
            # Entity exists, ensure it's associated with this config entry
            if entity_entry.config_entry_id != entry.entry_id:
                _LOGGER.info(f"Migrating existing entity {entity_entity_id} to hub config entry {entry.entry_id}")
                entity_registry.async_update_entity(
                    entity_entity_id,
                    config_entry_id=entry.entry_id
                )
            else:
                _LOGGER.debug(f"Entity {entity_entity_id} already associated with hub config entry")
        else:
            # Entity doesn't exist by entity_id, check if it exists by unique_id
            # This handles cases where entity_id might have been customized
            existing_entity = None
            for reg_entity_id, reg_entity in entity_registry.entities.items():
                if reg_entity.unique_id == unique_id and reg_entity.platform == DOMAIN:
                    existing_entity = reg_entity
                    break
            
            if existing_entity:
                _LOGGER.info(f"Found entity with unique_id {unique_id}, associating with hub config entry")
                entity_registry.async_update_entity(
                    existing_entity.entity_id,
                    config_entry_id=entry.entry_id
                )
            else:
                _LOGGER.info(f"Entity {entity_entity_id} will be created when the platform is set up")
    
    # Update the config entry to ensure it has the required entity IDs
    updated_data = dict(entry.data)
    updated_data[CONF_BATTERY_SOC_TARGET_ENTITY_ID] = f"number.{entity_id}_home_battery_soc_target"
    updated_data[CONF_CHARGIN_MODE_ENTITY_ID] = f"select.{entity_id}_charging_mode"
    updated_data[CONF_ALLOW_GRID_CHARGING_ENTITY_ID] = f"switch.{entity_id}_allow_grid_charging"
    updated_data[CONF_POWER_BUFFER_ENTITY_ID] = f"number.{entity_id}_power_buffer"
    updated_data["integration_version"] = INTEGRATION_VERSION
    
    hass.config_entries.async_update_entry(entry, data=updated_data)
    _LOGGER.info("Updated hub config entry with entity IDs")


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Unload a Dynamic OCPP EVSE config entry."""
    entry_type = entry.data.get(ENTRY_TYPE, ENTRY_TYPE_HUB)
    
    if entry_type == ENTRY_TYPE_HUB:
        # Unload hub platforms
        for domain in ["select", "number", "switch"]:
            await hass.config_entries.async_forward_entry_unload(entry, domain)
        
        # Remove hub from data
        if entry.entry_id in hass.data[DOMAIN]["hubs"]:
            del hass.data[DOMAIN]["hubs"][entry.entry_id]
    
    elif entry_type == ENTRY_TYPE_CHARGER:
        # Unload charger platforms
        for domain in ["sensor", "number", "button"]:
            await hass.config_entries.async_forward_entry_unload(entry, domain)
        
        # Remove charger from hub's list
        hub_entry_id = entry.data.get(CONF_HUB_ENTRY_ID)
        if hub_entry_id in hass.data[DOMAIN]["hubs"]:
            chargers_list = hass.data[DOMAIN]["hubs"][hub_entry_id]["chargers"]
            if entry.entry_id in chargers_list:
                chargers_list.remove(entry.entry_id)
        
        # Remove charger from data
        if entry.entry_id in hass.data[DOMAIN]["chargers"]:
            del hass.data[DOMAIN]["chargers"][entry.entry_id]
        if entry.entry_id in hass.data[DOMAIN]["charger_allocations"]:
            del hass.data[DOMAIN]["charger_allocations"][entry.entry_id]
    
    return True


def get_hub_for_charger(hass: HomeAssistant, charger_entry_id: str) -> ConfigEntry | None:
    """Get the hub config entry for a charger."""
    charger_data = hass.data[DOMAIN]["chargers"].get(charger_entry_id)
    if not charger_data:
        return None
    
    hub_entry_id = charger_data.get("hub_entry_id")
    hub_data = hass.data[DOMAIN]["hubs"].get(hub_entry_id)
    if not hub_data:
        return None
    
    return hub_data.get("entry")


def get_chargers_for_hub(hass: HomeAssistant, hub_entry_id: str) -> list[ConfigEntry]:
    """Get all charger config entries for a hub."""
    hub_data = hass.data[DOMAIN]["hubs"].get(hub_entry_id)
    if not hub_data:
        return []
    
    chargers = []
    for charger_entry_id in hub_data.get("chargers", []):
        charger_data = hass.data[DOMAIN]["chargers"].get(charger_entry_id)
        if charger_data:
            chargers.append(charger_data.get("entry"))
    
    return chargers


def distribute_current_to_chargers(hass: HomeAssistant, hub_entry_id: str, total_available_current: float) -> dict:
    """
    Distribute available current to chargers based on priority.
    
    Algorithm:
    1. Sort chargers by priority (1 = highest)
    2. For each charger (in priority order):
       - If remaining_current >= min_current: allocate min(remaining, max_current)
       - Else: allocate 0 (charger waits)
    3. After initial allocation, distribute any excess evenly among active chargers
    
    Returns dict of {charger_entry_id: allocated_current}
    """
    chargers = get_chargers_for_hub(hass, hub_entry_id)
    if not chargers:
        return {}
    
    # Build charger info list with priority
    charger_info = []
    for entry in chargers:
        min_current = entry.data.get(CONF_EVSE_MINIMUM_CHARGE_CURRENT, DEFAULT_MIN_CHARGE_CURRENT)
        max_current = entry.data.get(CONF_EVSE_MAXIMUM_CHARGE_CURRENT, DEFAULT_MAX_CHARGE_CURRENT)
        priority = entry.data.get(CONF_CHARGER_PRIORITY, DEFAULT_CHARGER_PRIORITY)
        
        charger_info.append({
            "entry_id": entry.entry_id,
            "min_current": min_current,
            "max_current": max_current,
            "priority": priority,
            "allocated": 0,
        })
    
    # Sort by priority (lower number = higher priority)
    charger_info.sort(key=lambda x: x["priority"])
    
    remaining_current = total_available_current
    active_chargers = []
    
    # Phase 1: Initial allocation based on priority
    for charger in charger_info:
        if remaining_current >= charger["min_current"]:
            # Allocate minimum current
            charger["allocated"] = charger["min_current"]
            remaining_current -= charger["min_current"]
            active_chargers.append(charger)
        else:
            # Not enough current for this charger
            charger["allocated"] = 0
    
    # Phase 2: Distribute excess current evenly among active chargers
    if active_chargers and remaining_current > 0:
        while remaining_current > 0.1:  # Small threshold to avoid infinite loop
            distributed_any = False
            share = remaining_current / len(active_chargers)
            
            for charger in active_chargers:
                room = charger["max_current"] - charger["allocated"]
                if room > 0:
                    add = min(share, room)
                    charger["allocated"] += add
                    remaining_current -= add
                    distributed_any = True
            
            if not distributed_any:
                break
    
    # Build result dict and update global allocations
    result = {}
    for charger in charger_info:
        result[charger["entry_id"]] = round(charger["allocated"], 1)
        hass.data[DOMAIN]["charger_allocations"][charger["entry_id"]] = round(charger["allocated"], 1)
    
    _LOGGER.debug("Current distribution - Total: %.1fA, Allocations: %s", total_available_current, result)
    
    return result


def get_charger_allocation(hass: HomeAssistant, charger_entry_id: str) -> float:
    """Get the current allocation for a specific charger."""
    return hass.data[DOMAIN]["charger_allocations"].get(charger_entry_id, 0)
