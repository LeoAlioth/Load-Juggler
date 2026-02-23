import logging
from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from .entity_mixins import HubEntityMixin, ChargerEntityMixin
from .const import (
    DOMAIN, ENTRY_TYPE, ENTRY_TYPE_HUB, ENTRY_TYPE_CHARGER, CONF_NAME, CONF_ENTITY_ID,
    CONF_DEVICE_TYPE, DEVICE_TYPE_EVSE, DEVICE_TYPE_PLUG,
    DISTRIBUTION_MODE_SHARED, DISTRIBUTION_MODE_PRIORITY,
    DISTRIBUTION_MODE_SEQUENTIAL_OPTIMIZED, DISTRIBUTION_MODE_SEQUENTIAL_STRICT,
    DEFAULT_DISTRIBUTION_MODE,
    OPERATING_MODE_STANDARD, OPERATING_MODE_CONTINUOUS,
    OPERATING_MODE_SOLAR_PRIORITY, OPERATING_MODE_SOLAR_ONLY, OPERATING_MODE_EXCESS,
    OPERATING_MODES_EVSE, OPERATING_MODES_PLUG,
    DEFAULT_OPERATING_MODE_EVSE, DEFAULT_OPERATING_MODE_PLUG,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, config_entry: ConfigEntry, async_add_entities: AddEntitiesCallback):
    """Set up the Dynamic OCPP EVSE Select from a config entry."""
    entry_type = config_entry.data.get(ENTRY_TYPE)

    # Hub entries get distribution_mode selector only
    if entry_type == ENTRY_TYPE_HUB:
        name = config_entry.data.get(CONF_NAME, "Site Load Management")
        entity_id = config_entry.data.get(CONF_ENTITY_ID, "site_load_management")

        entities = [
            DynamicOcppEvseDistributionModeSelect(hass, config_entry, name, entity_id)
        ]
        _LOGGER.info(f"Setting up hub select entities: {[entity.unique_id for entity in entities]}")
        async_add_entities(entities)
        return

    # Charger entries get per-charger operating mode selector
    if entry_type == ENTRY_TYPE_CHARGER:
        name = config_entry.data.get(CONF_NAME, "Charger")
        entity_id = config_entry.data.get(CONF_ENTITY_ID, "charger")

        entities = [
            OperatingModeSelect(hass, config_entry, name, entity_id)
        ]
        _LOGGER.info(f"Setting up charger select entities: {[entity.unique_id for entity in entities]}")
        async_add_entities(entities)
        return


class OperatingModeSelect(ChargerEntityMixin, SelectEntity, RestoreEntity):
    """Per-charger operating mode selector (EVSE or Smart Load)."""

    _charger_data_key = "operating_mode"

    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry, name: str, entity_id: str):
        self.hass = hass
        self.config_entry = config_entry
        self._attr_name = f"{name} Operating Mode"
        self._attr_unique_id = f"{entity_id}_operating_mode"

        device_type = config_entry.data.get(CONF_DEVICE_TYPE, DEVICE_TYPE_EVSE)
        if device_type == DEVICE_TYPE_PLUG:
            self._attr_options = list(OPERATING_MODES_PLUG)
            self._attr_current_option = DEFAULT_OPERATING_MODE_PLUG
        else:
            self._attr_options = list(OPERATING_MODES_EVSE)
            self._attr_current_option = DEFAULT_OPERATING_MODE_EVSE

    @property
    def icon(self):
        icons = {
            OPERATING_MODE_STANDARD: "mdi:flash",
            OPERATING_MODE_CONTINUOUS: "mdi:flash",
            OPERATING_MODE_SOLAR_PRIORITY: "mdi:leaf",
            OPERATING_MODE_SOLAR_ONLY: "mdi:solar-power",
            OPERATING_MODE_EXCESS: "mdi:solar-power-variant",
        }
        return icons.get(self._attr_current_option, "mdi:flash")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is not None and last_state.state in self._attr_options:
            self._attr_current_option = last_state.state
        self.async_write_ha_state()
        self._write_to_charger_data(self._attr_current_option)

    async def async_select_option(self, option: str) -> None:
        if option in self._attr_options:
            self._attr_current_option = option
            self.async_write_ha_state()
            self._write_to_charger_data(option)
            _LOGGER.info(f"Operating mode changed to: {option}")
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
