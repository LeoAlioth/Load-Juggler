import logging
from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass: HomeAssistant, config_entry: ConfigEntry, async_add_entities: AddEntitiesCallback):
    """Set up the Dynamic OCPP EVSE Select from a config entry."""
    name = config_entry.data["name"]
    async_add_entities([DynamicOcppEvseSelect(hass, config_entry, name)])

class DynamicOcppEvseSelect(SelectEntity):
    """Representation of a Dynamic OCPP EVSE Select."""

    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry, name: str):
        """Initialize the select entity."""
        self.hass = hass
        self.config_entry = config_entry
        self._attr_name = f"{name} Charging Mode"
        self._attr_unique_id = f"{config_entry.entry_id}_charging_mode"
        self._attr_options = ["Standard", "Eco", "Solar", "Excess"]
        self._attr_current_option = "Eco"

    async def async_select_option(self, option: str) -> None:
        """Change the selected option."""
        if option in self._attr_options:
            self._attr_current_option = option
            self.async_write_ha_state()
        else:
            _LOGGER.error(f"Invalid option selected: {option}")