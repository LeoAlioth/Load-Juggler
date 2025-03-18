from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.config_entries import ConfigEntry
from .const import DOMAIN

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback):
    """Set up the button entity."""
    async_add_entities([ResetButton(hass, entry)])

class ResetButton(ButtonEntity):
    """Representation of a reset button."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry):
        """Initialize the button."""
        self._hass = hass
        self._entry = entry
        self._attr_name = "Reset OCPP EVSE"
        self._attr_unique_id = f"{entry.entry_id}_reset_button"

    async def async_press(self) -> None:
        """Handle the button press."""
        await self._hass.services.async_call(DOMAIN, "reset_ocpp_evse")