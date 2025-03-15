from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers import config_validation as cv
from .const import DOMAIN

# Define the config schema
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

async def async_setup(hass: HomeAssistant, config: dict):
    """Set up the Dynamic OCPP EVSE component."""
    return True

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Set up Dynamic OCPP EVSE from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = entry

    # Forward the setup to the sensor and select platforms
    await hass.config_entries.async_forward_entry_setups(entry, ["sensor", "select"])

    return True

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Unload a Dynamic OCPP EVSE config entry."""
    await hass.config_entries.async_forward_entry_unload(entry, ["sensor", "select"])
    hass.data[DOMAIN].pop(entry.entry_id)
    return True
