import logging
from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from .entity_mixins import HubEntityMixin
from .const import (
    ENTRY_TYPE, ENTRY_TYPE_HUB, ENTRY_TYPE_CHARGER, CONF_NAME, CONF_ENTITY_ID,
    DISTRIBUTION_MODE_SHARED, DISTRIBUTION_MODE_PRIORITY,
    DISTRIBUTION_MODE_SEQUENTIAL_OPTIMIZED, DISTRIBUTION_MODE_SEQUENTIAL_STRICT,
    CHARGING_MODE_STANDARD, CHARGING_MODE_ECO, CHARGING_MODE_SOLAR, CHARGING_MODE_EXCESS,
    DEFAULT_DISTRIBUTION_MODE
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, config_entry: ConfigEntry, async_add_entities: AddEntitiesCallback):
    """Set up the Dynamic OCPP EVSE Select from a config entry."""
    entry_type = config_entry.data.get(ENTRY_TYPE)

    # Hub entries get both charging_mode and distribution_mode selectors
    if entry_type == ENTRY_TYPE_HUB:
        name = config_entry.data.get(CONF_NAME, "Dynamic OCPP EVSE")
        entity_id = config_entry.data.get(CONF_ENTITY_ID, "dynamic_ocpp_evse")

        entities = [
            DynamicOcppEvseChargingModeSelect(hass, config_entry, name, entity_id),
            DynamicOcppEvseDistributionModeSelect(hass, config_entry, name, entity_id)
        ]
        _LOGGER.info(f"Setting up hub select entities: {[entity.unique_id for entity in entities]}")
        async_add_entities(entities)
        return

    # Charger entries don't get any selectors now (all mode selection is at hub level)
    if entry_type == ENTRY_TYPE_CHARGER:
        _LOGGER.debug("No selector entities for charger entries - all mode selection is at hub level")
        return


class DynamicOcppEvseChargingModeSelect(HubEntityMixin, SelectEntity, RestoreEntity):
    """Representation of a Dynamic OCPP EVSE Charging Mode Select (Hub-level)."""

    _hub_data_key = "charging_mode"

    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry, name: str, entity_id: str):
        self.hass = hass
        self.config_entry = config_entry
        self._attr_name = f"{name} Charging Mode"
        self._attr_unique_id = f"{entity_id}_charging_mode"
        self._attr_options = [CHARGING_MODE_STANDARD, CHARGING_MODE_ECO, CHARGING_MODE_SOLAR, CHARGING_MODE_EXCESS]
        self._attr_current_option = CHARGING_MODE_STANDARD

    @property
    def icon(self):
        icons = {
            CHARGING_MODE_STANDARD: "mdi:flash",
            CHARGING_MODE_ECO: "mdi:leaf",
            CHARGING_MODE_SOLAR: "mdi:solar-power",
            CHARGING_MODE_EXCESS: "mdi:solar-power-variant",
        }
        return icons.get(self._attr_current_option, "mdi:flash")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is not None and last_state.state in self._attr_options:
            self._attr_current_option = last_state.state
        self.async_write_ha_state()
        self._write_to_hub_data(self._attr_current_option)

    async def async_select_option(self, option: str) -> None:
        if option in self._attr_options:
            self._attr_current_option = option
            self.async_write_ha_state()
            self._write_to_hub_data(option)
            _LOGGER.info(f"Charging mode changed to: {option}")
        else:
            _LOGGER.error(f"Invalid option selected: {option}")


class DynamicOcppEvseDistributionModeSelect(HubEntityMixin, SelectEntity, RestoreEntity):
    """Representation of a Dynamic OCPP EVSE Distribution Mode Select (Hub-level)."""

    _hub_data_key = "distribution_mode"

    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry, name: str, entity_id: str):
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
        self._attr_current_option = DEFAULT_DISTRIBUTION_MODE

    @property
    def icon(self):
        icons = {
            DISTRIBUTION_MODE_SHARED: "mdi:share-variant",
            DISTRIBUTION_MODE_PRIORITY: "mdi:format-list-numbered",
            DISTRIBUTION_MODE_SEQUENTIAL_OPTIMIZED: "mdi:arrow-right-circle",
            DISTRIBUTION_MODE_SEQUENTIAL_STRICT: "mdi:arrow-right-bold-circle",
        }
        return icons.get(self._attr_current_option, "mdi:share-variant")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is not None and last_state.state in self._attr_options:
            self._attr_current_option = last_state.state
        self.async_write_ha_state()
        self._write_to_hub_data(self._attr_current_option)

    async def async_select_option(self, option: str) -> None:
        if option in self._attr_options:
            self._attr_current_option = option
            self.async_write_ha_state()
            self._write_to_hub_data(option)
            _LOGGER.info(f"Distribution mode changed to: {option}")
        else:
            _LOGGER.error(f"Invalid distribution mode selected: {option}")
