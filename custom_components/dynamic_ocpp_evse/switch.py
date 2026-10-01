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
        entities = [DynamicControlSwitch(hass, config_entry, entity_id, name)]
        device_type = config_entry.data.get(CONF_DEVICE_TYPE, DEVICE_TYPE_EVSE)
        if device_type == DEVICE_TYPE_POWER_STATION:
            entities.append(
                StationStormReserveSwitch(hass, config_entry, entity_id, name)
            )
        async_add_entities(entities)
        return

    if entry_type == ENTRY_TYPE_INVERTER:
        # Write-control opt-ins, one per control and each gated on its own
        # target being configured - with nothing to write to, a switch would be
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

    # Hub-level switches - only if any fleet battery is configured
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


class _FlagSwitch(SwitchEntity, RestoreEntity):
    """An on/off CONFIG flag the engine reads back from the runtime dict.

    Restored from the last state, or ``_default`` when there is none.
    """

    _attr_entity_category = EntityCategory.CONFIG
    _default = False

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        self._set(self._default if last_state is None else last_state.state == "on")

    async def async_turn_on(self, **kwargs):
        self._set(True)
        _LOGGER.info("%s turned on", self.entity_id)

    async def async_turn_off(self, **kwargs):
        self._set(False)
        _LOGGER.info("%s turned off", self.entity_id)

    def _set(self, on: bool) -> None:
        self._attr_is_on = on
        self._publish(on)


class AllowGridChargingSwitch(HubEntityMixin, _FlagSwitch):
    """Switch to allow/disallow grid charging (hub-level)."""

    _data_key = "allow_grid_charging"
    _default = True
    _attr_icon = "mdi:transmission-tower"

    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry, entity_id: str, name: str):
        self._init_entity(
            hass, config_entry, f"{name} Allow Grid Charging", f"{entity_id}_allow_grid_charging"
        )


class DynamicControlSwitch(LoadEntityMixin, _FlagSwitch):
    """Per-load switch to enable/disable dynamic current control.

    When ON (default): the load receives dynamically calculated current.
    When OFF: the load charges at its configured maximum current.
    """

    _data_key = "dynamic_control"
    _default = True
    _attr_icon = "mdi:auto-fix"

    def __init__(self, hass, config_entry, entity_id, name):
        self._init_entity(
            hass, config_entry, f"{name} Dynamic Control", f"{entity_id}_dynamic_control"
        )


class StationStormReserveSwitch(LoadEntityMixin, _FlagSwitch):
    """Per-station switch to hold a storm reserve.

    When ON: the station holds its storm reserve level, charging from whatever
    source is available and refusing to discharge below it. That overrides the
    operating mode - a backup reserve that may only be filled from surplus is
    not a reserve - so the engine treats the station as a must-run load for as
    long as this is on.

    When OFF: the station returns to its operating mode and its normal reserve.

    Default off: a storm reserve should be a deliberate act, and it is the one
    state that lets the station charge from the grid at full rate.
    """

    _data_key = "station_storm_reserve"
    _attr_icon = "mdi:weather-lightning"

    def __init__(self, hass, config_entry, entity_id, name):
        self._init_entity(
            hass, config_entry, f"{name} Storm Reserve", f"{entity_id}_station_storm_reserve"
        )


class BatteryChargeControlSwitch(InverterEntityMixin, _FlagSwitch):
    """Per-inverter opt-in for writing the forecast's charge limit.

    OFF (the default): the clipping forecast stays advisory - the sensors show
    what it recommends and nothing is written to the inverter. ON: the
    recommended charge limit is written to the configured register, and the
    normal value is restored once the advice releases. Turning it off writes
    nothing from here: the control loop sees the disabled flag on its next tick
    and puts the normal value back, so the pacing and the write-once-on-release
    logic stay in one place.

    Default off on purpose. This is the only entity in the integration whose
    'on' state makes Load Juggler write to a third-party device's Modbus
    registers, so arming it should be a deliberate act - including after a
    restore with no previous state.
    """

    _data_key = INVERTER_RT_CONTROL_ENABLED
    _attr_icon = "mdi:battery-clock"
    # Named off the device, so renaming the inverter renames this too, and the
    # entity half of that name comes from the translations (entity.switch.
    # battery_charge_control.name) so the Slovenian UI names it the same way its
    # help text does. unique_id is unaffected either way.
    _attr_has_entity_name = True
    _attr_translation_key = "battery_charge_control"

    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry, entity_id: str):
        self._init_entity(hass, config_entry, None, f"{entity_id}_battery_charge_control")


class BatterySocControlSwitch(InverterEntityMixin, _FlagSwitch):
    """Per-inverter opt-in for writing the forecast's battery SOC ceiling.

    OFF (the default): the recommended max SOC stays advisory - the sensor shows
    it and none of the configured time-of-use slots is touched. ON: every
    configured slot is driven to the lower of the forecast's recommendation and
    the normal ceiling, and rises back with the recommendation on its own.
    Turning it off writes nothing, here or in the control loop: the slots keep
    whatever ceiling they currently hold. It stops writing; it does not undo
    history.

    A switch of its own rather than a second meaning for Battery Charge Control.
    The two controls write different things at different strengths - a rate limit
    slows the fill, a SOC ceiling stops it dead - and an inverter may support
    either without the other, so a site that wants only the gentler one must be
    able to say exactly that.

    Default off, for the same reason as its sibling: 'on' makes Load Juggler
    write to a third-party device, here to several of its entities at once, so
    arming it should be a deliberate act - including after a restore with no
    previous state.
    """

    _data_key = INVERTER_RT_SOC_CONTROL_ENABLED
    _attr_icon = "mdi:battery-lock"
    # Named off the device - see BatteryChargeControlSwitch (entity.switch.
    # battery_soc_control.name).
    _attr_has_entity_name = True
    _attr_translation_key = "battery_soc_control"

    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry, entity_id: str):
        self._init_entity(hass, config_entry, None, f"{entity_id}_battery_soc_control")
