from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.service import async_register_admin_service
from homeassistant.helpers.script import Script
from homeassistant.components.button import ButtonEntity
from .const import *

# Define the config schema
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

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

    # Forward the setup to the sensor, select, button, and number platforms
    await hass.config_entries.async_forward_entry_setups(entry, ["sensor", "select", "button", "number"])

    return True

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Unload a Dynamic OCPP EVSE config entry."""
    # Unload each domain separately
    for domain in ["sensor", "select", "button", "number"]:
        await hass.config_entries.async_forward_entry_unload(entry, domain)
    hass.data[DOMAIN].pop(entry.entry_id)
    return True
