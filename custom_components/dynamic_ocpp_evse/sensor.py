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
    entry_type = config_entry.data.get(ENTRY_TYPE)
    
    # Set up hub sensor for hub entries
    if entry_type == ENTRY_TYPE_HUB:
        name = config_entry.data.get(CONF_NAME, "Dynamic OCPP EVSE")
        entity_id = config_entry.data.get(CONF_ENTITY_ID, "dynamic_ocpp_evse")
        hub_sensor = DynamicOcppEvseHubSensor(hass, config_entry, name, entity_id)
        async_add_entities([hub_sensor])
        _LOGGER.info(f"Setting up hub sensor: {hub_sensor.unique_id}")
        return
    
    # Only set up charger sensors for charger entries
    if entry_type != ENTRY_TYPE_CHARGER:
        _LOGGER.debug("Skipping sensor setup for unknown entry type: %s", config_entry.title)
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
        self._detected_phases = None  # Remembered phase count from actual charging
        self._charging_mode = None
        self._calc_used = None
        self._allocated_current = None
        self._last_update = datetime.min
        self._pause_timer_running = False
        self._last_set_current = 0
        self._last_set_power = None
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
        """Return charger-specific attributes only (site-level data is on hub sensor)."""
        attrs = {
            "state_class": "measurement",
            CONF_PHASES: self._phases,
            "detected_phases": self._detected_phases,
            "allocated_current": self._allocated_current,
            "last_update": self._last_update,
            "pause_timer_running": self._pause_timer_running,
            "last_set_current": self._last_set_current,
            "last_set_power": self._last_set_power,
            "charger_priority": self.config_entry.data.get(CONF_CHARGER_PRIORITY, DEFAULT_CHARGER_PRIORITY),
            "hub_entry_id": self.config_entry.data.get(CONF_HUB_ENTRY_ID),
        }
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
            
            # Store charger-level calculation results
            self._phases = hub_data.get(CONF_PHASES)
            self._charging_mode = hub_data.get(CONF_CHARING_MODE)
            self._calc_used = hub_data.get("calc_used")
            charger_max_available = hub_data.get("charger_max_available", 0)
            self._target_evse = hub_data.get("target_evse")
            self._target_evse_standard = hub_data.get("target_evse_standard")
            self._target_evse_eco = hub_data.get("target_evse_eco")
            self._target_evse_solar = hub_data.get("target_evse_solar")
            self._target_evse_excess = hub_data.get("target_evse_excess")
            
            if "excess_charge_start_time" in hub_data:
                self._excess_charge_start_time = hub_data["excess_charge_start_time"]
            else:
                self._excess_charge_start_time = None

            # Store hub data in hass.data for hub sensor to read
            hub_entry_id = self.config_entry.data.get(CONF_HUB_ENTRY_ID)
            if DOMAIN not in self.hass.data:
                self.hass.data[DOMAIN] = {}
            if "hub_data" not in self.hass.data[DOMAIN]:
                self.hass.data[DOMAIN]["hub_data"] = {}
            self.hass.data[DOMAIN]["hub_data"][hub_entry_id] = {
                "battery_soc": hub_data.get("battery_soc"),
                "battery_soc_min": hub_data.get("battery_soc_min"),
                "battery_soc_target": hub_data.get("battery_soc_target"),
                "battery_power": hub_data.get("battery_power"),
                "available_battery_power": hub_data.get("available_battery_power"),
                # Site available per-phase current (A)
                "site_available_current_phase_a": hub_data.get("site_available_current_phase_a"),
                "site_available_current_phase_b": hub_data.get("site_available_current_phase_b"),
                "site_available_current_phase_c": hub_data.get("site_available_current_phase_c"),
                # Site battery available power (W)
                "site_battery_available_power": hub_data.get("site_battery_available_power"),
                # Site grid available power (W)
                "site_grid_available_power": hub_data.get("site_grid_available_power"),
                # Total site available power (W) - grid + battery
                "total_site_available_power": hub_data.get("total_site_available_power"),
                "last_update": datetime.utcnow(),
            }

            # Get total available current from hub calculation
            total_available = hub_data.get(CONF_AVAILABLE_CURRENT, 0)
            
            # Distribute current among all chargers connected to this hub
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

            # Prepare the data for the OCPP set_charge_rate service
            profile_timeout = self.config_entry.data.get(CONF_OCPP_PROFILE_TIMEOUT, DEFAULT_OCPP_PROFILE_TIMEOUT)
            stack_level = self.config_entry.data.get(CONF_STACK_LEVEL, DEFAULT_STACK_LEVEL)
            
            # Get charge rate unit from config (A or W)
            charge_rate_unit = self.config_entry.data.get(CONF_CHARGE_RATE_UNIT, DEFAULT_CHARGE_RATE_UNIT)
            
            # If set to auto or not recognized, detect from sensor
            if charge_rate_unit == CHARGE_RATE_UNIT_AUTO or charge_rate_unit not in [CHARGE_RATE_UNIT_AMPS, CHARGE_RATE_UNIT_WATTS]:
                _LOGGER.debug(f"Auto-detecting charge rate unit for {self._attr_name}")
                current_offered_entity = self.config_entry.data.get(CONF_EVSE_CURRENT_OFFERED_ENTITY_ID)
                if current_offered_entity:
                    sensor_state = self.hass.states.get(current_offered_entity)
                    if sensor_state:
                        unit = sensor_state.attributes.get("unit_of_measurement")
                        if unit == "W":
                            charge_rate_unit = CHARGE_RATE_UNIT_WATTS
                            _LOGGER.info(f"Auto-detected charge rate unit: Watts (W) for {self._attr_name}")
                        else:
                            charge_rate_unit = CHARGE_RATE_UNIT_AMPS
                            _LOGGER.info(f"Auto-detected charge rate unit: Amperes (A) for {self._attr_name}")
                    else:
                        _LOGGER.warning(f"Could not get state for {current_offered_entity}, defaulting to Amperes")
                        charge_rate_unit = CHARGE_RATE_UNIT_AMPS
                else:
                    _LOGGER.warning(f"No current_offered entity configured, defaulting to Amperes")
                    charge_rate_unit = CHARGE_RATE_UNIT_AMPS
            
            # Convert limit based on charge rate unit
            if charge_rate_unit == CHARGE_RATE_UNIT_WATTS:
                # For Watts mode: Always use 3 phases for calculation and numberPhases
                # This works with chargers configured for 3-phase (ConnectorPhaseRotation: 0.RST)
                # even when car uses only 1 phase - charger applies power correctly
                voltage = hub_entry.data.get(CONF_PHASE_VOLTAGE, DEFAULT_PHASE_VOLTAGE)
                limit_for_charger = round(limit * voltage, 1)
                rate_unit = "W"
                phases_for_profile = 3
                self._last_set_power = limit_for_charger
                self._last_set_current = None
            else:
                # For Amps mode: Use detected phases, default to 1
                # When using Amps with numberPhases, limit represents TOTAL current across all phases
                phases_for_profile = self._phases if self._phases else 1
                limit_for_charger = round(limit * phases_for_profile, 1)
                rate_unit = "A"
                self._last_set_current = limit_for_charger
                self._last_set_power = None
            
            # Use chargingScheduleDuration instead of absolute validFrom/validTo timestamps
            # This makes the profile valid for a duration (in seconds) from when the charger receives it
            # rather than tied to HA's clock which may be out of sync with the charger
            charging_profile = {
                "chargingProfileId": 11,
                "stackLevel": stack_level,
                "chargingProfileKind": "Relative",
                "chargingProfilePurpose": "TxDefaultProfile",
                "chargingSchedule": {
                    "chargingRateUnit": rate_unit,
                    "duration": profile_timeout,  # Duration in seconds the schedule is valid for
                    "chargingSchedulePeriod": [
                        {
                            "startPeriod": 0,
                            "limit": limit_for_charger,
                            "numberPhases": phases_for_profile  # Explicitly tell charger how to interpret the limit
                        }
                    ]
                }
            }

            _LOGGER.debug(f"Sending set_charge_rate for {self._attr_name} with limit: {limit_for_charger}{rate_unit} (calculated from {limit}A)")

            # Check if charge_control switch is off and we have available current - turn it on
            # But only if a car is actually plugged in (connector status is not "Available")
            charger_entity_id = self.config_entry.data.get(CONF_ENTITY_ID)
            charge_control_switch = f"switch.{charger_entity_id}_charge_control"
            charge_control_state = self.hass.states.get(charge_control_switch)
            
            # Check connector status - only turn on if car is plugged in
            # Status can be: Available, Preparing, Charging, SuspendedEV, SuspendedEVSE, Finishing, Reserved, Unavailable, Faulted
            connector_status_entity = f"sensor.{charger_entity_id}_status_connector"
            connector_status_state = self.hass.states.get(connector_status_entity)
            connector_status = connector_status_state.state if connector_status_state else "unknown"
            
            # Car is plugged in if status is NOT "Available" (meaning: Preparing, Charging, SuspendedEV, etc.)
            car_plugged_in = connector_status not in ["Available", "unknown", "unavailable"]
            
            _LOGGER.debug(f"Charge control check: entity={connector_status_entity}, status={connector_status}, car_plugged_in={car_plugged_in}, limit={limit}A, switch_state={charge_control_state.state if charge_control_state else 'not found'}")
            
            if charge_control_state and charge_control_state.state == "off" and limit > 0 and car_plugged_in:
                _LOGGER.info(f"Charge control switch {charge_control_switch} is off but limit is {limit}A and car is plugged in (connector: {connector_status}) - turning on")
                try:
                    await self.hass.services.async_call(
                        "switch",
                        "turn_on",
                        {
                            "entity_id": charge_control_switch
                        }
                    )
                except Exception as e:
                    _LOGGER.warning(f"Failed to turn on charge_control switch {charge_control_switch}: {e}")
            elif charge_control_state and charge_control_state.state == "off" and limit > 0 and not car_plugged_in:
                _LOGGER.debug(f"Charge control switch {charge_control_switch} is off with limit {limit}A, but no car plugged in (connector: {connector_status}) - not turning on")

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


class DynamicOcppEvseHubSensor(SensorEntity):
    """Hub-level sensor showing site-wide charging information."""

    def __init__(self, hass, config_entry, name, entity_id):
        """Initialize the hub sensor."""
        self.hass = hass
        self.config_entry = config_entry
        self._attr_name = f"{name} Site Info"
        self._attr_unique_id = f"{entity_id}_site_info"
        self._state = None
        self._battery_soc = None
        self._battery_soc_min = None
        self._battery_soc_target = None
        self._battery_power = None
        self._available_battery_power = None
        # Site available per-phase current (A)
        self._site_available_current_phase_a = None
        self._site_available_current_phase_b = None
        self._site_available_current_phase_c = None
        # Site battery available power (W)
        self._site_battery_available_power = None
        # Site grid available power (W)
        self._site_grid_available_power = None
        # Total site available power (W) - grid + battery
        self._total_site_available_power = None
        self._last_update = datetime.min

    @property
    def state(self):
        """Return the state of the sensor (battery SOC as primary state)."""
        if self._battery_soc is not None:
            return round(self._battery_soc, 1)
        return None

    @property
    def extra_state_attributes(self):
        """Return the state attributes - site-level data."""
        def round_value(val, decimals=1):
            return round(val, decimals) if val is not None else None
        
        return {
            "state_class": "measurement",
            "battery_soc_min": round_value(self._battery_soc_min),
            "battery_soc_target": round_value(self._battery_soc_target),
            "battery_power": round_value(self._battery_power),
            "available_battery_power": round_value(self._available_battery_power),
            # Site available per-phase current (A)
            "site_available_current_phase_a": round_value(self._site_available_current_phase_a),
            "site_available_current_phase_b": round_value(self._site_available_current_phase_b),
            "site_available_current_phase_c": round_value(self._site_available_current_phase_c),
            # Site battery available power (W)
            "site_battery_available_power": round_value(self._site_battery_available_power, 0),
            # Site grid available power (W)
            "site_grid_available_power": round_value(self._site_grid_available_power, 0),
            # Total site available power (W) - grid + battery
            "total_site_available_power": round_value(self._total_site_available_power, 0),
            "last_update": self._last_update,
        }

    @property
    def icon(self):
        """Return the icon to use in the frontend."""
        return "mdi:home-lightning-bolt"

    @property
    def unit_of_measurement(self):
        """Return the unit of measurement."""
        return "%"

    @property
    def device_class(self):
        """Return the device class."""
        return "battery"

    async def async_update(self):
        """Update hub sensor with site-wide data from hass.data."""
        try:
            hub_entry_id = self.config_entry.entry_id
            hub_data = self.hass.data.get(DOMAIN, {}).get("hub_data", {}).get(hub_entry_id, {})
            
            if hub_data:
                self._battery_soc = hub_data.get("battery_soc")
                self._battery_soc_min = hub_data.get("battery_soc_min")
                self._battery_soc_target = hub_data.get("battery_soc_target")
                self._battery_power = hub_data.get("battery_power")
                self._available_battery_power = hub_data.get("available_battery_power")
                # Site available per-phase current (A)
                self._site_available_current_phase_a = hub_data.get("site_available_current_phase_a")
                self._site_available_current_phase_b = hub_data.get("site_available_current_phase_b")
                self._site_available_current_phase_c = hub_data.get("site_available_current_phase_c")
                # Site battery available power (W)
                self._site_battery_available_power = hub_data.get("site_battery_available_power")
                # Site grid available power (W)
                self._site_grid_available_power = hub_data.get("site_grid_available_power")
                # Total site available power (W) - grid + battery
                self._total_site_available_power = hub_data.get("total_site_available_power")
                self._last_update = hub_data.get("last_update", datetime.utcnow())
        except Exception as e:
            _LOGGER.error(f"Error updating hub sensor {self._attr_name}: {e}", exc_info=True)
