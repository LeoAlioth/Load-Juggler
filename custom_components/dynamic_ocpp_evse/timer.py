import logging
from homeassistant.components.timer import TimerEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from .const import DOMAIN, ENTRY_TYPE_CHARGER

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, config_entry: ConfigEntry, async_add_entities: AddEntitiesCallback):
    """Set up timer entities for charger entries."""
    entry_type = config_entry.data.get(ENTRY_TYPE_CHARGER)
    
    # Only set up timers for charger entries
    if entry_type != ENTRY_TYPE_CHARGER:
        _LOGGER.debug("Skipping timer setup for non-charger entry: %s", config_entry.title)
        return
    
    entity_id = config_entry.data.get("entity_id")
    name = config_entry.data.get("name")
    
    # Create a unique ID for the charge pause timer
    timer_unique_id = f"{entity_id}_charge_pause_timer"
    
    entities = [ChargePauseTimer(hass, config_entry, entity_id, name, timer_unique_id)]
    _LOGGER.info(f"Setting up timer entities: {timer_unique_id}")
    async_add_entities(entities)


class ChargePauseTimer(RestoreEntity, TimerEntity):
    """Timer for pausing charging when current drops below minimum threshold."""
    
    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry, entity_id: str, name: str, unique_id: str):
        """Initialize the charge pause timer."""
        self.hass = hass
        self.config_entry = config_entry
        self._attr_name = f"{name} Charge Pause Timer"
        self._attr_unique_id = unique_id
        self._entity_id = entity_id
        
        # Timer duration from config (default: 180 seconds)
        self._duration_seconds = config_entry.data.get("charge_pause_duration", 180)
        
        # State tracking - using timer component conventions
        self._attr_icon = "mdi:timer"
        self._state = None
        self._remaining = None
        
    @property
    def device_info(self):
        """Return device information about this charger."""
        hub_entity_id = self.config_entry.data.get("hub_entry_id")
        return {
            "identifiers": {(DOMAIN, self.config_entry.entry_id)},
            "name": self.config_entry.data.get("name"),
            "manufacturer": "Dynamic OCPP EVSE",
            "model": "EV Charger",
            "via_device": (DOMAIN, hub_entity_id) if hub_entity_id else None,
        }
    
    @property
    def state(self):
        """Return the current state of the timer."""
        return self._state

    @property
    def icon(self):
        """Return the icon to use in the frontend."""
        return "mdi:timer"
    
    @property
    def extra_state_attributes(self):
        """Return the optional state attributes."""
        attrs = {}
        if self._remaining is not None:
            attrs["remaining"] = str(self._remaining)
        if hasattr(self, '_duration_seconds'):
            attrs["duration"] = f"00:03:{self._duration_seconds:02d}"
        return attrs

    async def async_start_timer(self, **kwargs):
        """Start the timer."""
        from datetime import timedelta
        
        # Use duration from kwargs or default
        duration = kwargs.get("duration", self._duration_seconds)
        
        # Convert to timedelta if needed
        if isinstance(duration, int):
            duration = timedelta(seconds=duration)
        
        self._state = "active"
        self._remaining = str(duration)
        self.async_write_ha_state()
        
        _LOGGER.debug(f"Charge pause timer started for {self.config_entry.data.get('name')}")

    async def async_cancel_timer(self, **kwargs):
        """Cancel the timer."""
        self._state = "idle"
        self._remaining = None
        self.async_write_ha_state()
        
        _LOGGER.debug(f"Charge pause timer cancelled for {self.config_entry.data.get('name')}")

    async def async_finish_timer(self, **kwargs):
        """Finish the timer (mark as completed)."""
        self._state = "idle"
        self._remaining = None
        self.async_write_ha_state()
        
        _LOGGER.debug(f"Charge pause timer finished for {self.config_entry.data.get('name')}")

    async def async_added_to_hass(self):
        """Restore previous state when added to hass."""
        await super().async_added_to_hass()
        
        last_state = await self.async_get_last_state()
        if last_state is not None and last_state.state != "idle":
            self._state = last_state.state
            self._remaining = last_state.attributes.get("remaining")
            _LOGGER.debug(f"Restored timer state to: {self._state}")
        else:
            self._state = "idle"
            self._remaining = None
        
        self.async_write_ha_state()