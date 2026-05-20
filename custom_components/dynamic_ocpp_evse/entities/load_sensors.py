import logging
from homeassistant.components.sensor import SensorEntity
from ..const import (
    DOMAIN,
    CONF_CHARGER_L1_PHASE,
    CONF_CHARGER_L2_PHASE,
    CONF_CHARGER_L3_PHASE,
    CONF_CLIMATE_ENTITY_ID,
)
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

            if climate_state is None or climate_state.state in (
                "unknown",
                "unavailable",
            ):
                self._state = "Unavailable"
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
            }
        except Exception as e:
            _LOGGER.error(f"Error updating {self._attr_name}: {e}", exc_info=True)
