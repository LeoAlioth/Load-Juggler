import logging
from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from .const import DOMAIN, ENTRY_TYPE, ENTRY_TYPE_HUB, CONF_NAME, CONF_ENTITY_ID
from .helpers import get_entry_value

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, config_entry: ConfigEntry, async_add_entities: AddEntitiesCallback):
    """Set up switch entities."""
    # Only set up switch entities for hub entries
    entry_type = config_entry.data.get(ENTRY_TYPE)
    if entry_type != ENTRY_TYPE_HUB:
        _LOGGER.debug("Skipping switch setup for non-hub entry: %s", config_entry.title)
        return
    
    # Check if battery is configured
    battery_soc_entity = get_entry_value(config_entry, "battery_soc_entity_id")
    battery_power_entity = get_entry_value(config_entry, "battery_power_entity_id")
    has_battery = bool(battery_soc_entity or battery_power_entity)
    
    if not has_battery:
        _LOGGER.info("No battery configured - skipping 'Allow Grid Charging' switch")
        return
    
    entity_id = config_entry.data.get(CONF_ENTITY_ID, "dynamic_ocpp_evse")
    name = config_entry.data.get(CONF_NAME, "Dynamic OCPP EVSE")
    
    entities = [AllowGridChargingSwitch(hass, config_entry, entity_id, name)]
    _LOGGER.info(f"Setting up hub switch entities: {[entity.unique_id for entity in entities]}")
    async_add_entities(entities)


class AllowGridChargingSwitch(SwitchEntity, RestoreEntity):
    """Switch to allow/disallow grid charging (hub-level)."""
    
    _attr_entity_category = EntityCategory.CONFIG
    
    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry, entity_id: str, name: str):
        self.hass = hass
        self.config_entry = config_entry
        self._attr_name = f"{name} Allow Grid Charging"
        self._attr_unique_id = f"{entity_id}_allow_grid_charging"
        self._state = True  # Default: allow grid charging
        self._attr_icon = "mdi:transmission-tower"

    @property
    def device_info(self):
        """Return device information about this hub."""
        return {
            "identifiers": {(DOMAIN, self.config_entry.entry_id)},
            "name": self.config_entry.data.get(CONF_NAME, "Dynamic OCPP EVSE"),
            "manufacturer": "Dynamic OCPP EVSE",
            "model": "Electrical System Hub",
        }

    @property
    def is_on(self):
        """Return true if grid charging is allowed."""
        return self._state

    async def async_turn_on(self, **kwargs):
        """Turn on grid charging."""
        self._state = True
        self.async_write_ha_state()
        _LOGGER.info("Grid charging enabled")

    async def async_turn_off(self, **kwargs):
        """Turn off grid charging."""
        self._state = False
        self.async_write_ha_state()
        _LOGGER.info("Grid charging disabled")

    async def async_added_to_hass(self):
        """Restore previous state when added to hass."""
        await super().async_added_to_hass()
        
        last_state = await self.async_get_last_state()
        if last_state is not None:
            self._state = last_state.state == "on"
            _LOGGER.debug(f"Restored {self._attr_name} to: {self._state}")
        else:
            self._state = True  # Default to enabled
            _LOGGER.debug(f"No state to restore for {self._attr_name}, using default: True")
        
        self.async_write_ha_state()
