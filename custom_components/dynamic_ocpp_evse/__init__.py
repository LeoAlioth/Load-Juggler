from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.service import async_register_admin_service
from homeassistant.helpers.script import Script
from homeassistant.components.button import ButtonEntity
from homeassistant.helpers.entity_registry import async_get as async_get_entity_registry
import logging
from .const import *

_LOGGER = logging.getLogger(__name__)

# Define the config schema
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

# Integration version for entity migration
INTEGRATION_VERSION = "1.1.0"

async def async_setup(hass: HomeAssistant, config: dict):
    """Set up the Dynamic OCPP EVSE component."""
    
    async def handle_reset_service(call):
        """Handle the reset service call."""
        
        entry_id = call.data.get("entry_id")
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry is None:
            return

        evse_minimum_charge_current = entry.data.get(CONF_EVSE_MINIMUM_CHARGE_CURRENT, 6)  # Default to 6 if not set

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
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = entry

    # Check if this is an update and we need to migrate entities
    await _migrate_entities_if_needed(hass, entry)

    # Forward the setup to the sensor, select, button, number, and switch platforms
    await hass.config_entries.async_forward_entry_setups(entry, ["sensor", "select", "button", "number", "switch"])

    return True

async def _migrate_entities_if_needed(hass: HomeAssistant, entry: ConfigEntry):
    """Check if new entities need to be created after an integration update."""
    entity_registry = async_get_entity_registry(hass)
    entity_id = entry.data.get(CONF_ENTITY_ID)
    
    if not entity_id:
        _LOGGER.warning("No entity_id found in config entry, skipping entity migration")
        return
    
    # Define expected entities that should exist
    expected_entities = [
        f"number.{entity_id}_min_current",
        f"number.{entity_id}_max_current", 
        f"number.{entity_id}_home_battery_soc_target",
        f"select.{entity_id}_charging_mode",
        f"switch.{entity_id}_allow_grid_charging"
    ]
    
    missing_entities = []
    for expected_entity in expected_entities:
        if expected_entity not in entity_registry.entities:
            missing_entities.append(expected_entity)
    
    if missing_entities:
        _LOGGER.info(f"Found missing entities after update: {missing_entities}")
        _LOGGER.info("These entities will be created when the platforms are set up")
        
        # Update the config entry to ensure it has the required entity IDs
        updated_data = dict(entry.data)
        updated_data[CONF_MIN_CURRENT_ENTITY_ID] = f"number.{entity_id}_min_current"
        updated_data[CONF_MAX_CURRENT_ENTITY_ID] = f"number.{entity_id}_max_current"
        updated_data[CONF_BATTERY_SOC_TARGET_ENTITY_ID] = f"number.{entity_id}_home_battery_soc_target"
        updated_data[CONF_CHARGIN_MODE_ENTITY_ID] = f"select.{entity_id}_charging_mode"
        updated_data[CONF_ALLOW_GRID_CHARGING_ENTITY_ID] = f"switch.{entity_id}_allow_grid_charging"
        updated_data["integration_version"] = INTEGRATION_VERSION
        
        hass.config_entries.async_update_entry(entry, data=updated_data)
        _LOGGER.info("Updated config entry with missing entity IDs")

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Unload a Dynamic OCPP EVSE config entry."""
    # Unload each domain separately
    for domain in ["sensor", "select", "button", "number", "switch"]:
        await hass.config_entries.async_forward_entry_unload(entry, domain)
    hass.data[DOMAIN].pop(entry.entry_id)
    return True
