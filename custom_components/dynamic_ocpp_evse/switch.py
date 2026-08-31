import logging
from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from .entities.mixins import HubEntityMixin, LoadEntityMixin, InverterEntityMixin
from .const import (
    ENTRY_TYPE, ENTRY_TYPE_HUB, ENTRY_TYPE_LOAD, ENTRY_TYPE_INVERTER,
    CONF_NAME, CONF_ENTITY_ID,
    CONF_HUB_ENTRY_ID, CONF_BATTERY_SOC_ENTITY_ID, CONF_BATTERY_POWER_ENTITY_ID,
    CONF_DEVICE_TYPE, DEVICE_TYPE_EVSE, DEVICE_TYPE_POWER_STATION,
    CONF_CHARGE_LIMIT_ENTITY_ID, INVERTER_RT_CONTROL_ENABLED,
    INVERTER_RT_SOC_CONTROL_ENABLED,
)
from .control.inverter import soc_targets
from .helpers import get_entry_value, hub_has_battery

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, config_entry: ConfigEntry, async_add_entities: AddEntitiesCallback):
    """Set up switch entities."""
    entry_type = config_entry.data.get(ENTRY_TYPE)

    if entry_type == ENTRY_TYPE_LOAD:
        entity_id = config_entry.data.get(CONF_ENTITY_ID, "load")
        name = config_entry.data.get(CONF_NAME, "Load")
        hub_entry_id = config_entry.data.get(CONF_HUB_ENTRY_ID)
        hub_entry = hass.config_entries.async_get_entry(hub_entry_id) if hub_entry_id else None
        entities = [DynamicControlSwitch(hass, config_entry, hub_entry, entity_id, name)]
        device_type = config_entry.data.get(CONF_DEVICE_TYPE, DEVICE_TYPE_EVSE)
        if device_type == DEVICE_TYPE_POWER_STATION:
            entities.append(
                StationStormReserveSwitch(
                    hass, config_entry, hub_entry, entity_id, name
                )
            )
        async_add_entities(entities)
        return

    if entry_type == ENTRY_TYPE_INVERTER:
        # Write-control opt-ins, one per control and each gated on its own
        # target being configured — with nothing to write to, a switch would be
        # a lie. The two are independent: an inverter may expose a charge-current
        # register, TOU SOC slots, both, or neither.
        #
        # No name is passed to either: both are named off the device via
        # has_entity_name + a translation key, so HA composes the displayed name
        # from device_info's name rather than it being baked in here.
        entity_id = config_entry.data.get(CONF_ENTITY_ID, "inverter")
        entities = []
        if get_entry_value(config_entry, CONF_CHARGE_LIMIT_ENTITY_ID, None):
            entities.append(
                BatteryChargeControlSwitch(hass, config_entry, entity_id)
            )
        if soc_targets(config_entry):
            entities.append(
                BatterySocControlSwitch(hass, config_entry, entity_id)
            )
        if not entities:
            _LOGGER.debug(
                "No write-control targets on %s - skipping its control switches",
                config_entry.title,
            )
            return
        async_add_entities(entities)
        return

    if entry_type != ENTRY_TYPE_HUB:
        _LOGGER.debug("Skipping switch setup for unknown entry type: %s", config_entry.title)
        return

    # Hub-level switches — only if any fleet battery is configured
    # (the hub's legacy fields or an inverter entry)
    has_battery = hub_has_battery(hass, config_entry)

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


class DynamicControlSwitch(LoadEntityMixin, SwitchEntity, RestoreEntity):
    """Per-load switch to enable/disable dynamic current control.

    When ON (default): the load receives dynamically calculated current.
    When OFF: the load charges at its configured maximum current.
    """

    _attr_entity_category = EntityCategory.CONFIG
    _load_data_key = "dynamic_control"

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
        self._write_to_load_data(True)
        _LOGGER.info("Dynamic control enabled for %s", self._attr_name)

    async def async_turn_off(self, **kwargs):
        self._state = False
        self.async_write_ha_state()
        self._write_to_load_data(False)
        _LOGGER.info("Dynamic control disabled for %s — load will use max current", self._attr_name)

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is not None:
            self._state = last_state.state == "on"
        else:
            self._state = True
        self.async_write_ha_state()
        self._write_to_load_data(self._state)


class StationStormReserveSwitch(LoadEntityMixin, SwitchEntity, RestoreEntity):
    """Per-station switch to hold a storm reserve.

    When ON: the station holds its storm reserve level, charging from whatever
    source is available and refusing to discharge below it. That overrides the
    operating mode — a backup reserve that may only be filled from surplus is
    not a reserve — so the engine treats the station as a must-run load for as
    long as this is on.

    When OFF: the station returns to its operating mode and its normal reserve.
    """

    _attr_entity_category = EntityCategory.CONFIG
    _load_data_key = "station_storm_reserve"

    def __init__(self, hass, config_entry, hub_entry, entity_id, name):
        self.hass = hass
        self.config_entry = config_entry
        self.hub_entry = hub_entry
        self._attr_name = f"{name} Storm Reserve"
        self._attr_unique_id = f"{entity_id}_station_storm_reserve"
        self._state = False
        self._attr_icon = "mdi:weather-lightning"

    @property
    def is_on(self):
        return self._state

    async def async_turn_on(self, **kwargs):
        self._state = True
        self.async_write_ha_state()
        self._write_to_load_data(True)
        _LOGGER.info(
            "Storm reserve enabled for %s — charging from any source and holding",
            self._attr_name,
        )

    async def async_turn_off(self, **kwargs):
        self._state = False
        self.async_write_ha_state()
        self._write_to_load_data(False)
        _LOGGER.info("Storm reserve disabled for %s", self._attr_name)

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        # Default off: a storm reserve should be a deliberate act, and it is the
        # one state that lets the station charge from the grid at full rate.
        self._state = last_state is not None and last_state.state == "on"
        self.async_write_ha_state()
        self._write_to_load_data(self._state)


class BatteryChargeControlSwitch(InverterEntityMixin, SwitchEntity, RestoreEntity):
    """Per-inverter opt-in for writing the forecast's charge limit.

    OFF (the default): the clipping forecast stays advisory — the sensors show
    what it recommends and nothing is written to the inverter. ON: the
    recommended charge limit is written to the configured register, and the
    normal value is restored once the advice releases.

    Default off on purpose. This is the only entity in the integration whose
    'on' state makes Load Juggler write to a third-party device's Modbus
    registers, so arming it should be a deliberate act — including after a
    restore with no previous state.
    """

    _attr_entity_category = EntityCategory.CONFIG
    _inverter_data_key = INVERTER_RT_CONTROL_ENABLED
    # Named off the device, so renaming the inverter renames this too, and the
    # entity half of that name comes from the translations (entity.switch.
    # battery_charge_control.name) so the Slovenian UI names it the same way its
    # help text does. unique_id is unaffected either way.
    _attr_has_entity_name = True
    _attr_translation_key = "battery_charge_control"

    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry, entity_id: str):
        self.hass = hass
        self.config_entry = config_entry
        self._attr_unique_id = f"{entity_id}_battery_charge_control"
        self._state = False
        self._attr_icon = "mdi:battery-clock"

    @property
    def is_on(self):
        return self._state

    async def async_turn_on(self, **kwargs):
        self._state = True
        self.async_write_ha_state()
        self._write_to_inverter_data(True)
        _LOGGER.info(
            "Battery charge control enabled for %s — the PV clipping forecast "
            "will now write %s",
            self.config_entry.title,
            get_entry_value(self.config_entry, CONF_CHARGE_LIMIT_ENTITY_ID, None),
        )

    async def async_turn_off(self, **kwargs):
        self._state = False
        self.async_write_ha_state()
        self._write_to_inverter_data(False)
        # The control loop sees the disabled flag on its next tick and puts
        # the normal value back — no write from here, so the pacing and the
        # write-once-on-release logic stay in one place.
        _LOGGER.info(
            "Battery charge control disabled for %s — restoring its normal "
            "charge limit",
            self.config_entry.title,
        )

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        self._state = last_state is not None and last_state.state == "on"
        self.async_write_ha_state()
        self._write_to_inverter_data(self._state)


class BatterySocControlSwitch(InverterEntityMixin, SwitchEntity, RestoreEntity):
    """Per-inverter opt-in for writing the forecast's battery SOC ceiling.

    OFF (the default): the recommended max SOC stays advisory — the sensor shows
    it and none of the configured time-of-use slots is touched. ON: every
    configured slot is driven to the lower of the forecast's recommendation and
    the normal ceiling, and rises back with the recommendation on its own.

    A switch of its own rather than a second meaning for Battery Charge Control.
    The two controls write different things at different strengths — a rate limit
    slows the fill, a SOC ceiling stops it dead — and an inverter may support
    either without the other, so a site that wants only the gentler one must be
    able to say exactly that.

    Default off, for the same reason as its sibling: 'on' makes Load Juggler
    write to a third-party device, here to several of its entities at once, so
    arming it should be a deliberate act — including after a restore with no
    previous state.
    """

    _attr_entity_category = EntityCategory.CONFIG
    _inverter_data_key = INVERTER_RT_SOC_CONTROL_ENABLED
    # Named off the device, so renaming the inverter renames this too, and the
    # entity half of that name comes from the translations (entity.switch.
    # battery_soc_control.name) so the Slovenian UI names it the same way its
    # help text does. unique_id is unaffected either way.
    _attr_has_entity_name = True
    _attr_translation_key = "battery_soc_control"

    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry, entity_id: str):
        self.hass = hass
        self.config_entry = config_entry
        self._attr_unique_id = f"{entity_id}_battery_soc_control"
        self._state = False
        self._attr_icon = "mdi:battery-lock"

    @property
    def is_on(self):
        return self._state

    async def async_turn_on(self, **kwargs):
        self._state = True
        self.async_write_ha_state()
        self._write_to_inverter_data(True)
        _LOGGER.info(
            "Battery SOC control enabled for %s — the PV clipping forecast will "
            "now write %s",
            self.config_entry.title,
            ", ".join(soc_targets(self.config_entry)),
        )

    async def async_turn_off(self, **kwargs):
        self._state = False
        self.async_write_ha_state()
        self._write_to_inverter_data(False)
        # No restore write from here, and none from the control loop either: the
        # slots keep whatever ceiling they currently hold, which is either their
        # owner's value or a limit that will simply stop being maintained.
        # Turning this off stops writing; it does not undo history.
        _LOGGER.info(
            "Battery SOC control disabled for %s — its SOC slots are left as they "
            "stand",
            self.config_entry.title,
        )

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        self._state = last_state is not None and last_state.state == "on"
        self.async_write_ha_state()
        self._write_to_inverter_data(self._state)
