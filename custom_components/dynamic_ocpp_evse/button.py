import logging
from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.config_entries import ConfigEntry
from .const import DOMAIN, ENTRY_TYPE, ENTRY_TYPE_CHARGER, CONF_NAME, CONF_ENTITY_ID

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback):
    """Set up the button entity."""
    # Only set up button entities for charger entries
    entry_type = entry.data.get(ENTRY_TYPE)
    if entry_type != ENTRY_TYPE_CHARGER:
        _LOGGER.debug("Skipping button setup for non-charger entry: %s", entry.title)
        return
    
    name = entry.data.get(CONF_NAME, "OCPP Charger")
    entity_id = entry.data.get(CONF_ENTITY_ID, "charger")
    
    async_add_entities([ResetButton(hass, entry, name, entity_id)])
    _LOGGER.info(f"Setting up charger reset button for: {name}")


class ResetButton(ButtonEntity):
    """Representation of a reset button for a charger."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, name: str, entity_id: str):
        """Initialize the button."""
        self._hass = hass
        self._entry = entry
        self._attr_name = f"{name} Reset OCPP"
        self._attr_unique_id = f"{entity_id}_reset_button"
        self._attr_icon = "mdi:restart"

    async def async_press(self) -> None:
        """Handle the button press."""
        _LOGGER.info(f"Reset button pressed for charger: {self._entry.title}")
        await self._hass.services.async_call(
            DOMAIN, 
            "reset_ocpp_evse",
            {"entry_id": self._entry.entry_id}
        )
