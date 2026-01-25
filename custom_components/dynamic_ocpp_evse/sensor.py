import logging
from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from datetime import timedelta, datetime
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from .dynamic_ocpp_evse import calculate_available_current_for_hub
from .const import *
from . import get_hub_for_charger, distribute_current_to_chargers, get_charger_allocation

_LOGGER = logging.getLogger(__name__)
SCAN_INTERVAL = timedelta(seconds=10)


async def async_setup_entry(hass: HomeAssistant, config_entry: ConfigEntry, async_add_entities):
    """Set up the Dynamic OCPP EVSE Sensor from a config entry."""
    # Only set up sensors for charger entries
    entry_type = config_entry.data.get(ENTRY_TYPE)
    if entry_type != ENTRY_TYPE_CHARGER:
        _LOGGER.debug("Skipping sensor setup for non-charger entry: %s", config_entry.title)
        return
    
    name = config_entry.data[CONF_NAME]
    entity_id = config_entry.data[CONF_ENTITY_ID]
    charger_entry_id = config_entry.entry_id

    # Get the hub entry for this charger
    hub_entry = get_hub_for_charger(hass, charger_entry_id)
    if not hub_entry:
        _LOGGER.error("No hub found for charger: %s", name)
        return

    # Fetch the initial update frequency from the configuration
    update_frequency = config_entry.data.get(CONF_UPDATE_FREQUENCY, DEFAULT_UPDATE_FREQUENCY)
    _LOGGER.info(f"Initial update frequency for {name}: {update_frequency} seconds")

    async def async_update_data():
        """Fetch data for the coordinator."""
        # Create a temporary sensor instance to calculate the data
        temp_sensor = DynamicOcppEvseChargerSensor(hass, config_entry, hub_entry, name, entity_id, None)
        await temp_sensor.async_update()
        return {
            CONF_AVAILABLE_CURRENT: temp_sensor._state,
            CONF_PHASES: temp_sensor._phases,
            CONF_CHARING_MODE: temp_sensor._charging_mode,
            "calc_used": temp_sensor._calc_used,
            "max_evse_available": temp_sensor._max_evse_available,
            "allocated_current": temp_sensor._allocated_current,
        }

    # Create a DataUpdateCoordinator to manage the update interval dynamically
    coordinator = DataUpdateCoordinator(
        hass,
        _LOGGER,
        name=f"Dynamic OCPP EVSE Coordinator - {name}",
        update_method=async_update_data,
        update_interval=timedelta(seconds=update_frequency),
    )

    # Create the sensor entity
    sensor = DynamicOcppEvseChargerSensor(hass, config_entry, hub_entry, name, entity_id, coordinator)
    async_add_entities([sensor])

    # Start the first update
    await coordinator.async_config_entry_first_refresh()

    # Listen for updates to the config entry and recreate the coordinator if necessary
    async def async_update_listener(hass: HomeAssistant, entry: ConfigEntry):
        """Handle options update."""
        nonlocal update_frequency
        _LOGGER.debug("async_update_listener triggered for %s", name)
        new_update_frequency = entry.data.get(CONF_UPDATE_FREQUENCY, DEFAULT_UPDATE_FREQUENCY)
        _LOGGER.info(f"Detected update frequency change for {name}: {new_update_frequency} seconds")
        if new_update_frequency != update_frequency:
            _LOGGER.info(f"Updating update_frequency to {new_update_frequency} seconds")
            nonlocal coordinator
            coordinator = DataUpdateCoordinator(
                hass,
                _LOGGER,
                name=f"Dynamic OCPP EVSE Coordinator - {name}",
                update_method=async_update_data,
                update_interval=timedelta(seconds=new_update_frequency),
            )
            update_frequency = new_update_frequency
            _LOGGER.debug(f"Recreated DataUpdateCoordinator with update_interval: {new_update_frequency} seconds")
            await coordinator.async_config_entry_first_refresh()
            sensor.coordinator = coordinator

    # Register the listener for config entry updates
    _LOGGER.debug("Registering async_on_update listener for %s", name)
    config_entry.async_on_unload(config_entry.add_update_listener(async_update_listener))


class DynamicOcppEvseChargerSensor(SensorEntity):
    """Representation of a Dynamic OCPP EVSE Charger Sensor."""

    def __init__(self, hass, config_entry, hub_entry, name, entity_id, coordinator):
        """Initialize the sensor."""
        self.hass = hass
        self.config_entry = config_entry
        self.hub_entry = hub_entry
        self._attr_name = f"{name} Available Current"
        self._attr_unique_id = f"{entity_id}_available_current"
        self._state = None
        self._phases = None
        self._charging_mode = None
        self._calc_used = None
        self._max_evse_available = None
        self._allocated_current = None
        self._last_update = datetime.min
        self._pause_timer_running = False
        self._last_set_current = 0
        self._target_evse = None
        self._target_evse_standard = None
        self._target_evse_eco = None
        self._target_evse_solar = None
        self._target_evse_excess = None
        self.coordinator = coordinator

    @property
    def state(self):
        """Return the state of the sensor."""
        return self._state

    @property
    def extra_state_attributes(self):
        """Return the state attributes."""
        attrs = {
            "state_class": "measurement",
            CONF_PHASES: self._phases,
            CONF_CHARING_MODE: self._charging_mode,
            "calc_used": self._calc_used,
            "max_evse_available": self._max_evse_available,
            "allocated_current": self._allocated_current,
            "last_update": self._last_update,
            "pause_timer_running": self._pause_timer_running,
            "last_set_current": self._last_set_current,
            "target_evse": self._target_evse,
            "target_evse_standard": self._target_evse_standard,
            "target_evse_eco": self._target_evse_eco,
            "target_evse_solar": self._target_evse_solar,
            "target_evse_excess": self._target_evse_excess,
            "charger_priority": self.config_entry.data.get(CONF_CHARGER_PRIORITY, DEFAULT_CHARGER_PRIORITY),
            "hub_entry_id": self.config_entry.data.get(CONF_HUB_ENTRY_ID),
        }
        if hasattr(self, '_excess_charge_start_time') and self._excess_charge_start_time is not None:
            attrs["excess_charge_start_time"] = self._excess_charge_start_time
        return attrs

    @property
    def icon(self):
        """Return the icon to use in the frontend."""
        return "mdi:ev-station"

    @property
    def unit_of_measurement(self):
        """Return the unit of measurement."""
        return "A"

    @property
    def device_class(self):
        """Return the device class."""
        return "current"

    async def async_update(self):
        """Fetch new state data for the sensor asynchronously."""
        try:
            # Get fresh hub entry in case it was updated
            hub_entry = get_hub_for_charger(self.hass, self.config_entry.entry_id)
            if not hub_entry:
                _LOGGER.error("Hub not found for charger: %s", self._attr_name)
                return
            
            self.hub_entry = hub_entry
            
            # Calculate total available current at hub level
            hub_data = calculate_available_current_for_hub(self)
            
            # Store hub-level calculation results
            self._phases = hub_data.get(CONF_PHASES)
            self._charging_mode = hub_data.get(CONF_CHARING_MODE)
            self._calc_used = hub_data.get("calc_used")
            self._max_evse_available = hub_data.get("max_evse_available")
            self._target_evse = hub_data.get("target_evse")
            self._target_evse_standard = hub_data.get("target_evse_standard")
            self._target_evse_eco = hub_data.get("target_evse_eco")
            self._target_evse_solar = hub_data.get("target_evse_solar")
            self._target_evse_excess = hub_data.get("target_evse_excess")
            
            if "excess_charge_start_time" in hub_data:
                self._excess_charge_start_time = hub_data["excess_charge_start_time"]
            else:
                self._excess_charge_start_time = None

            # Get total available current from hub calculation
            total_available = hub_data.get(CONF_AVAILABLE_CURRENT, 0)
            
            # Distribute current among all chargers connected to this hub
            hub_entry_id = self.config_entry.data.get(CONF_HUB_ENTRY_ID)
            distribute_current_to_chargers(self.hass, hub_entry_id, total_available)
            
            # Get this charger's allocated current
            self._allocated_current = get_charger_allocation(self.hass, self.config_entry.entry_id)
            self._state = self._allocated_current

            # Get charger-specific limits
            min_charge_current = self.config_entry.data.get(CONF_EVSE_MINIMUM_CHARGE_CURRENT, DEFAULT_MIN_CHARGE_CURRENT)

            # Check if the state drops below minimum
            if self._state < min_charge_current and not self._pause_timer_running:
                # Start the Charge Pause Timer
                timer_entity_id = f"timer.{self.config_entry.data[CONF_ENTITY_ID]}_charge_pause_timer"
                try:
                    await self.hass.services.async_call(
                        "timer",
                        "start",
                        {
                            "entity_id": timer_entity_id,
                            "duration": self.config_entry.data.get(CONF_CHARGE_PAUSE_DURATION, DEFAULT_CHARGE_PAUSE_DURATION)
                        }
                    )
                    self._pause_timer_running = True
                except Exception as e:
                    _LOGGER.debug(f"Timer {timer_entity_id} not available: {e}")

            # Check if the timer is running
            timer_entity_id = f"timer.{self.config_entry.data[CONF_ENTITY_ID]}_charge_pause_timer"
            timer_state = self.hass.states.get(timer_entity_id)
            if timer_state and timer_state.state == "active":
                limit = 0
            else:
                limit = round(self._state, 1)
                self._pause_timer_running = False

            self._last_set_current = limit

            # Prepare the data for the OCPP set_charge_rate service
            profile_timeout = self.config_entry.data.get(CONF_OCPP_PROFILE_TIMEOUT, DEFAULT_OCPP_PROFILE_TIMEOUT)
            valid_from = datetime.utcnow().isoformat(timespec='seconds') + 'Z'
            valid_to = (datetime.utcnow() + timedelta(seconds=profile_timeout)).isoformat(timespec='seconds') + 'Z'
            stack_level = self.config_entry.data.get(CONF_STACK_LEVEL, DEFAULT_STACK_LEVEL)
            
            # Get charge rate unit from config (A or W)
            charge_rate_unit = self.config_entry.data.get(CONF_CHARGE_RATE_UNIT, DEFAULT_CHARGE_RATE_UNIT)
            
            # Convert limit based on charge rate unit
            if charge_rate_unit == CHARGE_RATE_UNIT_WATTS:
                # Convert Amps to Watts: W = A * V * phases
                voltage = hub_entry.data.get(CONF_PHASE_VOLTAGE, DEFAULT_PHASE_VOLTAGE)
                phases = self._phases if self._phases else 3
                limit_for_charger = round(limit * voltage * phases, 1)
                rate_unit = "W"
            else:
                # Keep as Amps
                limit_for_charger = limit
                rate_unit = "A"
            
            charging_profile = {
                "chargingProfileId": 11,
                "stackLevel": stack_level,
                "chargingProfileKind": "Relative",
                "chargingProfilePurpose": "TxDefaultProfile",
                "validFrom": valid_from,
                "validTo": valid_to,
                "chargingSchedule": {
                    "chargingRateUnit": rate_unit,
                    "chargingSchedulePeriod": [
                        {
                            "startPeriod": 0,
                            "limit": limit_for_charger
                        }
                    ]
                }
            }

            _LOGGER.debug(f"Sending set_charge_rate for {self._attr_name} with limit: {limit_for_charger}{rate_unit} (calculated from {limit}A)")

            # Call the OCPP set_charge_rate service
            await self.hass.services.async_call(
                "ocpp",
                "set_charge_rate",
                {
                    "custom_profile": charging_profile
                }
            )
            
            self._last_update = datetime.utcnow()
        except Exception as e:
            _LOGGER.error(f"Error updating Dynamic OCPP EVSE Charger Sensor {self._attr_name}: {e}", exc_info=True)
