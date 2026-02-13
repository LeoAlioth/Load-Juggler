import logging
from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from datetime import timedelta, datetime
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from .dynamic_ocpp_evse import calculate_available_current_for_hub
from .const import *
from .helpers import get_entry_value
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
        
        entities = [DynamicOcppEvseHubSensor(hass, config_entry, name, entity_id)]
        # Create individual hub data sensors from definitions
        for defn in HUB_SENSOR_DEFINITIONS:
            entities.append(DynamicOcppEvseHubDataSensor(hass, config_entry, name, entity_id, defn))
        
        async_add_entities(entities)
        _LOGGER.info(f"Setting up hub sensors for {name}")
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
    update_frequency = get_entry_value(config_entry, CONF_UPDATE_FREQUENCY, DEFAULT_UPDATE_FREQUENCY)
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
        new_update_frequency = get_entry_value(entry, CONF_UPDATE_FREQUENCY, DEFAULT_UPDATE_FREQUENCY)
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
    def device_info(self):
        """Return device information about this charger."""
        hub_entity_id = self.hub_entry.data.get(CONF_ENTITY_ID, "dynamic_ocpp_evse")
        return {
            "identifiers": {(DOMAIN, self.config_entry.entry_id)},
            "name": self.config_entry.data.get(CONF_NAME),
            "manufacturer": "Dynamic OCPP EVSE",
            "model": "EV Charger",
            "via_device": (DOMAIN, self.hub_entry.entry_id),
        }

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
            "charger_priority": get_entry_value(self.config_entry, CONF_CHARGER_PRIORITY, DEFAULT_CHARGER_PRIORITY),
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
                # NEW: Site power balance
                "total_evse_power": hub_data.get("total_evse_power"),
                "net_site_consumption": hub_data.get("net_site_consumption"),
                "solar_surplus_power": hub_data.get("solar_surplus_power"),
                "solar_surplus_current": hub_data.get("solar_surplus_current"),
                "last_update": datetime.utcnow(),
            }

            # Get total available current from hub calculation
            total_available = hub_data.get(CONF_AVAILABLE_CURRENT, 0)
            
            # Use pre-computed charger targets from the SiteContext calculation
            charger_targets = hub_data.get("charger_targets", {})

            _LOGGER.debug(f"Charger targets: {', '.join([f'{k[-8:]}: {v:.1f}A' for k, v in charger_targets.items()])}")
            
            # Distribute current among all chargers connected to this hub
            distribute_current_to_chargers(self.hass, hub_entry_id, total_available, charger_targets)
            
            # Get this charger's allocated current
            self._allocated_current = get_charger_allocation(self.hass, self.config_entry.entry_id)
            self._state = self._allocated_current

            # Get charger-specific limits
            min_charge_current = get_entry_value(self.config_entry, CONF_EVSE_MINIMUM_CHARGE_CURRENT, DEFAULT_MIN_CHARGE_CURRENT)

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
                            "duration": get_entry_value(self.config_entry, CONF_CHARGE_PAUSE_DURATION, DEFAULT_CHARGE_PAUSE_DURATION)
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
            profile_timeout = get_entry_value(self.config_entry, CONF_OCPP_PROFILE_TIMEOUT, DEFAULT_OCPP_PROFILE_TIMEOUT)
            stack_level = get_entry_value(self.config_entry, CONF_STACK_LEVEL, DEFAULT_STACK_LEVEL)
            profile_validity_mode = get_entry_value(self.config_entry, CONF_PROFILE_VALIDITY_MODE, DEFAULT_PROFILE_VALIDITY_MODE)
            
            # Get charge rate unit from config (A or W)
            charge_rate_unit = get_entry_value(self.config_entry, CONF_CHARGE_RATE_UNIT, DEFAULT_CHARGE_RATE_UNIT)
            
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
                voltage = hub_entry.data.get(CONF_PHASE_VOLTAGE, DEFAULT_PHASE_VOLTAGE)
                phases_for_profile = self._phases if self._phases else 1
                limit_for_charger = round(limit * voltage * phases_for_profile, 0)
                rate_unit = "W"
                self._last_set_power = limit_for_charger
                self._last_set_current = None
            else:
                limit_for_charger = round(limit , 1)
                rate_unit = "A"
                self._last_set_current = limit_for_charger
                self._last_set_power = None
            
            # Build charging profile based on validity mode
            if profile_validity_mode == PROFILE_VALIDITY_MODE_ABSOLUTE:
                # Absolute mode: Use validFrom/validTo timestamps
                # This provides explicit time windows and is more reliable for some chargers
                now = datetime.utcnow()
                valid_from = now.strftime("%Y-%m-%dT%H:%M:%SZ")
                valid_to = (now + timedelta(seconds=profile_timeout)).strftime("%Y-%m-%dT%H:%M:%SZ")
                
                charging_profile = {
                    "chargingProfileId": 11,
                    "stackLevel": stack_level,
                    "chargingProfileKind": "Absolute",
                    "chargingProfilePurpose": "TxDefaultProfile",
                    "validFrom": valid_from,
                    "validTo": valid_to,
                    "chargingSchedule": {
                        "chargingRateUnit": rate_unit,
                        "startSchedule": valid_from,
                        "chargingSchedulePeriod": [
                            {
                                "startPeriod": 0,
                                "limit": limit_for_charger,
                            }
                        ]
                    }
                }
                _LOGGER.debug(f"Using absolute profile validity mode: {valid_from} to {valid_to}")
            else:
                # Relative mode: Use duration (default)
                # Profile is valid for X seconds from when the charger receives it
                charging_profile = {
                    "chargingProfileId": 11,
                    "stackLevel": stack_level,
                    "chargingProfileKind": "Relative",
                    "chargingProfilePurpose": "TxDefaultProfile",
                    "chargingSchedule": {
                        "chargingRateUnit": rate_unit,
                        "duration": profile_timeout,
                        "chargingSchedulePeriod": [
                            {
                                "startPeriod": 0,
                                "limit": limit_for_charger,
                            }
                        ]
                    }
                }
                _LOGGER.debug(f"Using relative profile validity mode: duration={profile_timeout}s")

            # Get the OCPP device ID for targeting the correct charger
            ocpp_device_id = self.config_entry.data.get(CONF_OCPP_DEVICE_ID)
            if not ocpp_device_id:
                _LOGGER.error(f"No OCPP device ID configured for {self._attr_name} - cannot send charging profile")
                return

            _LOGGER.debug(f"Sending set_charge_rate to device {ocpp_device_id} for {self._attr_name} with limit: {limit_for_charger}{rate_unit} (calculated from {limit}A)")

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

            # Call the OCPP set_charge_rate service with device_id
            await self.hass.services.async_call(
                "ocpp",
                "set_charge_rate",
                {
                    "devid": ocpp_device_id,
                    "custom_profile": charging_profile
                },
                blocking=False,
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
        self._attr_name = f"{name} Site Available Power"
        self._attr_unique_id = f"{entity_id}_site_info"
        self._total_site_available_power = None
        self._last_update = datetime.min

    @property
    def device_info(self):
        """Return device information about this hub."""
        return {
            "identifiers": {(DOMAIN, self.config_entry.entry_id)},
            "name": self.config_entry.data.get(CONF_NAME, "Dynamic OCPP EVSE"),
            "manufacturer": "Dynamic OCPP EVSE",
            "model": "Electrical System Hub",
        }

    @property
    def state(self):
        """Return the state of the sensor (total site available power as primary state)."""
        # Return 0 when no data is available instead of None to avoid "unknown" state
        if self._total_site_available_power is not None:
            return round(self._total_site_available_power, 0)
        return 0.0

    @property
    def extra_state_attributes(self):
        """Return the state attributes."""
        return {
            "state_class": "measurement",
            "last_update": self._last_update,
        }

    @property
    def icon(self):
        """Return the icon to use in the frontend."""
        return "mdi:home-lightning-bolt"

    @property
    def unit_of_measurement(self):
        """Return the unit of measurement."""
        return "W"

    @property
    def device_class(self):
        """Return the device class."""
        return "power"

    async def async_update(self):
        """Update hub sensor with site-wide data from hass.data."""
        try:
            hub_entry_id = self.config_entry.entry_id
            hub_data = self.hass.data.get(DOMAIN, {}).get("hub_data", {}).get(hub_entry_id, {})

            if hub_data:
                self._total_site_available_power = hub_data.get("total_site_available_power")
                self._last_update = hub_data.get("last_update", datetime.utcnow())
        except Exception as e:
            _LOGGER.error(f"Error updating hub sensor {self._attr_name}: {e}", exc_info=True)


# ---------------------------------------------------------------------------
# Data-driven hub sensor definitions
# Each entry defines one individual sensor that reads from hub_data.
# ---------------------------------------------------------------------------
HUB_SENSOR_DEFINITIONS = [
    {
        "name_suffix": "Battery SOC",
        "unique_id_suffix": "battery_soc",
        "hub_data_key": "battery_soc",
        "unit": "%",
        "device_class": "battery",
        "icon": "mdi:battery-80",
        "decimals": 1,
    },
    {
        "name_suffix": "Battery Power",
        "unique_id_suffix": "battery_power",
        "hub_data_key": "battery_power",
        "unit": "W",
        "device_class": "power",
        "icon": "mdi:battery-charging",
        "decimals": 0,
    },
    {
        "name_suffix": "Available Battery Power",
        "unique_id_suffix": "available_battery_power",
        "hub_data_key": "available_battery_power",
        "unit": "W",
        "device_class": "power",
        "icon": "mdi:battery-high",
        "decimals": 0,
    },
    {
        "name_suffix": "Total Site Available Power",
        "unique_id_suffix": "total_site_available_power",
        "hub_data_key": "total_site_available_power",
        "unit": "W",
        "device_class": "power",
        "icon": "mdi:home-lightning-bolt",
        "decimals": 0,
    },
    {
        "name_suffix": "Net Site Consumption",
        "unique_id_suffix": "net_site_consumption",
        "hub_data_key": "net_site_consumption",
        "unit": "W",
        "device_class": "power",
        "icon": "mdi:home-import-outline",
        "decimals": 0,
    },
    {
        "name_suffix": "Site Available Current Phase A",
        "unique_id_suffix": "site_available_current_phase_a",
        "hub_data_key": "site_available_current_phase_a",
        "unit": "A",
        "device_class": "current",
        "icon": "mdi:current-ac",
        "decimals": 1,
    },
    {
        "name_suffix": "Site Available Current Phase B",
        "unique_id_suffix": "site_available_current_phase_b",
        "hub_data_key": "site_available_current_phase_b",
        "unit": "A",
        "device_class": "current",
        "icon": "mdi:current-ac",
        "decimals": 1,
    },
    {
        "name_suffix": "Site Available Current Phase C",
        "unique_id_suffix": "site_available_current_phase_c",
        "hub_data_key": "site_available_current_phase_c",
        "unit": "A",
        "device_class": "current",
        "icon": "mdi:current-ac",
        "decimals": 1,
    },
    {
        "name_suffix": "Site Battery Available Power",
        "unique_id_suffix": "site_battery_available_power",
        "hub_data_key": "site_battery_available_power",
        "unit": "W",
        "device_class": "power",
        "icon": "mdi:battery-arrow-up",
        "decimals": 0,
    },
    {
        "name_suffix": "Site Grid Available Power",
        "unique_id_suffix": "site_grid_available_power",
        "hub_data_key": "site_grid_available_power",
        "unit": "W",
        "device_class": "power",
        "icon": "mdi:transmission-tower",
        "decimals": 0,
    },
    {
        "name_suffix": "Total EVSE Power",
        "unique_id_suffix": "total_evse_power",
        "hub_data_key": "total_evse_power",
        "unit": "W",
        "device_class": "power",
        "icon": "mdi:ev-station",
        "decimals": 0,
    },
    {
        "name_suffix": "Solar Surplus Power",
        "unique_id_suffix": "solar_surplus_power",
        "hub_data_key": "solar_surplus_power",
        "unit": "W",
        "device_class": "power",
        "icon": "mdi:solar-power",
        "decimals": 0,
    },
    {
        "name_suffix": "Solar Surplus Current",
        "unique_id_suffix": "solar_surplus_current",
        "hub_data_key": "solar_surplus_current",
        "unit": "A",
        "device_class": "current",
        "icon": "mdi:solar-power",
        "decimals": 2,
    },
]


class DynamicOcppEvseHubDataSensor(SensorEntity):
    """Generic hub data sensor driven by a definition dict."""

    def __init__(self, hass, config_entry, name, entity_id, defn):
        self.hass = hass
        self.config_entry = config_entry
        self._defn = defn
        self._attr_name = f"{name} {defn['name_suffix']}"
        self._attr_unique_id = f"{entity_id}_{defn['unique_id_suffix']}"
        self._attr_native_unit_of_measurement = defn["unit"]
        self._attr_device_class = defn["device_class"]
        self._attr_icon = defn["icon"]
        self._state = None

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self.config_entry.entry_id)},
            "name": self.config_entry.data.get(CONF_NAME, "Dynamic OCPP EVSE"),
            "manufacturer": "Dynamic OCPP EVSE",
            "model": "Electrical System Hub",
        }

    @property
    def state(self):
        return self._state

    async def async_update(self):
        try:
            hub_data = self.hass.data.get(DOMAIN, {}).get("hub_data", {}).get(self.config_entry.entry_id, {})
            key = self._defn["hub_data_key"]
            if hub_data and key in hub_data and hub_data[key] is not None:
                self._state = round(float(hub_data[key]), self._defn["decimals"])
        except Exception as e:
            _LOGGER.error(f"Error updating {self._attr_name}: {e}", exc_info=True)
