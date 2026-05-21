import logging
from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from .entities.mixins import HubEntityMixin, ChargerEntityMixin
from .const import (
    DOMAIN, ENTRY_TYPE, ENTRY_TYPE_HUB, ENTRY_TYPE_CHARGER, CONF_NAME, CONF_ENTITY_ID,
    CONF_DEVICE_TYPE, DEVICE_TYPE_EVSE, DEVICE_TYPE_PLUG, DEVICE_TYPE_HOT_WATER_TANK,
    DISTRIBUTION_MODE_SHARED, DISTRIBUTION_MODE_PRIORITY,
    DISTRIBUTION_MODE_SEQUENTIAL_OPTIMIZED, DISTRIBUTION_MODE_SEQUENTIAL_STRICT,
    DEFAULT_DISTRIBUTION_MODE,
    OPERATING_MODES_EVSE, OPERATING_MODES_PLUG, OPERATING_MODES_HOT_WATER_TANK,
    DEFAULT_OPERATING_MODE_EVSE, DEFAULT_OPERATING_MODE_PLUG,
    DEFAULT_OPERATING_MODE_HOT_WATER_TANK,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, config_entry: ConfigEntry, async_add_entities: AddEntitiesCallback):
    """Set up the Load Juggler Select from a config entry."""
    entry_type = config_entry.data.get(ENTRY_TYPE)

    # Hub entries get distribution_mode selector only
    if entry_type == ENTRY_TYPE_HUB:
        name = config_entry.data.get(CONF_NAME, "Site Load Management")
        entity_id = config_entry.data.get(CONF_ENTITY_ID, "site_load_management")

        entities = [
            LoadJugglerDistributionModeSelect(hass, config_entry, name, entity_id)
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
    """Per-charger operating mode selector (EVSE / Smart Load / Hot Water Tank).

    Each device type has its own independent list of OperatingMode objects;
    the select exposes their keys as options.
    """

    _charger_data_key = "operating_mode"

    # Mode keys renamed across versions — a restored value is migrated before
    # use so existing installs keep a valid selection.
    _RENAMED_MODE_KEYS = {
        DEVICE_TYPE_HOT_WATER_TANK: {"Solar Only": "Solar Priority"},
    }

    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry, name: str, entity_id: str):
        self.hass = hass
        self.config_entry = config_entry
        self._attr_name = f"{name} Operating Mode"
        self._attr_unique_id = f"{entity_id}_operating_mode"

        self._device_type = config_entry.data.get(CONF_DEVICE_TYPE, DEVICE_TYPE_EVSE)
        if self._device_type == DEVICE_TYPE_PLUG:
            modes, default = OPERATING_MODES_PLUG, DEFAULT_OPERATING_MODE_PLUG
        elif self._device_type == DEVICE_TYPE_HOT_WATER_TANK:
            modes, default = OPERATING_MODES_HOT_WATER_TANK, DEFAULT_OPERATING_MODE_HOT_WATER_TANK
        else:
            modes, default = OPERATING_MODES_EVSE, DEFAULT_OPERATING_MODE_EVSE
        self._modes = modes
        self._attr_options = [m.key for m in modes]
        self._attr_current_option = default.key

    @property
    def icon(self):
        icons = {m.key: m.icon for m in self._modes}
        return icons.get(self._attr_current_option, "mdi:flash")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is not None:
            restored = self._RENAMED_MODE_KEYS.get(self._device_type, {}).get(
                last_state.state, last_state.state
            )
            if restored in self._attr_options:
                self._attr_current_option = restored
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


class LoadJugglerDistributionModeSelect(HubEntityMixin, SelectEntity, RestoreEntity):
    """Representation of a Load Juggler Distribution Mode Select (Hub-level)."""

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
