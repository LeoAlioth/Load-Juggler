import logging
from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass: HomeAssistant, config_entry: ConfigEntry, async_add_entities: AddEntitiesCallback):
    """Set up the Dynamic OCPP EVSE Select from a config entry."""
    name = config_entry.data["name"]
    
    entities = [DynamicOcppEvseSelect(hass, config_entry, name)]
    _LOGGER.info(f"Setting up select entities: {[entity.unique_id for entity in entities]}")
    async_add_entities(entities)

class DynamicOcppEvseSelect(SelectEntity, RestoreEntity):
    """Representation of a Dynamic OCPP EVSE Select."""

    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry, name: str):
        """Initialize the select entity."""
        self.hass = hass
        self.config_entry = config_entry
        self._attr_name = f"{name} Charging Mode"
        self._attr_unique_id = f"{config_entry.entry_id}_charging_mode"
        self._attr_options = ["Standard", "Eco", "Solar", "Excess"]
        self._attr_current_option = "Standard"  # Default, will be overridden by restore

    async def async_added_to_hass(self) -> None:
        """Restore last state when added to hass."""
        await super().async_added_to_hass()
        
        # Try to restore the last state
        last_state = await self.async_get_last_state()
        if last_state is not None and last_state.state in self._attr_options:
            self._attr_current_option = last_state.state
            _LOGGER.debug(f"Restored charging mode to: {self._attr_current_option}")
        else:
            _LOGGER.debug(f"No valid state to restore, using default: {self._attr_current_option}")

    async def async_select_option(self, option: str) -> None:
        """Change the selected option."""
        if option in self._attr_options:
            self._attr_current_option = option
            self.async_write_ha_state()
        else:
            _LOGGER.error(f"Invalid option selected: {option}")
