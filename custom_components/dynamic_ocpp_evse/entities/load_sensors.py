import logging
from homeassistant.components.sensor import SensorEntity
from ..const import (
    DOMAIN,
    CONF_CHARGER_L1_PHASE,
    CONF_CHARGER_L2_PHASE,
    CONF_CHARGER_L3_PHASE,
    CONF_CLIMATE_ENTITY_ID,
    CONF_PLUG_SWITCH_ENTITY_ID,
    CONF_STATION_CHARGE_SPEED_ENTITY_ID,
    CONF_STATION_BATTERY_LEVEL_ENTITY_ID,
    CONF_STATION_CHARGE_LIMIT_ENTITY_ID,
    CONF_CHARGER_PRIORITY,
    CONF_HUB_ENTRY_ID,
    DEFAULT_CHARGER_PRIORITY,
)
from ..helpers import get_entry_value
from .. import units
from .mixins import ChargerEntityMixin

_LOGGER = logging.getLogger(__name__)


class LoadJugglerAllocatedCurrentSensor(ChargerEntityMixin, SensorEntity):
    """Sensor showing the allocated current for a managed device."""

    def __init__(self, hass, config_entry, hub_entry, name, entity_id):
        """Initialize the allocated current sensor."""
        self.hass = hass
        self.config_entry = config_entry
        self.hub_entry = hub_entry
        self._attr_name = f"{name} Allocated Current"
        self._attr_unique_id = f"{entity_id}_allocated_current"
        self._state = 0.0

    @property
    def state(self):
        return self._state

    @property
    def icon(self):
        return "mdi:current-ac"

    @property
    def unit_of_measurement(self):
        return "A"

    @property
    def device_class(self):
        return "current"

    @property
    def extra_state_attributes(self):
        return {"state_class": "measurement"}

    async def async_update(self):
        """Read allocated current from hass.data (populated by the charger sensor)."""
        try:
            allocations = self.hass.data.get(DOMAIN, {}).get("charger_allocations", {})
            value = allocations.get(self.config_entry.entry_id, 0)
            self._state = round(float(value), 1)
        except Exception as e:
            _LOGGER.error(f"Error updating {self._attr_name}: {e}", exc_info=True)


class LoadJugglerEffectivePrioritySensor(ChargerEntityMixin, SensorEntity):
    """Sensor showing a device's effective priority rank within its hub.

    When power is contended the engine serves loads by mode urgency first, then
    the configured priority number — so a device's real standing can differ
    from the priority it was given (e.g. a Continuous load outranks a
    higher-priority Solar Only load). This sensor reports that resolved rank:
    1 = served first.
    """

    def __init__(self, hass, config_entry, hub_entry, name, entity_id):
        """Initialize the effective priority sensor."""
        self.hass = hass
        self.config_entry = config_entry
        self.hub_entry = hub_entry
        self._attr_name = f"{name} Effective Priority"
        self._attr_unique_id = f"{entity_id}_effective_priority"
        self._state = None
        self._attrs = {}

    @property
    def state(self):
        return self._state

    @property
    def icon(self):
        return "mdi:sort-numeric-ascending"

    @property
    def extra_state_attributes(self):
        return self._attrs

    async def async_update(self):
        """Read the effective priority rank from hass.data (set by the engine)."""
        try:
            ranks = self.hass.data.get(DOMAIN, {}).get("charger_ranks", {})
            self._state = ranks.get(self.config_entry.entry_id)
            self._attrs = {
                "configured_priority": get_entry_value(
                    self.config_entry,
                    CONF_CHARGER_PRIORITY,
                    DEFAULT_CHARGER_PRIORITY,
                ),
                "total_devices": self._ranked_siblings(ranks),
            }
        except Exception as e:
            _LOGGER.error(f"Error updating {self._attr_name}: {e}", exc_info=True)

    def _ranked_siblings(self, ranks: dict) -> int:
        """Count the ranked loads that share this load's hub.

        "charger_ranks" is a flat, domain-wide bucket: it holds every load of
        every hub, and nothing removes a load's key when its config entry is
        deleted. A rank of "2 of N" is only meaningful against the loads the
        engine actually ranked together, so filter at read time rather than
        trusting the bucket's size (issue #40) — stale keys are dropped by the
        config-entry lookup, foreign hubs by the hub id comparison.
        """
        my_hub = self.config_entry.data.get(CONF_HUB_ENTRY_ID)
        total = 0
        for entry_id, rank in ranks.items():
            if rank is None:
                continue
            load_entry = self.hass.config_entries.async_get_entry(entry_id)
            if load_entry is None:
                continue
            if load_entry.data.get(CONF_HUB_ENTRY_ID) != my_hub:
                continue
            total += 1
        return total


class LoadJugglerDeviceStatusSensor(ChargerEntityMixin, SensorEntity):
    """Sensor showing the current status reason for a managed device."""

    def __init__(self, hass, config_entry, hub_entry, name, entity_id):
        """Initialize the charging status sensor."""
        self.hass = hass
        self.config_entry = config_entry
        self.hub_entry = hub_entry
        self._attr_name = f"{name} Charging Status"
        self._attr_unique_id = f"{entity_id}_charging_status"
        self._state = "Unknown"

    @property
    def state(self):
        return self._state

    @property
    def icon(self):
        return "mdi:information-outline"

    async def async_update(self):
        """Read charging status from hass.data (populated by the charger sensor)."""
        try:
            status = self.hass.data.get(DOMAIN, {}).get("charger_status", {})
            self._state = status.get(self.config_entry.entry_id, "Unknown")
        except Exception as e:
            _LOGGER.error(f"Error updating {self._attr_name}: {e}", exc_info=True)


class LoadJugglerPlugStatusSensor(ChargerEntityMixin, SensorEntity):
    """Status sensor for a smart plug — on/off plus error states.

    A plug has no connector to plug a car into, so the EVSE charging-status
    vocabulary ("Unplugged", "Charging", ...) does not apply. This simply
    reflects the controlled switch: ``On`` / ``Off``, or an error state when
    the switch entity is missing or unavailable.
    """

    def __init__(self, hass, config_entry, hub_entry, name, entity_id):
        """Initialize the smart plug status sensor."""
        self.hass = hass
        self.config_entry = config_entry
        self.hub_entry = hub_entry
        self._attr_name = f"{name} Status"
        # Keep the original unique_id suffix so existing installs upgrade in
        # place (the entity is renamed, not duplicated).
        self._attr_unique_id = f"{entity_id}_charging_status"
        self._switch_entity = config_entry.data.get(CONF_PLUG_SWITCH_ENTITY_ID)
        self._state = "Unknown"

    @property
    def state(self):
        return self._state

    @property
    def icon(self):
        return {
            "On": "mdi:power-plug",
            "Off": "mdi:power-plug-off",
        }.get(self._state, "mdi:power-plug-off-outline")

    async def async_update(self):
        """Reflect the smart plug's switch state, surfacing error states."""
        try:
            if not self._switch_entity:
                self._state = "Not Configured"
                return
            switch_state = self.hass.states.get(self._switch_entity)
            if units.is_unavailable(switch_state):
                self._state = "Unavailable"
            elif switch_state.state == "on":
                self._state = "On"
            else:
                self._state = "Off"
        except Exception as e:
            _LOGGER.error(f"Error updating {self._attr_name}: {e}", exc_info=True)


class LoadJugglerStationStatusSensor(ChargerEntityMixin, SensorEntity):
    """Status sensor for a portable power station.

    Shows what Load Juggler last asked of it — ``Charging`` with the resolved
    speed, ``Storm Reserve`` while holding for an outage, ``Full`` once it has
    reached its charge limit, or ``Idle`` when the reserve has been dropped and
    the station is running on (and off) its own battery. Attributes carry the
    resolved reserve so the two knobs are visible in one place.
    """

    def __init__(self, hass, config_entry, hub_entry, name, entity_id):
        self.hass = hass
        self.config_entry = config_entry
        self.hub_entry = hub_entry
        self._attr_name = f"{name} Status"
        # Same unique_id suffix as the other device types, so switching a
        # device's type upgrades the entity in place.
        self._attr_unique_id = f"{entity_id}_charging_status"
        self._state = "Unknown"
        self._attrs = {}

    @property
    def state(self):
        return self._state

    @property
    def extra_state_attributes(self):
        return self._attrs

    @property
    def icon(self):
        return {
            "Charging": "mdi:battery-charging",
            "Storm Reserve": "mdi:weather-lightning",
            "Full": "mdi:battery",
            "Idle": "mdi:battery-arrow-down",
            "Manual": "mdi:hand-back-right",
        }.get(self._state, "mdi:battery-unknown")

    async def async_update(self):
        try:
            charger_rt = (
                self.hass.data.get(DOMAIN, {})
                .get("chargers", {})
                .get(self.config_entry.entry_id, {})
            )
            speed_entity = self.config_entry.data.get(
                CONF_STATION_CHARGE_SPEED_ENTITY_ID
            )
            soc = _read_float(
                self.hass,
                get_entry_value(
                    self.config_entry, CONF_STATION_BATTERY_LEVEL_ENTITY_ID, None
                ),
            )
            charge_limit = _read_float(
                self.hass,
                get_entry_value(
                    self.config_entry, CONF_STATION_CHARGE_LIMIT_ENTITY_ID, None
                ),
            )
            speed_state = (
                self.hass.states.get(speed_entity) if speed_entity else None
            )

            if not speed_entity:
                self._state = "Not Configured"
            elif units.is_unavailable(speed_state):
                # These integrations talk BLE, one connection at a time — the
                # vendor app taking over looks exactly like this.
                self._state = "Unavailable"
            elif not charger_rt.get("dynamic_control", True):
                self._state = "Manual"
            elif charger_rt.get("station_storm_reserve"):
                self._state = "Storm Reserve"
            elif (
                soc is not None
                and charge_limit is not None
                and soc >= charge_limit
            ):
                self._state = "Full"
            elif charger_rt.get("station_charging"):
                self._state = "Charging"
            else:
                self._state = "Idle"

            self._attrs = {
                "operating_mode": charger_rt.get("operating_mode"),
                "battery_level": soc,
                "charge_limit": charge_limit,
                "charge_speed": charger_rt.get("station_charge_speed"),
                "backup_reserve": charger_rt.get("station_reserve"),
                "reserve_source": charger_rt.get("station_reserve_label"),
            }
        except Exception as e:
            _LOGGER.error(f"Error updating {self._attr_name}: {e}", exc_info=True)


def _read_float(hass, entity_id):
    """Current numeric state of ``entity_id``, or None if unusable."""
    if not entity_id:
        return None
    state = hass.states.get(entity_id)
    if units.is_unavailable(state):
        return None
    try:
        value = float(state.state)
    except (TypeError, ValueError):
        return None
    return None if units.is_unusable_number(value) else value


class LoadJugglerPhaseMaskSensor(ChargerEntityMixin, SensorEntity):
    """Sensor showing which site phases a 3-phase EVSE is currently drawing on."""

    def __init__(self, hass, config_entry, hub_entry, name, entity_id):
        """Initialize the phase mask sensor."""
        self.hass = hass
        self.config_entry = config_entry
        self.hub_entry = hub_entry
        self._attr_name = f"{name} Phase Mask"
        self._attr_unique_id = f"{entity_id}_phase_mask"
        self._state = "Idle"
        l1 = config_entry.data.get(CONF_CHARGER_L1_PHASE, "A")
        l2 = config_entry.data.get(CONF_CHARGER_L2_PHASE, "B")
        l3 = config_entry.data.get(CONF_CHARGER_L3_PHASE, "C")
        self._wiring_mask = "".join(sorted({l1, l2, l3}))

    @property
    def state(self):
        return self._state

    @property
    def icon(self):
        return "mdi:sine-wave"

    @property
    def extra_state_attributes(self):
        active = 0 if self._state in ("Idle", "Unknown") else len(self._state)
        return {
            "wiring_phases": self._wiring_mask,
            "active_phase_count": active,
        }

    async def async_update(self):
        """Read the live phase mask from hass.data (populated by the charger sensor)."""
        try:
            masks = self.hass.data.get(DOMAIN, {}).get("charger_phase_masks", {})
            mask = masks.get(self.config_entry.entry_id)
            self._state = mask if mask else "Idle"
        except Exception as e:
            _LOGGER.error(f"Error updating {self._attr_name}: {e}", exc_info=True)


class LoadJugglerTankStatusSensor(ChargerEntityMixin, SensorEntity):
    """Status sensor for a hot water tank — heating state, temp, and setpoint."""

    def __init__(self, hass, config_entry, hub_entry, name, entity_id):
        """Initialize the hot water tank status sensor."""
        self.hass = hass
        self.config_entry = config_entry
        self.hub_entry = hub_entry
        self._attr_name = f"{name} Tank Status"
        self._attr_unique_id = f"{entity_id}_tank_status"
        self._climate_entity = config_entry.data.get(CONF_CLIMATE_ENTITY_ID)
        self._state = "Unknown"
        self._attrs = {}

    @property
    def state(self):
        return self._state

    @property
    def icon(self):
        return "mdi:water-boiler"

    @property
    def extra_state_attributes(self):
        return self._attrs

    async def async_update(self):
        """Derive the tank state from the climate entity + shared charger data."""
        try:
            charger_rt = (
                self.hass.data.get(DOMAIN, {})
                .get("chargers", {})
                .get(self.config_entry.entry_id, {})
            )
            climate_state = (
                self.hass.states.get(self._climate_entity)
                if self._climate_entity
                else None
            )
            hvac_action = (
                climate_state.attributes.get("hvac_action")
                if climate_state
                else None
            )
            current_temp = (
                climate_state.attributes.get("current_temperature")
                if climate_state
                else None
            )

            if units.is_unavailable(climate_state):
                self._state = "Unavailable"
            elif not charger_rt.get("dynamic_control", True):
                # Dynamic Control off — Load Juggler is not managing the tank;
                # the thermostat runs on its own.
                self._state = "Manual"
            elif not charger_rt.get("tank_heating_permitted", True):
                self._state = "Waiting for Power"
            else:
                self._state = {
                    "heating": "Heating",
                    "idle": "Idle",
                    "off": "Off",
                }.get(hvac_action, "Idle")

            self._attrs = {
                "operating_mode": charger_rt.get("operating_mode"),
                "current_temperature": current_temp,
                "target_setpoint": charger_rt.get("tank_setpoint"),
                "setpoint_source": charger_rt.get("tank_setpoint_label"),
                "heating_permitted": charger_rt.get("tank_heating_permitted"),
                "priority_elevated": charger_rt.get("tank_priority_elevated", False),
            }
        except Exception as e:
            _LOGGER.error(f"Error updating {self._attr_name}: {e}", exc_info=True)
