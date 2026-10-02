import logging
from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from .entities.mixins import HubEntityMixin, LoadEntityMixin
from . import consume_plug_mode_migration
from .const import (
    ENTRY_TYPE, ENTRY_TYPE_HUB, ENTRY_TYPE_LOAD, CONF_NAME, CONF_ENTITY_ID,
    CONF_DEVICE_TYPE, DEVICE_TYPE_EVSE, DEVICE_TYPE_HOT_WATER_TANK,
    DISTRIBUTION_MODE_SHARED, DISTRIBUTION_MODE_PRIORITY,
    DISTRIBUTION_MODE_SEQUENTIAL_OPTIMIZED, DISTRIBUTION_MODE_SEQUENTIAL_STRICT,
    DEFAULT_DISTRIBUTION_MODE, DISTRIBUTION_MODES,
    modes_for,
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

    # Load entries get per-load operating mode selector
    if entry_type == ENTRY_TYPE_LOAD:
        name = config_entry.data.get(CONF_NAME, "Load")
        entity_id = config_entry.data.get(CONF_ENTITY_ID, "load")

        entities = [
            OperatingModeSelect(hass, config_entry, name, entity_id)
        ]
        _LOGGER.info(f"Setting up load select entities: {[entity.unique_id for entity in entities]}")
        async_add_entities(entities)
        return


class OperatingModeSelect(LoadEntityMixin, SelectEntity, RestoreEntity):
    """Per-load operating mode selector (EVSE / Smart Load / Hot Water Tank).

    Each device type has its own independent list of OperatingMode objects;
    the select exposes their keys as options.
    """

    _data_key = "operating_mode"

    # Mode keys renamed across versions - a restored value is migrated before
    # use so existing installs keep a valid selection.
    _RENAMED_MODE_KEYS = {
        DEVICE_TYPE_HOT_WATER_TANK: {"Solar Only": "Solar Priority"},
    }

    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry, name: str, entity_id: str):
        self._init_entity(hass, config_entry, f"{name} Operating Mode", f"{entity_id}_operating_mode")

        self._device_type = config_entry.data.get(CONF_DEVICE_TYPE, DEVICE_TYPE_EVSE)
        modes, default = modes_for(self._device_type)
        self._modes = modes
        self._attr_options = [m.key for m in modes]
        self._attr_current_option = default.key

    @property
    def icon(self):
        icons = {m.key: m.icon for m in self._modes}
        return icons.get(self._attr_current_option, "mdi:flash")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # One-time plug migration (2.2 → 2.3): the old "Solar Only" plug mode
        # was renamed to "Solar Priority"; the key "Solar Only" now denotes a
        # different, target-gated mode. async_migrate_entry flags the entry and
        # async_setup_entry turns that into a one-shot runtime marker, claimed
        # here. The claim MUST NOT write to the config entry - that fires the
        # update listener and reloads an entry that may still be
        # SETUP_IN_PROGRESS (issue #34); async_setup_entry clears the persisted
        # flag itself, at a point where no update listener is registered.
        migrate_plug_solar_only = consume_plug_mode_migration(
            self.hass, self.config_entry.entry_id
        )
        last_state = await self.async_get_last_state()
        if last_state is not None:
            restored = last_state.state
            if migrate_plug_solar_only and restored == "Solar Only":
                restored = "Solar Priority"
            # Tank: "Solar Only" was renamed to "Solar Priority" (no key reuse,
            # so this remap is unconditional and safe).
            restored = self._RENAMED_MODE_KEYS.get(self._device_type, {}).get(
                restored, restored
            )
            if restored in self._attr_options:
                self._attr_current_option = restored
        self._publish(self._attr_current_option)

    async def async_select_option(self, option: str) -> None:
        # HA's select.select_option has already rejected an unknown option.
        self._attr_current_option = option
        self._publish(option)
        _LOGGER.info(f"Operating mode changed to: {option}")


class LoadJugglerDistributionModeSelect(HubEntityMixin, SelectEntity, RestoreEntity):
    """Representation of a Load Juggler Distribution Mode Select (Hub-level)."""

    _data_key = "distribution_mode"

    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry, name: str, entity_id: str):
        self._init_entity(hass, config_entry, f"{name} Distribution Mode", f"{entity_id}_distribution_mode")
        self._attr_options = DISTRIBUTION_MODES
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
        self._publish(self._attr_current_option)

    async def async_select_option(self, option: str) -> None:
        # HA's select.select_option has already rejected an unknown option.
        self._attr_current_option = option
        self._publish(option)
        _LOGGER.info(f"Distribution mode changed to: {option}")
