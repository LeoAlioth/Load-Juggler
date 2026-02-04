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

        # Get the OCPP device ID
        ocpp_device_id = entry.data.get(CONF_OCPP_DEVICE_ID)
        if not ocpp_device_id:
            _LOGGER.error(f"No OCPP device ID configured for entry {entry.title} - cannot reset")
            return

        evse_minimum_charge_current = entry.data.get(CONF_EVSE_MINIMUM_CHARGE_CURRENT, DEFAULT_MIN_CHARGE_CURRENT)
        
        # Get charge rate unit from charger config
        charge_rate_unit = entry.data.get(CONF_CHARGE_RATE_UNIT, DEFAULT_CHARGE_RATE_UNIT)
        
        # If set to auto, detect from sensor
        if charge_rate_unit == CHARGE_RATE_UNIT_AUTO:
            current_offered_entity = entry.data.get(CONF_EVSE_CURRENT_OFFERED_ENTITY_ID)
            if current_offered_entity:
                sensor_state = hass.states.get(current_offered_entity)
                if sensor_state:
                    unit = sensor_state.attributes.get("unit_of_measurement")
                    charge_rate_unit = CHARGE_RATE_UNIT_WATTS if unit == "W" else CHARGE_RATE_UNIT_AMPS
                else:
                    charge_rate_unit = CHARGE_RATE_UNIT_AMPS
            else:
                charge_rate_unit = CHARGE_RATE_UNIT_AMPS
        
        # Convert limit if using Watts
        if charge_rate_unit == CHARGE_RATE_UNIT_WATTS:
            # Need to get hub config for voltage/phases
            hub_entry_id = entry.data.get(CONF_HUB_ENTRY_ID)
            if hub_entry_id:
                hub_entry = hass.config_entries.async_get_entry(hub_entry_id)
                if hub_entry:
                    voltage = hub_entry.data.get(CONF_PHASE_VOLTAGE, DEFAULT_PHASE_VOLTAGE)
                    # Assume 3 phases for reset
                    limit_for_charger = round(evse_minimum_charge_current * voltage * 3, 1)
                    rate_unit = "W"
                else:
                    limit_for_charger = evse_minimum_charge_current
                    rate_unit = "A"
            else:
                limit_for_charger = evse_minimum_charge_current
                rate_unit = "A"
        else:
            limit_for_charger = evse_minimum_charge_current
            rate_unit = "A"
        
        # Stack level for reset should be 1 lower than regular operation
        configured_stack_level = entry.data.get(CONF_STACK_LEVEL, DEFAULT_STACK_LEVEL)
        reset_stack_level = max(1, configured_stack_level - 1)

        sequence = [
            {"service": "ocpp.clear_profile", "data": {"device_id": ocpp_device_id}},
            {"delay": {"seconds": 10}},
            {
                "service": "ocpp.set_charge_rate",
                "data": {
                    "device_id": ocpp_device_id,
                    "custom_profile": {
                        "chargingProfileId": 10,
                        "stackLevel": reset_stack_level,
                        "chargingProfileKind": "Relative",
                        "chargingProfilePurpose": "TxDefaultProfile",
                        "chargingSchedule": {
                            "chargingRateUnit": rate_unit,
                            "chargingSchedulePeriod": [
                                {"startPeriod": 0, "limit": limit_for_charger}
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
    
    # Forward setup to hub platforms (number, switch, sensor, select for hub-level entities)
    await hass.config_entries.async_forward_entry_setups(entry, ["number", "switch", "sensor", "select"])
    
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
    
    # Forward setup to charger platforms (sensor, number, button, select for charger-specific entities)
    await hass.config_entries.async_forward_entry_setups(entry, ["sensor", "number", "button", "select"])
    
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
        f"number.{entity_id}_home_battery_soc_min": f"{entity_id}_home_battery_soc_min",
        f"number.{entity_id}_power_buffer": f"{entity_id}_power_buffer",
        f"select.{entity_id}_charging_mode": f"{entity_id}_charging_mode",
        f"switch.{entity_id}_allow_grid_charging": f"{entity_id}_allow_grid_charging"
    }
    
    # Check and update existing entities to be associated with this config entry
    entities_migrated = []
    for entity_entity_id, unique_id in expected_entities.items():
        # Try to find entity by unique_id (this is the key for matching)
        existing_entity = None
        for reg_entity_id, reg_entity in entity_registry.entities.items():
            if reg_entity.unique_id == unique_id and reg_entity.platform == DOMAIN:
                existing_entity = reg_entity
                break
        
        if existing_entity:
            # Entity exists with this unique_id
            if existing_entity.config_entry_id != entry.entry_id:
                _LOGGER.info(f"Migrating existing entity {existing_entity.entity_id} (unique_id: {unique_id}) to hub config entry {entry.entry_id}")
                entity_registry.async_update_entity(
                    existing_entity.entity_id,
                    config_entry_id=entry.entry_id
                )
                entities_migrated.append(unique_id)
            else:
                _LOGGER.debug(f"Entity {existing_entity.entity_id} already associated with hub config entry")
                entities_migrated.append(unique_id)
        else:
            _LOGGER.info(f"Entity with unique_id {unique_id} will be created when the platform is set up")
    
    # Update the config entry to ensure it has the required entity IDs
    updated_data = dict(entry.data)
    updated_data[CONF_BATTERY_SOC_TARGET_ENTITY_ID] = f"number.{entity_id}_home_battery_soc_target"
    updated_data[CONF_CHARGIN_MODE_ENTITY_ID] = f"select.{entity_id}_charging_mode"
    updated_data[CONF_ALLOW_GRID_CHARGING_ENTITY_ID] = f"switch.{entity_id}_allow_grid_charging"
    updated_data[CONF_POWER_BUFFER_ENTITY_ID] = f"number.{entity_id}_power_buffer"
    updated_data["integration_version"] = INTEGRATION_VERSION
    
    hass.config_entries.async_update_entry(entry, data=updated_data)
    _LOGGER.info(f"Updated hub config entry with entity IDs. Migrated {len(entities_migrated)} entities")


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Unload a Dynamic OCPP EVSE config entry."""
    entry_type = entry.data.get(ENTRY_TYPE, ENTRY_TYPE_HUB)
    
    if entry_type == ENTRY_TYPE_HUB:
        # Unload hub platforms (includes select for distribution mode)
        for domain in ["number", "switch", "sensor", "select"]:
            await hass.config_entries.async_forward_entry_unload(entry, domain)
        
        # Remove hub from data
        if entry.entry_id in hass.data[DOMAIN]["hubs"]:
            del hass.data[DOMAIN]["hubs"][entry.entry_id]
    
    elif entry_type == ENTRY_TYPE_CHARGER:
        # Unload charger platforms
        for domain in ["sensor", "number", "button", "select"]:
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


def distribute_current_to_chargers(
    hass: HomeAssistant, 
    hub_entry_id: str, 
    total_available_current: float,
    charger_targets: dict = None  # {charger_entry_id: mode_target_current}
) -> dict:
    """
    Distribute available current to chargers based on distribution mode and priority.
    
    Distribution Modes:
    - Shared: Allocate minimums first, then distribute excess equally
    - Priority: Allocate minimums first, then distribute excess by priority
    - Sequential - Optimized: Allocate in priority order, use leftover if higher priority can't use it
    - Sequential - Strict: Fully satisfy each charger in strict priority order before moving to next
    
    Args:
        hass: HomeAssistant instance
        hub_entry_id: Hub config entry ID
        total_available_current: Total current available for distribution (A)
        charger_targets: Dict of mode-specific targets for each charger (optional, uses max_current if not provided)
    
    Returns:
        dict of {charger_entry_id: allocated_current}
    """
    chargers = get_chargers_for_hub(hass, hub_entry_id)
    if not chargers:
        return {}
    
    # Get distribution mode from hub's select entity
    hub_entry = hass.data[DOMAIN]["hubs"][hub_entry_id]["entry"]
    hub_entity_id = hub_entry.data.get(CONF_ENTITY_ID, "dynamic_ocpp_evse")
    distribution_mode_entity = f"select.{hub_entity_id}_distribution_mode"
    distribution_mode_state = hass.states.get(distribution_mode_entity)
    
    if distribution_mode_state and distribution_mode_state.state in [
        DISTRIBUTION_MODE_SHARED, DISTRIBUTION_MODE_PRIORITY,
        DISTRIBUTION_MODE_SEQUENTIAL_OPTIMIZED, DISTRIBUTION_MODE_SEQUENTIAL_STRICT
    ]:
        distribution_mode = distribution_mode_state.state
    else:
        distribution_mode = DEFAULT_DISTRIBUTION_MODE
        _LOGGER.debug(f"Distribution mode entity not found or invalid, using default: {distribution_mode}")
    
    # Build charger info list with priority
    charger_info = []
    for entry in chargers:
        min_current = entry.data.get(CONF_EVSE_MINIMUM_CHARGE_CURRENT, DEFAULT_MIN_CHARGE_CURRENT)
        max_current = entry.data.get(CONF_EVSE_MAXIMUM_CHARGE_CURRENT, DEFAULT_MAX_CHARGE_CURRENT)
        priority = entry.data.get(CONF_CHARGER_PRIORITY, DEFAULT_CHARGER_PRIORITY)
        
        # Determine effective max: use mode target if available, otherwise use configured max
        if charger_targets and entry.entry_id in charger_targets:
            mode_target = charger_targets[entry.entry_id]
            effective_max = min(max_current, mode_target) if mode_target > 0 else 0
        else:
            effective_max = max_current
        
        charger_info.append({
            "entry_id": entry.entry_id,
            "min_current": min_current,
            "max_current": max_current,
            "effective_max": effective_max,
            "priority": priority,
            "allocated": 0,
        })
    
    # Sort by priority (lower number = higher priority)
    charger_info.sort(key=lambda x: x["priority"])
    
    # Apply distribution algorithm based on mode
    if distribution_mode == DISTRIBUTION_MODE_SHARED:
        _distribute_shared(charger_info, total_available_current)
    elif distribution_mode == DISTRIBUTION_MODE_PRIORITY:
        _distribute_priority(charger_info, total_available_current)
    elif distribution_mode == DISTRIBUTION_MODE_SEQUENTIAL_OPTIMIZED:
        _distribute_sequential_optimized(charger_info, total_available_current)
    elif distribution_mode == DISTRIBUTION_MODE_SEQUENTIAL_STRICT:
        _distribute_sequential_strict(charger_info, total_available_current)
    
    # Build result dict and update global allocations
    result = {}
    for charger in charger_info:
        result[charger["entry_id"]] = round(charger["allocated"], 1)
        hass.data[DOMAIN]["charger_allocations"][charger["entry_id"]] = round(charger["allocated"], 1)
    
    _LOGGER.debug(
        f"Current distribution ({distribution_mode}) - Total: {total_available_current:.1f}A, "
        f"Allocations: {', '.join([f'{c['priority']}: {c['allocated']:.1f}A' for c in charger_info])}"
    )
    
    return result


def _distribute_shared(charger_info: list, total_available_current: float):
    """
    Shared mode: Allocate minimums first, then distribute excess equally.
    
    Phase 1: Give each charger its minimum (if target allows)
    Phase 2: Distribute remaining equally among active chargers
    """
    remaining_current = total_available_current
    active_chargers = []
    
    # Phase 1: Allocate minimums
    for charger in charger_info:
        if charger["effective_max"] >= charger["min_current"] and remaining_current >= charger["min_current"]:
            charger["allocated"] = charger["min_current"]
            remaining_current -= charger["min_current"]
            active_chargers.append(charger)
        else:
            charger["allocated"] = 0
    
    # Phase 2: Distribute excess equally
    if active_chargers and remaining_current > 0:
        while remaining_current > 0.1:
            distributed_any = False
            share = remaining_current / len(active_chargers)
            
            for charger in active_chargers:
                room = charger["effective_max"] - charger["allocated"]
                if room > 0:
                    add = min(share, room)
                    charger["allocated"] += add
                    remaining_current -= add
                    distributed_any = True
            
            if not distributed_any:
                break


def _distribute_priority(charger_info: list, total_available_current: float):
    """
    Priority mode: Allocate minimums first, then distribute excess by priority.
    
    Phase 1: Give each charger its minimum (in priority order, if target allows)
    Phase 2: Distribute remaining in priority order (satisfy higher priority first)
    """
    remaining_current = total_available_current
    
    # Phase 1: Allocate minimums
    for charger in charger_info:
        if charger["effective_max"] >= charger["min_current"] and remaining_current >= charger["min_current"]:
            charger["allocated"] = charger["min_current"]
            remaining_current -= charger["min_current"]
        else:
            charger["allocated"] = 0
    
    # Phase 2: Distribute excess by priority
    if remaining_current > 0:
        for charger in charger_info:
            if charger["allocated"] > 0:  # Only distribute to active chargers
                room = charger["effective_max"] - charger["allocated"]
                if room > 0:
                    add = min(remaining_current, room)
                    charger["allocated"] += add
                    remaining_current -= add
                    
                    if remaining_current <= 0.1:
                        break


def _distribute_sequential_optimized(charger_info: list, total_available_current: float):
    """
    Sequential - Optimized mode: Allocate in priority order, use leftover if higher priority can't use it.
    
    For each charger in priority order:
    - Allocate up to min(remaining, effective_max)
    - If allocated >= min_current, accept it; otherwise allocate 0 and continue
    """
    remaining_current = total_available_current
    
    for charger in charger_info:
        if charger["effective_max"] <= 0:
            # Mode says charger doesn't want any current
            charger["allocated"] = 0
            continue
        
        # Try to allocate up to effective_max
        potential_allocation = min(remaining_current, charger["effective_max"])
        
        if potential_allocation >= charger["min_current"]:
            # Can allocate at least minimum - accept it
            charger["allocated"] = potential_allocation
            remaining_current -= potential_allocation
        else:
            # Can't reach minimum - don't allocate anything
            charger["allocated"] = 0


def _distribute_sequential_strict(charger_info: list, total_available_current: float):
    """
    Sequential - Strict mode: Fully satisfy each charger before moving to next.
    
    For each charger in priority order:
    - Only allocate if previous charger is fully satisfied (at effective_max)
    - Allocate up to effective_max if possible
    - If can't reach minimum, allocate 0
    """
    remaining_current = total_available_current
    previous_satisfied = True  # First charger can always try
    
    for charger in charger_info:
        if not previous_satisfied:
            # Previous charger wasn't fully satisfied - skip this one
            charger["allocated"] = 0
            continue
        
        if charger["effective_max"] <= 0:
            # Mode says charger doesn't want any current
            charger["allocated"] = 0
            previous_satisfied = True  # Consider it "satisfied" (doesn't want current anyway)
            continue
        
        # Try to allocate up to effective_max
        potential_allocation = min(remaining_current, charger["effective_max"])
        
        if potential_allocation >= charger["min_current"]:
            # Can allocate at least minimum
            charger["allocated"] = potential_allocation
            remaining_current -= potential_allocation
            
            # Check if this charger is fully satisfied
            previous_satisfied = (charger["allocated"] >= charger["effective_max"] - 0.1)
        else:
            # Can't reach minimum - don't allocate anything
            charger["allocated"] = 0
            previous_satisfied = False


def get_charger_allocation(hass: HomeAssistant, charger_entry_id: str) -> float:
    """Get the current allocation for a specific charger."""
    return hass.data[DOMAIN]["charger_allocations"].get(charger_entry_id, 0)
