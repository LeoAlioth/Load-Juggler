import logging
from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from datetime import timedelta, datetime
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from .dynamic_ocpp_evse import calculate_available_current
from .const import *

_LOGGER = logging.getLogger(__name__)
SCAN_INTERVAL = timedelta(seconds=10)

async def async_setup_entry(hass: HomeAssistant, config_entry: ConfigEntry, async_add_entities):
    """Set up the Dynamic OCPP EVSE Sensor from a config entry."""
    name = config_entry.data[CONF_NAME]
    entity_id = config_entry.data[CONF_ENTITY_ID]

    # Fetch the initial update frequency from the configuration
    update_frequency = config_entry.data.get(CONF_UPDATE_FREQUENCY, 10)  # Default to 5 seconds if not set
    _LOGGER.info(f"Initial update frequency: {update_frequency} seconds")

    async def async_update_data():
        """Fetch data for the coordinator."""
        # Create a temporary sensor instance to calculate the data
        temp_sensor = DynamicOcppEvseSensor(hass, config_entry, name, entity_id, None)
        await temp_sensor.async_update()
        return {
            CONF_AVAILABLE_CURRENT: temp_sensor._state,
            CONF_PHASES: temp_sensor._phases,
            CONF_CHARING_MODE: temp_sensor._charging_mode,
            "calc_used": temp_sensor._calc_used,
            "max_evse_available": temp_sensor._max_evse_available,
        }

    # Create a DataUpdateCoordinator to manage the update interval dynamically
    coordinator = DataUpdateCoordinator(
        hass,
        _LOGGER,
        name="Dynamic OCPP EVSE Coordinator",
        update_method=async_update_data,
        update_interval=timedelta(seconds=update_frequency),
    )

    # Create the sensor entity
    sensor = DynamicOcppEvseSensor(hass, config_entry, name, entity_id, coordinator)
    async_add_entities([sensor])

    # Start the first update
    await coordinator.async_config_entry_first_refresh()

    # Listen for updates to the config entry and recreate the coordinator if necessary
    async def async_update_listener(hass: HomeAssistant, entry: ConfigEntry):
        """Handle options update."""
        nonlocal update_frequency  # Declare nonlocal before using the variable
        _LOGGER.debug("async_update_listener triggered")
        new_update_frequency = entry.data.get(CONF_UPDATE_FREQUENCY, 5)
        _LOGGER.info(f"Detected update frequency change: {new_update_frequency} seconds")
        if new_update_frequency != update_frequency:
            _LOGGER.info(f"Updating update_frequency to {new_update_frequency} seconds")
            # Recreate the coordinator with the new update frequency
            nonlocal coordinator  # Update the outer coordinator variable
            coordinator = DataUpdateCoordinator(
                hass,
                _LOGGER,
                name="Dynamic OCPP EVSE Coordinator",
                update_method=async_update_data,
                update_interval=timedelta(seconds=new_update_frequency),
            )
            update_frequency = new_update_frequency  # Update the variable
            _LOGGER.debug(f"Recreated DataUpdateCoordinator with update_interval: {new_update_frequency} seconds")
            await coordinator.async_config_entry_first_refresh()
            sensor.coordinator = coordinator

    # Register the listener for config entry updates
    _LOGGER.debug("Registering async_on_update listener")
    config_entry.async_on_unload(config_entry.add_update_listener(async_update_listener))


class DynamicOcppEvseSensor(SensorEntity):
    """Representation of a Dynamic OCPP EVSE Sensor."""

    def __init__(self, hass, config_entry, name, entity_id, coordinator):
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
        self.coordinator = coordinator

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
            # Fetch all attributes from the calculate_available_current function
            data = calculate_available_current(self)
            self._state = data[CONF_AVAILABLE_CURRENT]
            self._phases = data[CONF_PHASES]
            self._charging_mode = data[CONF_CHARING_MODE]
            self._calc_used = data["calc_used"]
            self._max_evse_available = data["max_evse_available"]

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
            profile_timeout = self.config_entry.data.get(CONF_OCPP_PROFILE_TIMEOUT, 15)  # Default to 15 seconds if not set
            valid_from = datetime.utcnow().isoformat(timespec='seconds') + 'Z'
            valid_to = (datetime.utcnow() + timedelta(seconds=profile_timeout)).isoformat(timespec='seconds') + 'Z'
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
