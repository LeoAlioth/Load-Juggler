import logging
from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from .const import (
    DOMAIN, ENTRY_TYPE, ENTRY_TYPE_HUB, ENTRY_TYPE_CHARGER, CONF_NAME, CONF_ENTITY_ID,
    DISTRIBUTION_MODE_SHARED, DISTRIBUTION_MODE_PRIORITY, 
    DISTRIBUTION_MODE_SEQUENTIAL_OPTIMIZED, DISTRIBUTION_MODE_SEQUENTIAL_STRICT,
    DEFAULT_DISTRIBUTION_MODE
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, config_entry: ConfigEntry, async_add_entities: AddEntitiesCallback):
    """Set up the Dynamic OCPP EVSE Select from a config entry."""
    entry_type = config_entry.data.get(ENTRY_TYPE)
    
    # Set up select entities for charger entries (charging mode per charger)
    if entry_type == ENTRY_TYPE_CHARGER:
        name = config_entry.data.get(CONF_NAME, "Charger")
        entity_id = config_entry.data.get(CONF_ENTITY_ID, "charger")
        
        entities = [DynamicOcppEvseChargingModeSelect(hass, config_entry, name, entity_id)]
        _LOGGER.info(f"Setting up charger select entities: {[entity.unique_id for entity in entities]}")
        async_add_entities(entities)
        return
    
    # Set up select entities for hub entries (distribution mode at hub level)
    if entry_type == ENTRY_TYPE_HUB:
        name = config_entry.data.get(CONF_NAME, "Dynamic OCPP EVSE")
        entity_id = config_entry.data.get(CONF_ENTITY_ID, "dynamic_ocpp_evse")
        
        entities = [DynamicOcppEvseDistributionModeSelect(hass, config_entry, name, entity_id)]
        _LOGGER.info(f"Setting up hub select entities: {[entity.unique_id for entity in entities]}")
        async_add_entities(entities)
        return


class DynamicOcppEvseChargingModeSelect(SelectEntity, RestoreEntity):
    """Representation of a Dynamic OCPP EVSE Charging Mode Select (Charger-level)."""

    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry, name: str, entity_id: str):
        """Initialize the select entity."""
        self.hass = hass
        self.config_entry = config_entry
        self._attr_name = f"{name} Charging Mode"
        self._attr_unique_id = f"{entity_id}_charging_mode"
        self._attr_options = ["Standard", "Eco", "Solar", "Excess"]
        self._attr_current_option = "Standard"  # Default, will be overridden by restore

    @property
    def device_info(self):
        """Return device information about this charger."""
        from . import get_hub_for_charger
        hub_entry = get_hub_for_charger(self.hass, self.config_entry.entry_id)
        return {
            "identifiers": {(DOMAIN, self.config_entry.entry_id)},
            "name": self.config_entry.data.get(CONF_NAME),
            "manufacturer": "Dynamic OCPP EVSE",
            "model": "EV Charger",
            "via_device": (DOMAIN, hub_entry.entry_id) if hub_entry else None,
        }

    @property
    def icon(self):
        """Return the icon based on current mode."""
        icons = {
            "Standard": "mdi:flash",
            "Eco": "mdi:leaf",
            "Solar": "mdi:solar-power",
            "Excess": "mdi:solar-power-variant",
        }
        return icons.get(self._attr_current_option, "mdi:flash")

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
        
        # Write initial state
        self.async_write_ha_state()

    async def async_select_option(self, option: str) -> None:
        """Change the selected option."""
        if option in self._attr_options:
            self._attr_current_option = option
            self.async_write_ha_state()
            _LOGGER.info(f"Charging mode changed to: {option}")
        else:
            _LOGGER.error(f"Invalid option selected: {option}")


class DynamicOcppEvseDistributionModeSelect(SelectEntity, RestoreEntity):
    """Representation of a Dynamic OCPP EVSE Distribution Mode Select (Hub-level)."""

    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry, name: str, entity_id: str):
        """Initialize the select entity."""
        self.hass = hass
        self.config_entry = config_entry
        self._attr_name = f"{name} Distribution Mode"
        self._attr_unique_id = f"{entity_id}_distribution_mode"
        self._attr_options = [
            DISTRIBUTION_MODE_SHARED,
            DISTRIBUTION_MODE_PRIORITY,
            DISTRIBUTION_MODE_SEQUENTIAL_OPTIMIZED,
            DISTRIBUTION_MODE_SEQUENTIAL_STRICT
        ]
        self._attr_current_option = DEFAULT_DISTRIBUTION_MODE  # Default, will be overridden by restore

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
    def icon(self):
        """Return the icon based on current mode."""
        icons = {
            DISTRIBUTION_MODE_SHARED: "mdi:share-variant",
            DISTRIBUTION_MODE_PRIORITY: "mdi:format-list-numbered",
            DISTRIBUTION_MODE_SEQUENTIAL_OPTIMIZED: "mdi:arrow-right-circle",
            DISTRIBUTION_MODE_SEQUENTIAL_STRICT: "mdi:arrow-right-bold-circle",
        }
        return icons.get(self._attr_current_option, "mdi:share-variant")

    async def async_added_to_hass(self) -> None:
        """Restore last state when added to hass."""
        await super().async_added_to_hass()
        
        # Try to restore the last state
        last_state = await self.async_get_last_state()
        if last_state is not None and last_state.state in self._attr_options:
            self._attr_current_option = last_state.state
            _LOGGER.debug(f"Restored distribution mode to: {self._attr_current_option}")
        else:
            _LOGGER.debug(f"No valid state to restore, using default: {self._attr_current_option}")
        
        # Write initial state
        self.async_write_ha_state()

    async def async_select_option(self, option: str) -> None:
        """Change the selected option."""
        if option in self._attr_options:
            self._attr_current_option = option
            self.async_write_ha_state()
            _LOGGER.info(f"Distribution mode changed to: {option}")
        else:
            _LOGGER.error(f"Invalid distribution mode selected: {option}")
