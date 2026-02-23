from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.config_entries import ConfigEntry, SOURCE_INTEGRATION_DISCOVERY
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.service import async_register_admin_service
from homeassistant.helpers.script import Script
from homeassistant.helpers.entity_registry import async_get as async_get_entity_registry
from homeassistant.helpers.device_registry import async_get as async_get_device_registry
from datetime import datetime, timedelta
import logging
import voluptuous as vol
from .const import *
from .helpers import get_entry_value, prettify_name

_LOGGER = logging.getLogger(__name__)

# Define the config schema
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

# Integration version for entity migration
INTEGRATION_VERSION = "2.0.0"


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate old entry to new version."""
    _LOGGER.info("Migrating from version %s.%s to version 2.2",
                 entry.version,
                 getattr(entry, 'minor_version', 0))

    if entry.version < 2:
        # Migrate from V1 (single config) to V2 (hub + charger architecture)
        new_data = dict(entry.data)
        
        # Mark this as a hub entry (legacy entries become hubs)
        new_data[ENTRY_TYPE] = ENTRY_TYPE_HUB
        
        # Generate entity IDs for hub-created entities if not present
        entity_id = new_data.get(CONF_ENTITY_ID, "dynamic_ocpp_evse")
        if CONF_BATTERY_SOC_TARGET_ENTITY_ID not in new_data:
            new_data[CONF_BATTERY_SOC_TARGET_ENTITY_ID] = f"number.{entity_id}_home_battery_soc_target"
        if CONF_ALLOW_GRID_CHARGING_ENTITY_ID not in new_data:
            new_data[CONF_ALLOW_GRID_CHARGING_ENTITY_ID] = f"switch.{entity_id}_allow_grid_charging"
        if CONF_POWER_BUFFER_ENTITY_ID not in new_data:
            new_data[CONF_POWER_BUFFER_ENTITY_ID] = f"number.{entity_id}_power_buffer"
        
        # Update the config entry with new version
        options = dict(entry.options)
        options.setdefault(CONF_EVSE_MINIMUM_CHARGE_CURRENT, new_data.get(CONF_EVSE_MINIMUM_CHARGE_CURRENT, DEFAULT_MIN_CHARGE_CURRENT))
        options.setdefault(CONF_EVSE_MAXIMUM_CHARGE_CURRENT, new_data.get(CONF_EVSE_MAXIMUM_CHARGE_CURRENT, DEFAULT_MAX_CHARGE_CURRENT))
        options.setdefault(CONF_UPDATE_FREQUENCY, new_data.get(CONF_UPDATE_FREQUENCY, DEFAULT_UPDATE_FREQUENCY))
        options.setdefault(CONF_OCPP_PROFILE_TIMEOUT, new_data.get(CONF_OCPP_PROFILE_TIMEOUT, DEFAULT_OCPP_PROFILE_TIMEOUT))
        options.setdefault(CONF_CHARGE_PAUSE_DURATION, new_data.get(CONF_CHARGE_PAUSE_DURATION, DEFAULT_CHARGE_PAUSE_DURATION))
        options.setdefault(CONF_STACK_LEVEL, new_data.get(CONF_STACK_LEVEL, DEFAULT_STACK_LEVEL))
        options.setdefault(CONF_CHARGE_RATE_UNIT, new_data.get(CONF_CHARGE_RATE_UNIT, DEFAULT_CHARGE_RATE_UNIT))
        options.setdefault(CONF_PROFILE_VALIDITY_MODE, new_data.get(CONF_PROFILE_VALIDITY_MODE, DEFAULT_PROFILE_VALIDITY_MODE))
        options.setdefault(CONF_BATTERY_SOC_ENTITY_ID, new_data.get(CONF_BATTERY_SOC_ENTITY_ID))
        options.setdefault(CONF_BATTERY_POWER_ENTITY_ID, new_data.get(CONF_BATTERY_POWER_ENTITY_ID))
        options.setdefault(CONF_BATTERY_MAX_CHARGE_POWER, new_data.get(CONF_BATTERY_MAX_CHARGE_POWER, DEFAULT_BATTERY_MAX_POWER))
        options.setdefault(CONF_BATTERY_MAX_DISCHARGE_POWER, new_data.get(CONF_BATTERY_MAX_DISCHARGE_POWER, DEFAULT_BATTERY_MAX_POWER))
        options.setdefault(CONF_BATTERY_SOC_HYSTERESIS, new_data.get(CONF_BATTERY_SOC_HYSTERESIS, DEFAULT_BATTERY_SOC_HYSTERESIS))

        hass.config_entries.async_update_entry(
            entry,
            data=new_data,
            options=options,
            version=2,
            minor_version=2
        )

        _LOGGER.info(
            "Migration to version 2.2 successful. Legacy entry converted to hub. "
            "You will need to add chargers separately after migration."
        )

        return True

    # Handle minor version updates if version is already 2
    if entry.version == 2 and getattr(entry, 'minor_version', 0) < 1:
        options = dict(entry.options)
        data = entry.data
        options.setdefault(CONF_EVSE_MINIMUM_CHARGE_CURRENT, data.get(CONF_EVSE_MINIMUM_CHARGE_CURRENT, DEFAULT_MIN_CHARGE_CURRENT))
        options.setdefault(CONF_EVSE_MAXIMUM_CHARGE_CURRENT, data.get(CONF_EVSE_MAXIMUM_CHARGE_CURRENT, DEFAULT_MAX_CHARGE_CURRENT))
        options.setdefault(CONF_UPDATE_FREQUENCY, data.get(CONF_UPDATE_FREQUENCY, DEFAULT_UPDATE_FREQUENCY))
        options.setdefault(CONF_OCPP_PROFILE_TIMEOUT, data.get(CONF_OCPP_PROFILE_TIMEOUT, DEFAULT_OCPP_PROFILE_TIMEOUT))
        options.setdefault(CONF_CHARGE_PAUSE_DURATION, data.get(CONF_CHARGE_PAUSE_DURATION, DEFAULT_CHARGE_PAUSE_DURATION))
        options.setdefault(CONF_STACK_LEVEL, data.get(CONF_STACK_LEVEL, DEFAULT_STACK_LEVEL))
        options.setdefault(CONF_CHARGE_RATE_UNIT, data.get(CONF_CHARGE_RATE_UNIT, DEFAULT_CHARGE_RATE_UNIT))
        options.setdefault(CONF_PROFILE_VALIDITY_MODE, data.get(CONF_PROFILE_VALIDITY_MODE, DEFAULT_PROFILE_VALIDITY_MODE))
        options.setdefault(CONF_BATTERY_SOC_ENTITY_ID, data.get(CONF_BATTERY_SOC_ENTITY_ID))
        options.setdefault(CONF_BATTERY_POWER_ENTITY_ID, data.get(CONF_BATTERY_POWER_ENTITY_ID))
        options.setdefault(CONF_BATTERY_MAX_CHARGE_POWER, data.get(CONF_BATTERY_MAX_CHARGE_POWER, DEFAULT_BATTERY_MAX_POWER))
        options.setdefault(CONF_BATTERY_MAX_DISCHARGE_POWER, data.get(CONF_BATTERY_MAX_DISCHARGE_POWER, DEFAULT_BATTERY_MAX_POWER))
        options.setdefault(CONF_BATTERY_SOC_HYSTERESIS, data.get(CONF_BATTERY_SOC_HYSTERESIS, DEFAULT_BATTERY_SOC_HYSTERESIS))

        hass.config_entries.async_update_entry(
            entry,
            options=options,
            minor_version=1
        )
        _LOGGER.info("Updated minor version to 1 and seeded options")

    # Migrate 2.1 → 2.2: convert charge_pause_duration from seconds to minutes
    if entry.version == 2 and getattr(entry, 'minor_version', 0) < 2:
        options = dict(entry.options)
        old_pause = options.get(CONF_CHARGE_PAUSE_DURATION)
        if old_pause is not None and old_pause > 10:
            # Value is in seconds (old format) — convert to minutes
            new_pause = max(1, round(old_pause / 60))
            options[CONF_CHARGE_PAUSE_DURATION] = new_pause
            _LOGGER.info("Migrated charge_pause_duration from %ds to %dmin", old_pause, new_pause)

        hass.config_entries.async_update_entry(
            entry,
            options=options,
            minor_version=2
        )
        _LOGGER.info("Updated minor version to 2")
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

        evse_minimum_charge_current = get_entry_value(entry, CONF_EVSE_MINIMUM_CHARGE_CURRENT, DEFAULT_MIN_CHARGE_CURRENT)
        
        # Get charge rate unit from charger config
        charge_rate_unit = get_entry_value(entry, CONF_CHARGE_RATE_UNIT, DEFAULT_CHARGE_RATE_UNIT)
        
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
            # Need to get hub config for voltage and charger config for phases
            hub_entry_id = entry.data.get(CONF_HUB_ENTRY_ID)
            if hub_entry_id:
                hub_entry = hass.config_entries.async_get_entry(hub_entry_id)
                if hub_entry:
                    voltage = hub_entry.data.get(CONF_PHASE_VOLTAGE, DEFAULT_PHASE_VOLTAGE)
                    charger_phases = int(entry.data.get(CONF_PHASES, 3) or 3)
                    limit_for_charger = round(evse_minimum_charge_current * voltage * charger_phases, 1)
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
        configured_stack_level = int(get_entry_value(entry, CONF_STACK_LEVEL, DEFAULT_STACK_LEVEL))
        reset_stack_level = max(1, configured_stack_level - 1)

        sequence = [
            {
                "action": "ocpp.clear_profile",
                "target": {},
                "data": {"devid": ocpp_device_id}
            },
            {"delay": {"seconds": 10}},
            {
                "action": "ocpp.set_charge_rate",
                "target": {},
                "data": {
                    "devid": ocpp_device_id,
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
        script = Script(hass, sequence, "Reset OCPP EVSE", DOMAIN)
        await script.async_run(context=call.context)

    hass.services.async_register(DOMAIN, "reset_ocpp_evse", handle_reset_service)

    # --- Helper to find an entity by unique_id suffix within a config entry ---
    def _find_entity_state(entity_id_suffix: str, config_entry_id: str):
        """Find an entity's HA entity_id by matching unique_id pattern."""
        entity_registry = async_get_entity_registry(hass)
        for eid, entity in entity_registry.entities.items():
            if (entity.config_entry_id == config_entry_id
                    and entity.platform == DOMAIN
                    and entity.unique_id.endswith(entity_id_suffix)):
                return eid
        return None

    # --- set_operating_mode service ---
    async def handle_set_operating_mode(call: ServiceCall):
        """Set the operating mode for a charger."""
        entry_id = call.data["entry_id"]
        mode = call.data["mode"]

        entity_id = _find_entity_state("_operating_mode", entry_id)
        if not entity_id:
            _LOGGER.error("Could not find operating mode entity for charger %s", entry_id)
            return

        await hass.services.async_call(
            "select", "select_option",
            {"entity_id": entity_id, "option": mode},
            blocking=True,
        )

    hass.services.async_register(
        DOMAIN, "set_operating_mode", handle_set_operating_mode,
        schema=vol.Schema({
            vol.Required("entry_id"): cv.string,
            vol.Required("mode"): vol.In([
                OPERATING_MODE_STANDARD, OPERATING_MODE_CONTINUOUS,
                OPERATING_MODE_SOLAR_PRIORITY, OPERATING_MODE_SOLAR_ONLY,
                OPERATING_MODE_EXCESS,
            ]),
        }),
    )

    # --- set_distribution_mode service ---
    async def handle_set_distribution_mode(call: ServiceCall):
        """Set the distribution mode for a hub."""
        entry_id = call.data["entry_id"]
        mode = call.data["mode"]

        entity_id = _find_entity_state("_distribution_mode", entry_id)
        if not entity_id:
            _LOGGER.error("Could not find distribution mode entity for hub %s", entry_id)
            return

        await hass.services.async_call(
            "select", "select_option",
            {"entity_id": entity_id, "option": mode},
            blocking=True,
        )

    hass.services.async_register(
        DOMAIN, "set_distribution_mode", handle_set_distribution_mode,
        schema=vol.Schema({
            vol.Required("entry_id"): cv.string,
            vol.Required("mode"): vol.In([
                DISTRIBUTION_MODE_SHARED, DISTRIBUTION_MODE_PRIORITY,
                DISTRIBUTION_MODE_SEQUENTIAL_OPTIMIZED, DISTRIBUTION_MODE_SEQUENTIAL_STRICT,
            ]),
        }),
    )

    # --- set_max_current service ---
    async def handle_set_max_current(call: ServiceCall):
        """Set the max current for a charger."""
        entry_id = call.data["entry_id"]
        current = call.data["current"]

        entity_id = _find_entity_state("_max_current", entry_id)
        if not entity_id:
            _LOGGER.error("Could not find max current entity for charger %s", entry_id)
            return

        await hass.services.async_call(
            "number", "set_value",
            {"entity_id": entity_id, "value": current},
            blocking=True,
        )

    hass.services.async_register(
        DOMAIN, "set_max_current", handle_set_max_current,
        schema=vol.Schema({
            vol.Required("entry_id"): cv.string,
            vol.Required("current"): vol.Coerce(float),
        }),
    )

    # --- set_min_current service ---
    async def handle_set_min_current(call: ServiceCall):
        """Set the min current for a charger."""
        entry_id = call.data["entry_id"]
        current = call.data["current"]

        entity_id = _find_entity_state("_min_current", entry_id)
        if not entity_id:
            _LOGGER.error("Could not find min current entity for charger %s", entry_id)
            return

        await hass.services.async_call(
            "number", "set_value",
            {"entity_id": entity_id, "value": current},
            blocking=True,
        )

    hass.services.async_register(
        DOMAIN, "set_min_current", handle_set_min_current,
        schema=vol.Schema({
            vol.Required("entry_id"): cv.string,
            vol.Required("current"): vol.Coerce(float),
        }),
    )

    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Set up Dynamic OCPP EVSE from a config entry."""
    hass.data.setdefault(DOMAIN, {
        "hubs": {},
        "chargers": {},
        "groups": {},  # Circuit group entries
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
    elif entry_type == ENTRY_TYPE_GROUP:
        await _setup_group_entry(hass, entry)

    # Reload entry when options change (e.g. battery entities added/removed)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    return True


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry):
    """Reload the config entry when options are changed."""
    await hass.config_entries.async_reload(entry.entry_id)


async def _setup_hub_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Set up a hub config entry."""
    _LOGGER.info("Setting up hub entry: %s", entry.title)
    
    # Store hub data (runtime state written by entities, read by calculation)
    hass.data[DOMAIN]["hubs"][entry.entry_id] = {
        "entry": entry,
        "chargers": [],  # List of charger entry_ids linked to this hub
        "groups": [],    # List of circuit group entry_ids linked to this hub
        "distribution_mode": DEFAULT_DISTRIBUTION_MODE,
        "allow_grid_charging": True,
        "power_buffer": 0,
        "max_import_power": None,
        "battery_soc_target": DEFAULT_BATTERY_SOC_TARGET,
        "battery_soc_min": DEFAULT_BATTERY_SOC_MIN,
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
    
    # Store charger data (runtime state written by entities, read by calculation)
    device_type = entry.data.get(CONF_DEVICE_TYPE, DEVICE_TYPE_EVSE)
    default_mode = DEFAULT_OPERATING_MODE_PLUG if device_type == DEVICE_TYPE_PLUG else DEFAULT_OPERATING_MODE_EVSE
    hass.data[DOMAIN]["chargers"][entry.entry_id] = {
        "entry": entry,
        "hub_entry_id": hub_entry_id,
        "min_current": None,
        "max_current": None,
        "device_power": None,
        "dynamic_control": True,
        "operating_mode": default_mode,
    }
    
    # Link charger to hub
    hass.data[DOMAIN]["hubs"][hub_entry_id]["chargers"].append(entry.entry_id)
    
    # Initialize charger allocation
    hass.data[DOMAIN]["charger_allocations"][entry.entry_id] = 0
    
    # Forward setup to charger platforms
    await hass.config_entries.async_forward_entry_setups(entry, ["sensor", "number", "button", "select", "switch"])
    
    return True


async def _setup_group_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Set up a circuit group config entry."""
    _LOGGER.info("Setting up circuit group entry: %s", entry.title)

    hub_entry_id = entry.data.get(CONF_HUB_ENTRY_ID)

    # Verify hub exists
    if hub_entry_id not in hass.data[DOMAIN]["hubs"]:
        _LOGGER.error("Hub entry %s not found for group %s", hub_entry_id, entry.title)
        return False

    # Store group data
    hass.data[DOMAIN]["groups"][entry.entry_id] = {
        "entry": entry,
        "hub_entry_id": hub_entry_id,
    }

    # Link group to hub
    hass.data[DOMAIN]["hubs"][hub_entry_id]["groups"].append(entry.entry_id)

    # Forward setup to sensor platform only (group sensors)
    await hass.config_entries.async_forward_entry_setups(entry, ["sensor"])

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
                device_name = prettify_name(base_name)
                device_id = None

                if entity.device_id:
                    device = device_registry.async_get(entity.device_id)
                    if device:
                        device_name = prettify_name(device.name) if device.name else device_name
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
        for domain in ["sensor", "number", "button", "select", "switch"]:
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

    elif entry_type == ENTRY_TYPE_GROUP:
        # Unload group platforms
        await hass.config_entries.async_forward_entry_unload(entry, "sensor")

        # Remove group from hub's list
        hub_entry_id = entry.data.get(CONF_HUB_ENTRY_ID)
        if hub_entry_id in hass.data[DOMAIN]["hubs"]:
            groups_list = hass.data[DOMAIN]["hubs"][hub_entry_id].get("groups", [])
            if entry.entry_id in groups_list:
                groups_list.remove(entry.entry_id)

        # Remove group from data
        if entry.entry_id in hass.data[DOMAIN]["groups"]:
            del hass.data[DOMAIN]["groups"][entry.entry_id]

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


def get_groups_for_hub(hass: HomeAssistant, hub_entry_id: str) -> list[ConfigEntry]:
    """Get all circuit group config entries for a hub."""
    hub_data = hass.data[DOMAIN]["hubs"].get(hub_entry_id)
    if not hub_data:
        return []

    groups = []
    for group_entry_id in hub_data.get("groups", []):
        group_data = hass.data[DOMAIN]["groups"].get(group_entry_id)
        if group_data:
            groups.append(group_data.get("entry"))

    return groups
