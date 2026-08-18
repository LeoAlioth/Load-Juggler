"""Per-load diagnostic sensors.

Every class here is a pure reader of what the hub's site cycle published (its
own load's slice of it), so they share one shape: no polling, pushed by the
hub coordinator, and available only while that publication is fresh — see
mixins.SiteCycleConsumerMixin.

Only the allocated-current sensor is a measurement. The status sensors publish
text, and the effective-priority sensor publishes an ordinal rank; neither
carries a device_class or a state_class, both of which HA validates against a
numeric measured quantity.
"""

import logging
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
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
from .mixins import ChargerEntityMixin, SiteCycleConsumerMixin

_LOGGER = logging.getLogger(__name__)


class LoadJugglerLoadSensor(SiteCycleConsumerMixin, ChargerEntityMixin, SensorEntity):
    """Base for the per-load diagnostic sensors.

    Holds the constructor the seven of them shared verbatim. ``hub_entry`` is
    kept on the instance because ChargerEntityMixin's device_info prefers it
    over a registry lookup.
    """

    def __init__(self, hass, config_entry, hub_entry, name, unique_id):
        self._init_entity(hass, config_entry, name, unique_id)
        self.hub_entry = hub_entry


class LoadJugglerAllocatedCurrentSensor(LoadJugglerLoadSensor):
    """Sensor showing the allocated current for a managed device."""

    _attr_native_unit_of_measurement = "A"
    _attr_device_class = SensorDeviceClass.CURRENT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:current-ac"

    def __init__(self, hass, config_entry, hub_entry, name, entity_id):
        """Initialize the allocated current sensor."""
        super().__init__(
            hass,
            config_entry,
            hub_entry,
            f"{name} Allocated Current",
            f"{entity_id}_allocated_current",
        )
        self._attr_native_value = 0.0

    def _read_site_data(self):
        """Read allocated current from hass.data (populated by the load processor)."""
        allocations = self._domain_bucket("charger_allocations")
        value = allocations.get(self.config_entry.entry_id, 0)
        self._attr_native_value = round(float(value), 1)


class LoadJugglerEffectivePrioritySensor(LoadJugglerLoadSensor):
    """Sensor showing a device's effective priority rank within its hub.

    When power is contended the engine serves loads by mode urgency first, then
    the configured priority number — so a device's real standing can differ
    from the priority it was given (e.g. a Continuous load outranks a
    higher-priority Solar Only load). This sensor reports that resolved rank:
    1 = served first.

    Numeric, but deliberately NOT a measurement: the value is an ordinal rank
    within a set that changes size, so long-term statistics over it would
    average positions in a queue. No state_class, no device_class, no unit.
    """

    _attr_icon = "mdi:sort-numeric-ascending"

    def __init__(self, hass, config_entry, hub_entry, name, entity_id):
        """Initialize the effective priority sensor."""
        super().__init__(
            hass,
            config_entry,
            hub_entry,
            f"{name} Effective Priority",
            f"{entity_id}_effective_priority",
        )
        self._attr_native_value = None
        self._attrs = {}

    @property
    def extra_state_attributes(self):
        return self._attrs

    def _read_site_data(self):
        """Read the effective priority rank from hass.data (set by the engine)."""
        ranks = self._domain_bucket("charger_ranks")
        self._attr_native_value = ranks.get(self.config_entry.entry_id)
        self._attrs = {
            "configured_priority": get_entry_value(
                self.config_entry,
                CONF_CHARGER_PRIORITY,
                DEFAULT_CHARGER_PRIORITY,
            ),
            "total_devices": self._ranked_siblings(ranks),
        }

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


class LoadJugglerDeviceStatusSensor(LoadJugglerLoadSensor):
    """Sensor showing the current status reason for a managed device."""

    _attr_icon = "mdi:information-outline"

    def __init__(self, hass, config_entry, hub_entry, name, entity_id):
        """Initialize the charging status sensor."""
        super().__init__(
            hass,
            config_entry,
            hub_entry,
            f"{name} Charging Status",
            f"{entity_id}_charging_status",
        )
        self._attr_native_value = "Unknown"

    def _read_site_data(self):
        """Read charging status from hass.data (populated by the load processor)."""
        status = self._domain_bucket("charger_status")
        self._attr_native_value = status.get(self.config_entry.entry_id, "Unknown")


class LoadJugglerPlugStatusSensor(LoadJugglerLoadSensor):
    """Status sensor for a smart plug — on/off plus error states.

    A plug has no connector to plug a car into, so the EVSE charging-status
    vocabulary ("Unplugged", "Charging", ...) does not apply. This simply
    reflects the controlled switch: ``On`` / ``Off``, or an error state when
    the switch entity is missing or unavailable.
    """

    _PLUG_ICONS = {
        "On": "mdi:power-plug",
        "Off": "mdi:power-plug-off",
    }

    def __init__(self, hass, config_entry, hub_entry, name, entity_id):
        """Initialize the smart plug status sensor."""
        super().__init__(
            hass,
            config_entry,
            hub_entry,
            f"{name} Status",
            # Keep the original unique_id suffix so existing installs upgrade
            # in place (the entity is renamed, not duplicated).
            f"{entity_id}_charging_status",
        )
        self._switch_entity = config_entry.data.get(CONF_PLUG_SWITCH_ENTITY_ID)
        self._attr_native_value = "Unknown"
        self._attr_icon = "mdi:power-plug-off-outline"

    def _read_site_data(self):
        """Reflect the smart plug's switch state, surfacing error states."""
        if not self._switch_entity:
            self._attr_native_value = "Not Configured"
        else:
            switch_state = self.hass.states.get(self._switch_entity)
            if units.is_unavailable(switch_state):
                self._attr_native_value = "Unavailable"
            elif switch_state.state == "on":
                self._attr_native_value = "On"
            else:
                self._attr_native_value = "Off"
        self._attr_icon = self._PLUG_ICONS.get(
            self._attr_native_value, "mdi:power-plug-off-outline"
        )


class LoadJugglerStationStatusSensor(LoadJugglerLoadSensor):
    """Status sensor for a portable power station.

    Shows what Load Juggler last asked of it — ``Charging`` with the resolved
    speed, ``Storm Reserve`` while holding for an outage, ``Full`` once it has
    reached its charge limit, or ``Idle`` when the reserve has been dropped and
    the station is running on (and off) its own battery. Attributes carry the
    resolved reserve so the two knobs are visible in one place.
    """

    _STATION_ICONS = {
        "Charging": "mdi:battery-charging",
        "Storm Reserve": "mdi:weather-lightning",
        "Full": "mdi:battery",
        "Idle": "mdi:battery-arrow-down",
        "Manual": "mdi:hand-back-right",
    }

    def __init__(self, hass, config_entry, hub_entry, name, entity_id):
        super().__init__(
            hass,
            config_entry,
            hub_entry,
            f"{name} Status",
            # Same unique_id suffix as the other device types, so switching a
            # device's type upgrades the entity in place.
            f"{entity_id}_charging_status",
        )
        self._attr_native_value = "Unknown"
        self._attr_icon = "mdi:battery-unknown"
        self._attrs = {}

    @property
    def extra_state_attributes(self):
        return self._attrs

    def _read_site_data(self):
        charger_rt = self._charger_runtime()
        speed_entity = self.config_entry.data.get(CONF_STATION_CHARGE_SPEED_ENTITY_ID)
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
        speed_state = self.hass.states.get(speed_entity) if speed_entity else None

        if not speed_entity:
            self._attr_native_value = "Not Configured"
        elif units.is_unavailable(speed_state):
            # These integrations talk BLE, one connection at a time — the
            # vendor app taking over looks exactly like this.
            self._attr_native_value = "Unavailable"
        elif not charger_rt.get("dynamic_control", True):
            self._attr_native_value = "Manual"
        elif charger_rt.get("station_storm_reserve"):
            self._attr_native_value = "Storm Reserve"
        elif soc is not None and charge_limit is not None and soc >= charge_limit:
            self._attr_native_value = "Full"
        elif charger_rt.get("station_charging"):
            self._attr_native_value = "Charging"
        else:
            self._attr_native_value = "Idle"

        self._attr_icon = self._STATION_ICONS.get(
            self._attr_native_value, "mdi:battery-unknown"
        )
        self._attrs = {
            "operating_mode": charger_rt.get("operating_mode"),
            "battery_level": soc,
            "charge_limit": charge_limit,
            "charge_speed": charger_rt.get("station_charge_speed"),
            "backup_reserve": charger_rt.get("station_reserve"),
            "reserve_source": charger_rt.get("station_reserve_label"),
        }


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


class LoadJugglerPhaseMaskSensor(LoadJugglerLoadSensor):
    """Sensor showing which site phases a 3-phase EVSE is currently drawing on."""

    _attr_icon = "mdi:sine-wave"

    def __init__(self, hass, config_entry, hub_entry, name, entity_id):
        """Initialize the phase mask sensor."""
        super().__init__(
            hass,
            config_entry,
            hub_entry,
            f"{name} Phase Mask",
            f"{entity_id}_phase_mask",
        )
        self._attr_native_value = "Idle"
        l1 = config_entry.data.get(CONF_CHARGER_L1_PHASE, "A")
        l2 = config_entry.data.get(CONF_CHARGER_L2_PHASE, "B")
        l3 = config_entry.data.get(CONF_CHARGER_L3_PHASE, "C")
        self._wiring_mask = "".join(sorted({l1, l2, l3}))

    @property
    def extra_state_attributes(self):
        mask = self._attr_native_value
        active = 0 if mask in ("Idle", "Unknown") else len(mask)
        return {
            "wiring_phases": self._wiring_mask,
            "active_phase_count": active,
        }

    def _read_site_data(self):
        """Read the live phase mask from hass.data (populated by the load processor)."""
        masks = self._domain_bucket("charger_phase_masks")
        mask = masks.get(self.config_entry.entry_id)
        self._attr_native_value = mask if mask else "Idle"


class LoadJugglerTankStatusSensor(LoadJugglerLoadSensor):
    """Status sensor for a hot water tank — heating state, temp, and setpoint."""

    _attr_icon = "mdi:water-boiler"

    def __init__(self, hass, config_entry, hub_entry, name, entity_id):
        """Initialize the hot water tank status sensor."""
        super().__init__(
            hass,
            config_entry,
            hub_entry,
            f"{name} Tank Status",
            f"{entity_id}_tank_status",
        )
        self._climate_entity = config_entry.data.get(CONF_CLIMATE_ENTITY_ID)
        self._attr_native_value = "Unknown"
        self._attrs = {}

    @property
    def extra_state_attributes(self):
        return self._attrs

    def _read_site_data(self):
        """Derive the tank state from the climate entity + shared charger data."""
        charger_rt = self._charger_runtime()
        climate_state = (
            self.hass.states.get(self._climate_entity)
            if self._climate_entity
            else None
        )
        hvac_action = (
            climate_state.attributes.get("hvac_action") if climate_state else None
        )
        current_temp = (
            climate_state.attributes.get("current_temperature")
            if climate_state
            else None
        )

        if units.is_unavailable(climate_state):
            self._attr_native_value = "Unavailable"
        elif not charger_rt.get("dynamic_control", True):
            # Dynamic Control off — Load Juggler is not managing the tank;
            # the thermostat runs on its own.
            self._attr_native_value = "Manual"
        elif not charger_rt.get("tank_heating_permitted", True):
            self._attr_native_value = "Waiting for Power"
        else:
            self._attr_native_value = {
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
