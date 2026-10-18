import logging
from datetime import timedelta
from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import (
    async_track_point_in_utc_time,
    async_track_state_change_event,
)
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util import dt as dt_util
from .entities.mixins import HubEntityMixin, LoadEntityMixin, InverterEntityMixin
from .const import (
    behaviour_type,
    ENTRY_TYPE, ENTRY_TYPE_HUB, ENTRY_TYPE_LOAD, ENTRY_TYPE_INVERTER,
    CONF_NAME, CONF_ENTITY_ID,
    CONF_DEVICE_TYPE, DEVICE_TYPE_EVSE, DEVICE_TYPE_POWER_STATION,
    CONF_CHARGE_LIMIT_ENTITY_ID, INVERTER_RT_CONTROL_ENABLED,
    INVERTER_RT_SOC_CONTROL_ENABLED,
    DEVICE_TYPE_PLUG, DEVICE_TYPE_HOT_WATER_TANK,
    CONF_PLUG_SWITCH_ENTITY_ID, CONF_CLIMATE_ENTITY_ID,
    CONF_STATION_CHARGE_SPEED_ENTITY_ID, CONF_STATION_RESERVE_ENTITY_ID,
    CONF_MANUAL_OVERRIDE_TIMEOUT, DEFAULT_MANUAL_OVERRIDE_TIMEOUT,
    MIN_MANUAL_OVERRIDE_TIMEOUT,
)
from .control import manual_change
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
        device_type = behaviour_type(config_entry.data.get(CONF_DEVICE_TYPE, DEVICE_TYPE_EVSE))
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

    # Hub-level switches - only if any inverter entry has a battery
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
    """Per-load switch to enable/disable dynamic current control - the user's
    alone: Load Juggler never turns it off or on (2026-10-14).

    When ON (default): the load receives dynamically calculated current,
    unless a manual override runs. When OFF: the load is the user's (a
    charger charges at its configured maximum current, any other load is left
    alone) until the switch is turned back on.

    A manual override is the same hands-off with the switch still on, started
    by someone operating the device outside Load Juggler (control.manual_change
    - a plug switched, a tank's thermostat or a station's charge speed or
    reserve set; a charger has no such signal, so it has no override) and
    lasting the load's Manual override timeout. Its end time is the
    ``resumes_at`` attribute, restored with the state, so a restart neither
    cancels nor extends it. Turning the switch off cancels it; turning it on
    starts plain dynamic control. The engine reads ``dynamic_control`` (on
    and no override), the status ``manual_until`` (the override's end).
    """

    _data_key = "dynamic_control"
    _default = True
    _attr_icon = "mdi:auto-fix"
    _resumes_at = None
    _unsub_resume = None

    def __init__(self, hass, config_entry, entity_id, name):
        self._init_entity(
            hass, config_entry, f"{name} Dynamic Control", f"{entity_id}_dynamic_control"
        )

    @property
    def extra_state_attributes(self):
        return {"resumes_at": self._resumes_at.isoformat()} if self._resumes_at else {}

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        self.async_on_remove(self._cancel_resume)
        last = await self.async_get_last_state()
        until = last and dt_util.parse_datetime(last.attributes.get("resumes_at") or "")
        if until:
            # On with an override, or a pre-release's timed off (the switch
            # was the override then): on, with what is left of it.
            self._attr_is_on = True
            if until > dt_util.utcnow():
                self._override(until)
            else:
                self._apply()
        entry = self.config_entry
        watched = {
            DEVICE_TYPE_PLUG: [get_entry_value(entry, CONF_PLUG_SWITCH_ENTITY_ID, None)],
            DEVICE_TYPE_HOT_WATER_TANK: [entry.data.get(CONF_CLIMATE_ENTITY_ID)],
            DEVICE_TYPE_POWER_STATION: [
                entry.data.get(CONF_STATION_CHARGE_SPEED_ENTITY_ID),
                entry.data.get(CONF_STATION_RESERVE_ENTITY_ID),
            ],
        }.get(behaviour_type(entry.data.get(CONF_DEVICE_TYPE, DEVICE_TYPE_EVSE)), [])
        watched = [e for e in watched if e]
        if watched:
            self.async_on_remove(
                async_track_state_change_event(self.hass, watched, self._device_changed)
            )

    async def async_turn_on(self, **kwargs):
        self._cancel_resume()
        await super().async_turn_on(**kwargs)

    async def async_turn_off(self, **kwargs):
        self._cancel_resume()
        await super().async_turn_off(**kwargs)

    def _set(self, on: bool) -> None:
        self._attr_is_on = on
        self._apply()

    def _apply(self) -> None:
        """Publish the state: Load Juggler controls the load only while on
        with no override."""
        rt = self._runtime()
        rt["manual_until"] = self._resumes_at
        controlled = bool(self.is_on) and not self._resumes_at
        if not controlled:
            # What was asked before the hands-off is no reference after it.
            rt.pop("_commanded", None)
        self._publish(controlled)

    @callback
    def _device_changed(self, event):
        if not self.is_on or self._resumes_at:
            return
        change = manual_change(
            self._runtime(), event.data["old_state"], event.data["new_state"]
        )
        if change:
            self._override()
            _LOGGER.info(
                "%s operated outside Load Juggler (%s %s) - manual until %s",
                self.entity_id, event.data["entity_id"], change, self._resumes_at,
            )

    def _override(self, until=None):
        """Hand the load to the user until ``until`` (now + its timeout)."""
        minutes = max(
            MIN_MANUAL_OVERRIDE_TIMEOUT,
            get_entry_value(
                self.config_entry, CONF_MANUAL_OVERRIDE_TIMEOUT, DEFAULT_MANUAL_OVERRIDE_TIMEOUT
            ),
        )
        self._cancel_resume()
        self._resumes_at = until or dt_util.utcnow() + timedelta(minutes=minutes)
        self._unsub_resume = async_track_point_in_utc_time(
            self.hass, self._resume, self._resumes_at
        )
        self._apply()

    @callback
    def _resume(self, _now):
        self._unsub_resume = None
        self._resumes_at = None
        self._apply()
        _LOGGER.info("%s manual override timed out - back to dynamic control", self.entity_id)

    def _cancel_resume(self):
        if self._unsub_resume:
            self._unsub_resume()
        self._unsub_resume = self._resumes_at = None


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
