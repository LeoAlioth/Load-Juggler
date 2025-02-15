import logging
from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from datetime import timedelta
from .dynamic_ocpp_evse import calculate_available_current
from .const import *

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(seconds=5)

async def async_setup_entry(hass: HomeAssistant, config_entry: ConfigEntry, async_add_entities):
    """Set up the Dynamic OCPP EVSE Sensor from a config entry."""
    name = config_entry.data[CONF_NAME]
    entity_id = config_entry.data[CONF_ENTITY_ID]
    async_add_entities([DynamicOcppEvseSensor(hass, config_entry, name, entity_id)])

class DynamicOcppEvseSensor(SensorEntity):
    """Representation of an Dynamic OCPP EVSE Sensor."""

    def __init__(self, hass, config_entry, name, entity_id):
        """Initialize the sensor."""
        self.hass = hass
        self.config_entry = config_entry
        self._attr_name = name
        self._attr_unique_id = entity_id  # Set a unique ID for the entity
        self._state = None
        self._phases = None
        self._charging_mode = None
        self._calc_used = None
        self._max_evse_available = None
    @property
    def state(self):
        """Return the state of the sensor."""
        return self._state

    @property
    def extra_state_attributes(self):
        """Return the state attributes."""
        return {
            "state_class": "measurement",
            CONF_PHASES: self._phases,
            CONF_CHARING_MODE: self._charging_mode,
            "calc_used": self._calc_used,
            "max_evse_available": self._max_evse_available,
        }

    @property
    def icon(self):
        """Return the icon to use in the frontend."""
        return "mdi:transmission-tower"

    async def async_update(self):
        """Fetch new state data for the sensor asynchronously."""
        try:
            # Fetch all attributes from the calculate_tariff function
            data = calculate_available_current(self)
            self._state = data[CONF_AVAILABLE_CURRENT]
            self._phases = data[CONF_PHASES]
            self._charging_mode = data[CONF_CHARING_MODE]
            self._calc_used = data["calc_used"]
            self._max_evse_available = data["max_evse_available"]
        except Exception as e:
            _LOGGER.error(f"Error updating Dynamic OCPP EVSE Sensor: {e}")
