import logging
from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from .entity_mixins import HubEntityMixin, ChargerEntityMixin
from .const import ENTRY_TYPE, ENTRY_TYPE_HUB, ENTRY_TYPE_CHARGER, CONF_NAME, CONF_ENTITY_ID, CONF_HUB_ENTRY_ID, CONF_BATTERY_SOC_ENTITY_ID, CONF_BATTERY_POWER_ENTITY_ID
from .helpers import get_entry_value

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, config_entry: ConfigEntry, async_add_entities: AddEntitiesCallback):
    """Set up switch entities."""
    entry_type = config_entry.data.get(ENTRY_TYPE)

    if entry_type == ENTRY_TYPE_CHARGER:
        entity_id = config_entry.data.get(CONF_ENTITY_ID, "charger")
        name = config_entry.data.get(CONF_NAME, "Charger")
        hub_entry_id = config_entry.data.get(CONF_HUB_ENTRY_ID)
        hub_entry = hass.config_entries.async_get_entry(hub_entry_id) if hub_entry_id else None
        entities = [DynamicControlSwitch(hass, config_entry, hub_entry, entity_id, name)]
        async_add_entities(entities)
        return

    if entry_type != ENTRY_TYPE_HUB:
        _LOGGER.debug("Skipping switch setup for unknown entry type: %s", config_entry.title)
        return

    # Hub-level switches — only if battery is configured
    battery_soc_entity = get_entry_value(config_entry, CONF_BATTERY_SOC_ENTITY_ID)
    battery_power_entity = get_entry_value(config_entry, CONF_BATTERY_POWER_ENTITY_ID)
    has_battery = bool(battery_soc_entity or battery_power_entity)

    if not has_battery:
        _LOGGER.info("No battery configured - skipping 'Allow Grid Charging' switch")
        return

    entity_id = config_entry.data.get(CONF_ENTITY_ID, "site_load_management")
    name = config_entry.data.get(CONF_NAME, "Site Load Management")

    entities = [AllowGridChargingSwitch(hass, config_entry, entity_id, name)]
    _LOGGER.info(f"Setting up hub switch entities: {[entity.unique_id for entity in entities]}")
    async_add_entities(entities)


class AllowGridChargingSwitch(HubEntityMixin, SwitchEntity, RestoreEntity):
    """Switch to allow/disallow grid charging (hub-level)."""

    _attr_entity_category = EntityCategory.CONFIG
    _hub_data_key = "allow_grid_charging"

    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry, entity_id: str, name: str):
        self.hass = hass
        self.config_entry = config_entry
        self._attr_name = f"{name} Allow Grid Charging"
        self._attr_unique_id = f"{entity_id}_allow_grid_charging"
        self._state = True
        self._attr_icon = "mdi:transmission-tower"

    @property
    def is_on(self):
        return self._state

    async def async_turn_on(self, **kwargs):
        self._state = True
        self.async_write_ha_state()
        self._write_to_hub_data(True)
        _LOGGER.info("Grid charging enabled")

    async def async_turn_off(self, **kwargs):
        self._state = False
        self.async_write_ha_state()
        self._write_to_hub_data(False)
        _LOGGER.info("Grid charging disabled")

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is not None:
            self._state = last_state.state == "on"
        else:
            self._state = True
        self.async_write_ha_state()
        self._write_to_hub_data(self._state)


class DynamicControlSwitch(ChargerEntityMixin, SwitchEntity, RestoreEntity):
    """Per-charger switch to enable/disable dynamic current control.

    When ON (default): the charger receives dynamically calculated current.
    When OFF: the charger charges at its configured maximum current.
    """

    _attr_entity_category = EntityCategory.CONFIG
    _charger_data_key = "dynamic_control"

    def __init__(self, hass, config_entry, hub_entry, entity_id, name):
        self.hass = hass
        self.config_entry = config_entry
        self.hub_entry = hub_entry
        self._attr_name = f"{name} Dynamic Control"
        self._attr_unique_id = f"{entity_id}_dynamic_control"
        self._state = True
        self._attr_icon = "mdi:auto-fix"

    @property
    def is_on(self):
        return self._state

    async def async_turn_on(self, **kwargs):
        self._state = True
        self.async_write_ha_state()
        self._write_to_charger_data(True)
        _LOGGER.info("Dynamic control enabled for %s", self._attr_name)

    async def async_turn_off(self, **kwargs):
        self._state = False
        self.async_write_ha_state()
        self._write_to_charger_data(False)
        _LOGGER.info("Dynamic control disabled for %s — charger will use max current", self._attr_name)

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is not None:
            self._state = last_state.state == "on"
        else:
            self._state = True
        self.async_write_ha_state()
        self._write_to_charger_data(self._state)
