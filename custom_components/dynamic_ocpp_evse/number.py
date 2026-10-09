# filepath: custom_components/dynamic_ocpp_evse/number.py
import logging
from functools import partial

from homeassistant.components.number import NumberEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.restore_state import RestoreEntity
from .entities.mixins import HubEntityMixin, LoadEntityMixin
from .const import (
    CONF_CLIMATE_ENTITY_ID,
    ENTRY_TYPE,
    ENTRY_TYPE_HUB,
    ENTRY_TYPE_LOAD,
    CONF_NAME,
    CONF_ENTITY_ID,
    CONF_EVSE_MINIMUM_CHARGE_CURRENT,
    CONF_EVSE_MAXIMUM_CHARGE_CURRENT,
    CONF_PLUG_POWER_RATING,
    DEFAULT_PLUG_POWER_RATING,
    CONF_HEATING_ELEMENT_POWER,
    DEFAULT_HEATING_ELEMENT_POWER,
    DEFAULT_MIN_CHARGE_CURRENT,
    DEFAULT_MAX_CHARGE_CURRENT,
    DEFAULT_BATTERY_SOC_MIN,
    DEFAULT_BATTERY_SOC_TARGET,
    CONF_BATTERY_SOC_FULL,
    DEFAULT_BATTERY_SOC_FULL,
    CONF_DEVICE_TYPE,
    DEVICE_TYPE_EVSE,
    DEVICE_TYPE_PLUG,
    DEVICE_TYPE_HOT_WATER_TANK,
    DEVICE_TYPE_POWER_STATION,
    CONF_STATION_MIN_CHARGE_POWER,
    CONF_STATION_MAX_CHARGE_POWER,
    CONF_STATION_NORMAL_RESERVE,
    CONF_STATION_STORM_RESERVE,
    DEFAULT_STATION_MIN_CHARGE_POWER,
    DEFAULT_STATION_MAX_CHARGE_POWER,
    DEFAULT_STATION_NORMAL_RESERVE,
    DEFAULT_STATION_STORM_RESERVE,
    STATION_CHARGE_POWER_MAX,
    STATION_CHARGE_POWER_STEP,
    CONF_TANK_AWAY_TEMPERATURE,
    CONF_TANK_NORMAL_TEMPERATURE,
    CONF_TANK_BOOST_TEMPERATURE,
    DEFAULT_TANK_AWAY_TEMPERATURE,
    DEFAULT_TANK_NORMAL_TEMPERATURE,
    DEFAULT_TANK_BOOST_TEMPERATURE,
    CONF_ENABLE_MAX_IMPORT_POWER,
    CONF_MAX_IMPORT_POWER_ENTITY_ID,
    CONF_MAIN_BREAKER_RATING,
    CONF_PHASE_VOLTAGE,
    CONF_PHASE_B_CURRENT_ENTITY_ID,
    CONF_PHASE_C_CURRENT_ENTITY_ID,
    DEFAULT_MAIN_BREAKER_RATING,
    DEFAULT_PHASE_VOLTAGE,
)
from .helpers import get_entry_value, get_inverters_for_hub, hub_has_battery

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, config_entry: ConfigEntry, async_add_entities: AddEntitiesCallback):
    """Set up the number entities."""
    entry_type = config_entry.data.get(ENTRY_TYPE)
    name = config_entry.data.get(CONF_NAME, "Site Load Management")
    entity_id = config_entry.data.get(CONF_ENTITY_ID, "site_load_management")
    conf = partial(get_entry_value, config_entry)

    # One row per slider: class, name suffix, unique_id suffix, runtime key,
    # min, max, step, starting value, unit, icon.
    if entry_type == ENTRY_TYPE_HUB:
        # Always create Power Buffer (useful even without battery)
        rows = [
            (HubSlider, "Power Buffer", "power_buffer", "power_buffer", 0, 5000, 100, 0, "W", "mdi:buffer"),
        ]

        # Max Import Power slider: created when its checkbox is ticked AND no
        # override sensor is set - with a sensor the slider would be a dead
        # control, since the sensor takes precedence over it (hub form help
        # text; engine/hub_calculation._read_max_import_power). The checkbox
        # only ever decides the slider. It is not a switch for the limit:
        # 565a0bf treated it as one and a site driving its limit from a
        # sensor lost the limit for a week.
        max_import_entity = conf(CONF_MAX_IMPORT_POWER_ENTITY_ID, None)
        if max_import_entity:
            _LOGGER.info("Max import power from override sensor %s", max_import_entity)
        elif conf(CONF_ENABLE_MAX_IMPORT_POWER, True):
            # Starts at the full breaker capacity
            phases = 1 + bool(conf(CONF_PHASE_B_CURRENT_ENTITY_ID, None)) + bool(conf(CONF_PHASE_C_CURRENT_ENTITY_ID, None))
            breaker_power = round(
                conf(CONF_PHASE_VOLTAGE, DEFAULT_PHASE_VOLTAGE)
                * conf(CONF_MAIN_BREAKER_RATING, DEFAULT_MAIN_BREAKER_RATING)
                * phases / 100
            ) * 100
            rows.append(
                (HubSlider, "Max Import Power", "max_import_power", "max_import_power", 0, 50000, 100, breaker_power, "W", "mdi:transmission-tower-import"),
            )
            _LOGGER.info("Max import power slider created (no override sensor)")
        else:
            _LOGGER.info("Max import power: no sensor and no slider - unlimited")

        # Only create battery entities if any battery is on the fleet (hub
        # legacy fields or an inverter entry).
        #   SOC Target - Eco: below it charge at minimum rate, at/above it at
        #     solar rate or full speed. Solar: below it do not charge.
        #   SOC Min - below it no load charges in any mode: the absolute floor
        #     that protects the home battery.
        #   SOC Full - at/above it the battery counts as full (Excess plugs
        #     run, no charge allowance); starts at the inverter setting it
        #     replaced.
        if hub_has_battery(hass, config_entry):
            full = next(
                (
                    v for v in (
                        get_entry_value(e, CONF_BATTERY_SOC_FULL, None)
                        for e in (config_entry, *get_inverters_for_hub(hass, config_entry.entry_id))
                    ) if v
                ),
                DEFAULT_BATTERY_SOC_FULL,
            )
            rows += [
                (HubSlider, "Home Battery SOC Full", "home_battery_soc_full", "battery_soc_full", 50, 100, 1, full, "%", "mdi:battery-high"),
                (HubSlider, "Home Battery SOC Target", "home_battery_soc_target", "battery_soc_target", 0, 100, 1, DEFAULT_BATTERY_SOC_TARGET, "%", "mdi:battery-charging-80"),
                (HubSlider, "Home Battery SOC Min", "home_battery_soc_min", "battery_soc_min", 0, 95, 1, DEFAULT_BATTERY_SOC_MIN, "%", "mdi:battery-alert-variant-outline"),
            ]
            _LOGGER.info(f"Battery configured - creating battery number entities")
        else:
            _LOGGER.info(f"No battery configured - skipping battery number entities")

    elif entry_type == ENTRY_TYPE_LOAD:
        device_type = config_entry.data.get(CONF_DEVICE_TYPE, DEVICE_TYPE_EVSE)
        if device_type == DEVICE_TYPE_PLUG:
            power = conf(CONF_PLUG_POWER_RATING, DEFAULT_PLUG_POWER_RATING)
            rows = [
                (LoadPowerSlider, "Device Power", "device_power", "device_power", 10, max(power, 30000), 10, power, "W", "mdi:flash"),
            ]
        elif device_type == DEVICE_TYPE_HOT_WATER_TANK:
            power = conf(CONF_HEATING_ELEMENT_POWER, DEFAULT_HEATING_ELEMENT_POWER)
            rows = [
                (TankTemperatureSlider, "Away Temperature", "tank_away_temperature", "tank_away_temperature", 10, 90, 1, conf(CONF_TANK_AWAY_TEMPERATURE, DEFAULT_TANK_AWAY_TEMPERATURE), "°C", "mdi:thermometer-water"),
                (TankTemperatureSlider, "Normal Temperature", "tank_normal_temperature", "tank_normal_temperature", 10, 90, 1, conf(CONF_TANK_NORMAL_TEMPERATURE, DEFAULT_TANK_NORMAL_TEMPERATURE), "°C", "mdi:thermometer-water"),
                (TankTemperatureSlider, "Boost Temperature", "tank_boost_temperature", "tank_boost_temperature", 10, 90, 1, conf(CONF_TANK_BOOST_TEMPERATURE, DEFAULT_TANK_BOOST_TEMPERATURE), "°C", "mdi:thermometer-water"),
                (LoadPowerSlider, "Element Power", "device_power", "device_power", 10, max(power, 30000), 10, power, "W", "mdi:flash"),
            ]
        elif device_type == DEVICE_TYPE_POWER_STATION:
            # The charge-power bounds hold what the engine may allocate,
            # deliberately configured rather than read from the device - a
            # station whose hardware accepts 2400 W can be held to less. The
            # reserves are the station's on/off gate: below its battery level it
            # stops drawing from the wall and serves its own loads; the normal
            # one is what it falls back to with nothing to absorb.
            rows = [
                (LoadSlider, "Minimum Charge Power", "station_min_charge_power", "station_min_charge_power", 0, STATION_CHARGE_POWER_MAX, STATION_CHARGE_POWER_STEP, conf(CONF_STATION_MIN_CHARGE_POWER, DEFAULT_STATION_MIN_CHARGE_POWER), "W", "mdi:lightning-bolt-outline"),
                (LoadSlider, "Maximum Charge Power", "station_max_charge_power", "station_max_charge_power", 0, STATION_CHARGE_POWER_MAX, STATION_CHARGE_POWER_STEP, conf(CONF_STATION_MAX_CHARGE_POWER, DEFAULT_STATION_MAX_CHARGE_POWER), "W", "mdi:lightning-bolt-outline"),
                (LoadSlider, "Normal Reserve", "station_normal_reserve", "station_normal_reserve", 0, 100, 1, conf(CONF_STATION_NORMAL_RESERVE, DEFAULT_STATION_NORMAL_RESERVE), "%", "mdi:battery-charging-30"),
                (LoadSlider, "Storm Reserve", "station_storm_reserve", "station_storm_reserve_level", 0, 100, 1, conf(CONF_STATION_STORM_RESERVE, DEFAULT_STATION_STORM_RESERVE), "%", "mdi:battery-charging-30"),
            ]
        else:
            low = conf(CONF_EVSE_MINIMUM_CHARGE_CURRENT, DEFAULT_MIN_CHARGE_CURRENT)
            high = conf(CONF_EVSE_MAXIMUM_CHARGE_CURRENT, DEFAULT_MAX_CHARGE_CURRENT)
            rows = [
                (EVSECurrentSlider, "Min Current", "min_current", "min_current", low, high, 0.5, low, "A", "mdi:current-ac"),
                (EVSECurrentSlider, "Max Current", "max_current", "max_current", low, high, 0.5, high, "A", "mdi:current-ac"),
            ]

    else:
        _LOGGER.debug("Skipping number setup for unknown entry type: %s", config_entry.title)
        return

    entities = [
        cls(hass, config_entry, f"{name} {label}", f"{entity_id}_{uid}", *rest)
        for cls, label, uid, *rest in rows
    ]
    _LOGGER.info(f"Setting up {entry_type} number entities: {[entity.unique_id for entity in entities]}")
    async_add_entities(entities)


class _ConfigSlider(NumberEntity, RestoreEntity):
    """A CONFIG slider whose value the engine reads back from the runtime dict.

    Restored on start (clamped into its range), and a set is rounded to the
    step and clamped: HA's number.set_value already rejects an out-of-range
    value, but rounding can still step past an end that is not on the step.
    """

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, hass, config_entry, name, unique_id, data_key, low, high, step, value, unit, icon):
        self._init_entity(hass, config_entry, name, unique_id)
        self._data_key = data_key
        self._attr_native_min_value = low
        self._attr_native_max_value = high
        self._attr_native_step = step
        self._attr_native_value = value
        self._attr_native_unit_of_measurement = unit
        self._attr_icon = icon

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        await self._restore_and_publish_number()

    def _on_step(self, value: float) -> float:
        """``value`` rounded to the step, inside the range."""
        step = self._attr_native_step
        return max(
            self._attr_native_min_value,
            min(self._attr_native_max_value, round(value / step) * step),
        )

    async def async_set_native_value(self, value: float) -> None:
        self._attr_native_value = self._on_step(value)
        self._publish(self._attr_native_value)


class HubSlider(HubEntityMixin, _ConfigSlider):
    """A hub-level slider (hass.data[DOMAIN]["hubs"][entry_id])."""


class LoadSlider(LoadEntityMixin, _ConfigSlider):
    """A load-level slider (hass.data[DOMAIN]["loads"][entry_id])."""


class EVSECurrentSlider(LoadSlider):
    """The paired EVSE Min/Max Current sliders.

    The two sliders bound the same interval from opposite ends, and the engine
    trusts that interval: ``min_current > max_current`` makes every permit
    nonsensical (allocation floors above its own ceiling). Neither slider can
    see the other's HA state cheaply, but both publish into the same
    hass.data[DOMAIN]["loads"][entry_id] bucket the engine reads, so that
    bucket is the cross-check (issue #38).

    Behavior on a crossing set: the value being SET is clamped to the sibling's
    current value; the sibling is never moved. Rationale - the alternative
    (pushing the sibling along) silently rewrites a second entity the user did
    not touch, and would let one drag reconfigure the whole range. Clamping is
    also what the widget already does at the native_min/native_max ends, so the
    slider simply refuses to travel past its partner.
    """

    async def async_set_native_value(self, value: float) -> None:
        value = self._on_step(value)
        # The min slider's sibling bounds it from above, the max slider's from below.
        upper = self._data_key == "min_current"
        sibling_key = "max_current" if upper else "min_current"
        sibling = self._runtime().get(sibling_key)
        if isinstance(sibling, (int, float)) and (value > sibling if upper else value < sibling):
            _LOGGER.info(
                "%s: %.1fA would %s %s (%.1fA) - clamped to %.1fA",
                self._attr_name,
                value,
                "exceed" if upper else "fall below",
                sibling_key,
                sibling,
                sibling,
            )
            value = sibling
        self._attr_native_value = value
        self._publish(value)


class LoadPowerSlider(LoadSlider):
    """Slider for a binary load's power rating in Watts (smart plug or tank).

    Holds the load's set power. When a power-measurement entity is configured
    the engine overwrites ``device_power`` with the live measured draw each
    cycle, so this slider both seeds and then displays the device's real power.
    A managed load can be anything from a small pump to a 3-phase heater, so
    the range is wide and the step fine - the value is mostly auto-learned from
    the power-measurement entity anyway.
    """

    async def async_update(self) -> None:
        """Reflect the value the engine learned from the power-measurement entity."""
        learned = self._runtime().get("device_power")
        if learned is not None:
            self._attr_native_value = learned


class TankTemperatureSlider(LoadSlider):
    """Slider for a hot water tank setpoint temperature (away / normal / boost).

    The only place these are set: the setup and settings pages no longer ask
    (Anze, 2026-09-25) - an entry from before keeps its saved value as the
    starting one. Bounded by the thermostat's own min_temp / max_temp, which
    both climate and water_heater entities publish, and moved into them when
    they change; 10-90 °C until the thermostat reports. Load Juggler itself
    lowers one, through its set service, to a target the device does not keep
    (control/hot_water_tank._adopt_kept_target).
    """

    def __init__(self, hass, config_entry, *args):
        super().__init__(hass, config_entry, *args)
        self._thermostat = config_entry.data.get(CONF_CLIMATE_ENTITY_ID)

    def _sync_to_thermostat(self) -> bool:
        """Take the thermostat's range; True when anything shown changed."""
        state = self.hass.states.get(self._thermostat) if self._thermostat else None
        try:
            low = float(state.attributes["min_temp"])
            high = float(state.attributes["max_temp"])
        except (AttributeError, KeyError, TypeError, ValueError):
            return False
        if low > high:
            return False
        before = (self._attr_native_min_value, self._attr_native_max_value, self._attr_native_value)
        self._attr_native_min_value, self._attr_native_max_value = low, high
        if self._attr_native_value is not None:
            self._attr_native_value = min(max(float(self._attr_native_value), low), high)
        return before != (low, high, self._attr_native_value)

    async def async_added_to_hass(self) -> None:
        # Before the restore, so a restored value is clamped into this range.
        self._sync_to_thermostat()
        await super().async_added_to_hass()
        if self._thermostat:
            self.async_on_remove(
                async_track_state_change_event(
                    self.hass, [self._thermostat], self._thermostat_changed
                )
            )

    @callback
    def _thermostat_changed(self, _event) -> None:
        if self._sync_to_thermostat():
            self._publish(self._attr_native_value)
