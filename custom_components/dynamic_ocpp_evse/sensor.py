import logging
from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from datetime import timedelta, datetime
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
        self._last_update = datetime.min  # Initialize the last update timestamp
        self._pause_timer_running = False  # Track if the pause timer is running
        self._last_set_current = 0

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
            "last_update": self._last_update,
            "pause_timer_running": self._pause_timer_running,
            "last_set_current": self._last_set_current
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

            # Check if the update frequency has passed
            update_frequency = self.config_entry.data[CONF_UPDATE_FREQUENCY]
            if (datetime.utcnow() - self._last_update).total_seconds() >= update_frequency:
                # Check if the state drops below 6
                if self._state < 6 and not self._pause_timer_running:
                    # Start the Charge Pause Timer
                    await self.hass.services.async_call(
                        "timer",
                        "start",
                        {
                            "entity_id": f"timer.{self._attr_unique_id}_charge_pause_timer",
                            "duration": self.config_entry.data[CONF_CHARGE_PAUSE_DURATION]
                        }
                    )
                    self._pause_timer_running = True

                # Check if the timer is running
                timer_state = self.hass.states.get(f"timer.{self._attr_unique_id}_charge_pause_timer")
                if timer_state and timer_state.state == "active":
                    limit = 0
                else:
                    limit = round(self._state, 1)
                    self._pause_timer_running = False
                
                self._last_set_current = limit

                # Prepare the data for the OCPP set_charge_rate service
                valid_from = datetime.utcnow().isoformat(timespec='seconds') + 'Z'
                valid_to = (datetime.utcnow() + timedelta(seconds=40)).isoformat(timespec='seconds') + 'Z'
                charging_profile = {
                    "chargingProfileId": 11,
                    "stackLevel": 4,
                    "chargingProfileKind": "Relative",
                    "chargingProfilePurpose": "TxDefaultProfile",
                    "validFrom": valid_from,
                    "validTo": valid_to,
                    "chargingSchedule": {
                        "chargingRateUnit": "A",
                        "chargingSchedulePeriod": [
                            {
                                "startPeriod": 0,
                                "limit": limit
                            }
                        ]
                    }
                }

                # Log the data being sent
                _LOGGER.debug(f"Sending set_charge_rate with data: {charging_profile}")

                # Call the OCPP set_charge_rate service
                await self.hass.services.async_call(
                    "ocpp",
                    "set_charge_rate",
                    {
                        "custom_profile": charging_profile
                    }
                )
                # Update the last update timestamp
                self._last_update = datetime.utcnow()
        except Exception as e:
            _LOGGER.error(f"Error updating Dynamic OCPP EVSE Sensor: {e}", exc_info=True)
