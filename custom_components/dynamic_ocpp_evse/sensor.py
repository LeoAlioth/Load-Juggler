import logging
import time
from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from datetime import timedelta, datetime, timezone
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from .dynamic_ocpp_evse import run_hub_calculation
from .const import *
from .helpers import get_entry_value
from .entity_mixins import HubEntityMixin, ChargerEntityMixin
from . import get_hub_for_charger

_LOGGER = logging.getLogger(__name__)
SCAN_INTERVAL = timedelta(seconds=10)


async def async_setup_entry(hass: HomeAssistant, config_entry: ConfigEntry, async_add_entities):
    """Set up the Dynamic OCPP EVSE Sensor from a config entry."""
    entry_type = config_entry.data.get(ENTRY_TYPE)
    
    # Set up hub sensor for hub entries
    if entry_type == ENTRY_TYPE_HUB:
        name = config_entry.data.get(CONF_NAME, "Dynamic OCPP EVSE")
        entity_id = config_entry.data.get(CONF_ENTITY_ID, "dynamic_ocpp_evse")

        # Check which optional hardware is configured
        has_battery = bool(get_entry_value(config_entry, CONF_BATTERY_SOC_ENTITY_ID, None))
        has_phase_b = bool(get_entry_value(config_entry, CONF_PHASE_B_CURRENT_ENTITY_ID, None))
        has_phase_c = bool(get_entry_value(config_entry, CONF_PHASE_C_CURRENT_ENTITY_ID, None))

        entities = [DynamicOcppEvseHubSensor(hass, config_entry, name, entity_id)]
        # Create individual hub data sensors from definitions
        for defn in HUB_SENSOR_DEFINITIONS:
            if defn.get("requires_battery") and not has_battery:
                continue
            if defn.get("requires_phase") == "B" and not has_phase_b:
                continue
            if defn.get("requires_phase") == "C" and not has_phase_c:
                continue
            entities.append(DynamicOcppEvseHubDataSensor(hass, config_entry, name, entity_id, defn))

        async_add_entities(entities)
        phases = "A" + ("B" if has_phase_b else "") + ("C" if has_phase_c else "")
        _LOGGER.info(f"Setting up hub sensors for {name} (battery={'yes' if has_battery else 'no'}, phases={phases})")
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

    # Site update frequency (fast loop) — controls how often site sensors refresh.
    # Read from hub config since it's a site-level setting.
    site_update_frequency = get_entry_value(hub_entry, CONF_SITE_UPDATE_FREQUENCY, DEFAULT_SITE_UPDATE_FREQUENCY)
    _LOGGER.info(f"Initial site update frequency for {name}: {site_update_frequency}s (charger command rate: {get_entry_value(config_entry, CONF_UPDATE_FREQUENCY, DEFAULT_UPDATE_FREQUENCY)}s)")

    # Create the sensor FIRST so the coordinator can reference its persistent state
    # (e.g., _last_command_time for throttling OCPP commands).
    sensor = DynamicOcppEvseChargerSensor(hass, config_entry, hub_entry, name, entity_id, None)

    async def async_update_data():
        """Fetch data for the coordinator using the persistent sensor instance."""
        await sensor.async_update()
        return {
            CONF_TOTAL_ALLOCATED_CURRENT: sensor._state,
            CONF_PHASES: sensor._phases,
            "calc_used": sensor._calc_used,
            "allocated_current": sensor._allocated_current,
        }

    # Create a DataUpdateCoordinator at the fast site refresh rate.
    # OCPP commands are throttled internally by the sensor's _last_command_time.
    coordinator = DataUpdateCoordinator(
        hass,
        _LOGGER,
        name=f"Dynamic OCPP EVSE Coordinator - {name}",
        update_method=async_update_data,
        update_interval=timedelta(seconds=site_update_frequency),
    )
    sensor.coordinator = coordinator
    allocated_sensor = DynamicOcppEvseAllocatedCurrentSensor(hass, config_entry, hub_entry, name, entity_id)
    status_sensor = DynamicOcppEvseChargerStatusSensor(hass, config_entry, hub_entry, name, entity_id)
    async_add_entities([sensor, allocated_sensor, status_sensor])

    # Start the first update
    await coordinator.async_config_entry_first_refresh()

    # Listen for updates to the config entry and recreate the coordinator if necessary
    async def async_update_listener(hass: HomeAssistant, entry: ConfigEntry):
        """Handle options update."""
        nonlocal site_update_frequency
        _LOGGER.debug("async_update_listener triggered for %s", name)
        # Re-read hub entry for hub-level settings
        current_hub = get_hub_for_charger(hass, entry.entry_id)
        new_site_freq = get_entry_value(current_hub, CONF_SITE_UPDATE_FREQUENCY, DEFAULT_SITE_UPDATE_FREQUENCY) if current_hub else site_update_frequency
        if new_site_freq != site_update_frequency:
            _LOGGER.info(f"Updating site_update_frequency to {new_site_freq}s for {name}")
            nonlocal coordinator
            coordinator = DataUpdateCoordinator(
                hass,
                _LOGGER,
                name=f"Dynamic OCPP EVSE Coordinator - {name}",
                update_method=async_update_data,
                update_interval=timedelta(seconds=new_site_freq),
            )
            site_update_frequency = new_site_freq
            await coordinator.async_config_entry_first_refresh()
            sensor.coordinator = coordinator

    # Register the listener for config entry updates
    _LOGGER.debug("Registering async_on_update listener for %s", name)
    config_entry.async_on_unload(config_entry.add_update_listener(async_update_listener))


class DynamicOcppEvseChargerSensor(ChargerEntityMixin, SensorEntity):
    """Representation of a Dynamic OCPP EVSE Charger Sensor."""

    def __init__(self, hass, config_entry, hub_entry, name, entity_id, coordinator):
        """Initialize the sensor."""
        self.hass = hass
        self.config_entry = config_entry
        self.hub_entry = hub_entry
        self._attr_name = f"{name} Available Current"
        self._attr_unique_id = f"{entity_id}_available_current"
        # Pre-build OCPP entity IDs (used in multiple methods)
        charger_entity_id = config_entry.data.get(CONF_ENTITY_ID)
        self._connector_status_entity = f"sensor.{charger_entity_id}_status_connector"
        self._charge_control_entity = f"switch.{charger_entity_id}_charge_control"
        self._state = None
        self._phases = None
        self._car_active_phases = None  # Actual car phase count for W conversion
        self._detected_phases = None  # Remembered phase count from actual charging
        self._operating_mode = None  # Per-charger operating mode
        self._calc_used = None
        self._allocated_current = None
        self._available_current = None  # What the charger could get if active
        self._last_update = datetime.min
        self._pause_started_at = None  # datetime when charge pause started
        self._grace_started_at = None  # datetime when Solar/Excess grace period started
        self._prev_operating_mode = None   # for detecting mode changes to cancel pause
        self._prev_distribution_mode = None
        self._last_set_current = 0
        self._last_set_power = None
        self._ema_current = None             # EMA-smoothed engine output
        self._rate_limited_current = None   # Final output (after EMA + dead band + rate limit)
        self._last_commanded_limit = None   # Amps actually sent to charger (for compliance check)
        self._last_command_time: float = -float('inf')  # monotonic time of last OCPP/plug command
        self._mismatch_count = 0           # consecutive non-compliant cycles
        self._last_auto_reset_at = None    # datetime of last auto-reset
        self._target_evse = None
        self._target_evse_standard = None
        self._target_evse_eco = None
        self._target_evse_solar = None
        self._target_evse_excess = None
        self._charging_status = "Unknown"
        self.coordinator = coordinator

    @property
    def state(self):
        """Return the state of the sensor."""
        return self._state

    @property
    def extra_state_attributes(self):
        """Return charger-specific attributes only (site-level data is on hub sensor)."""
        pause_remaining = None
        if self._pause_started_at is not None:
            pause_duration_min = get_entry_value(self.config_entry, CONF_CHARGE_PAUSE_DURATION, DEFAULT_CHARGE_PAUSE_DURATION)
            elapsed = (datetime.now() - self._pause_started_at).total_seconds()
            pause_remaining = max(0, round(pause_duration_min * 60 - elapsed))

        grace_remaining = None
        if self._grace_started_at is not None:
            grace_period_min = get_entry_value(self.hub_entry, CONF_SOLAR_GRACE_PERIOD, DEFAULT_SOLAR_GRACE_PERIOD)
            elapsed = (datetime.now() - self._grace_started_at).total_seconds()
            grace_remaining = max(0, round(grace_period_min * 60 - elapsed))

        attrs = {
            "state_class": "measurement",
            CONF_PHASES: self._phases,
            "detected_phases": self._detected_phases,
            "allocated_current": self._allocated_current,
            "available_current": self._available_current,
            "last_update": self._last_update,
            "pause_active": self._pause_started_at is not None,
            "pause_remaining_seconds": pause_remaining,
            "grace_active": self._grace_started_at is not None,
            "grace_remaining_seconds": grace_remaining,
            "last_set_current": self._last_set_current,
            "last_set_power": self._last_set_power,
            "charger_priority": get_entry_value(self.config_entry, CONF_CHARGER_PRIORITY, DEFAULT_CHARGER_PRIORITY),
            "hub_entry_id": self.config_entry.data.get(CONF_HUB_ENTRY_ID),
            "auto_reset_mismatch_count": self._mismatch_count,
            "last_auto_reset": self._last_auto_reset_at,
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

    async def _detect_charge_rate_unit_ocpp(self, ocpp_device_id: str) -> str | None:
        """Query OCPP charger for ChargingScheduleAllowedChargingRateUnit."""
        if not ocpp_device_id:
            return None
        if not self.hass.services.has_service("ocpp", "get_configuration"):
            return None
        try:
            response = await self.hass.services.async_call(
                "ocpp", "get_configuration",
                {"devid": ocpp_device_id, "ocpp_key": "ChargingScheduleAllowedChargingRateUnit"},
                blocking=True,
                return_response=True,
            )
            if not response or not isinstance(response, dict):
                return None
            value = response.get("ChargingScheduleAllowedChargingRateUnit")
            if value is None:
                value = response.get("value")
            if value is None:
                for item in response.get("configurationKey", []):
                    if isinstance(item, dict) and item.get("key") == "ChargingScheduleAllowedChargingRateUnit":
                        value = item.get("value")
                        break
            if not value:
                return None
            value = str(value).strip()
            if "Current" in value and "Power" in value:
                return CHARGE_RATE_UNIT_AMPS
            elif "Power" in value:
                return CHARGE_RATE_UNIT_WATTS
            elif "Current" in value:
                return CHARGE_RATE_UNIT_AMPS
            return None
        except Exception:
            return None

    async def _check_profile_compliance(self, limit: float, dynamic_control_on: bool) -> None:
        """Check if the charger is following commanded profiles and auto-reset if not.

        Compares the previous cycle's commanded limit against the charger's
        current_offered entity. Triggers reset_ocpp_evse after sustained mismatch.
        """
        # Guard: only when dynamic control is on and we're actively charging
        if not dynamic_control_on or limit <= 0:
            self._mismatch_count = 0
            return

        # Guard: skip first cycle (no previous command to compare)
        if self._last_commanded_limit is None or self._last_commanded_limit <= 0:
            return

        # Guard: cooldown after last reset
        if self._last_auto_reset_at is not None:
            elapsed = (datetime.now() - self._last_auto_reset_at).total_seconds()
            if elapsed < AUTO_RESET_COOLDOWN_SECONDS:
                self._mismatch_count = 0
                return

        # Guard: car must be plugged in
        connector_status_state = self.hass.states.get(self._connector_status_entity)
        connector_status = connector_status_state.state if connector_status_state else "unknown"
        if connector_status in ("Available", "unknown", "unavailable"):
            self._mismatch_count = 0
            return

        # Read current_offered from OCPP entity
        current_offered_entity_id = self.config_entry.data.get(CONF_EVSE_CURRENT_OFFERED_ENTITY_ID)
        if not current_offered_entity_id:
            return

        current_offered_state = self.hass.states.get(current_offered_entity_id)
        if not current_offered_state or current_offered_state.state in ("unknown", "unavailable", None, ""):
            return

        try:
            current_offered = float(current_offered_state.state)
        except (ValueError, TypeError):
            return

        # Tolerance = max ramp-down per cycle (dynamically adapts to update_frequency)
        update_freq = get_entry_value(self.config_entry, CONF_UPDATE_FREQUENCY, DEFAULT_UPDATE_FREQUENCY)
        tolerance = RAMP_DOWN_RATE * update_freq

        diff = abs(current_offered - self._last_commanded_limit)
        if diff > tolerance:
            self._mismatch_count += 1
            _LOGGER.debug(
                "Profile mismatch for %s: commanded=%.1fA, offered=%.1fA, diff=%.1fA "
                "(cycle %d/%d)",
                self._attr_name, self._last_commanded_limit, current_offered,
                diff, self._mismatch_count, AUTO_RESET_MISMATCH_THRESHOLD,
            )
        else:
            if self._mismatch_count > 0:
                _LOGGER.debug(
                    "Profile compliance restored for %s (was at %d cycles)",
                    self._attr_name, self._mismatch_count,
                )
            self._mismatch_count = 0
            return

        # Check if threshold reached
        if self._mismatch_count >= AUTO_RESET_MISMATCH_THRESHOLD:
            _LOGGER.info(
                "Auto-reset triggered for %s: charger offered %.1fA but we commanded "
                "%.1fA for %d consecutive cycles",
                self._attr_name, current_offered, self._last_commanded_limit,
                self._mismatch_count,
            )
            self._mismatch_count = 0
            self._last_auto_reset_at = datetime.now()

            try:
                await self.hass.services.async_call(
                    DOMAIN, "reset_ocpp_evse",
                    {"entry_id": self.config_entry.entry_id},
                )
            except Exception as e:
                _LOGGER.error("Auto-reset service call failed for %s: %s", self._attr_name, e)

    async def _send_plug_command(self, limit: float, hub_data: dict, now_mono: float) -> None:
        """Send on/off command to a smart load device."""
        plug_switch_entity = self.config_entry.data.get(CONF_PLUG_SWITCH_ENTITY_ID)
        if not plug_switch_entity:
            _LOGGER.error(f"No switch entity configured for plug {self._attr_name}")
            return

        if limit > 0:
            _LOGGER.debug(f"Smart load {self._attr_name}: turning ON (limit={limit}A)")
            await self.hass.services.async_call(
                "switch", "turn_on",
                {"entity_id": plug_switch_entity},
                blocking=False,
            )
        else:
            _LOGGER.debug(f"Smart load {self._attr_name}: turning OFF (limit=0)")
            await self.hass.services.async_call(
                "switch", "turn_off",
                {"entity_id": plug_switch_entity},
                blocking=False,
            )

        # Auto-update device power from power monitoring average
        plug_auto_power = hub_data.get("plug_auto_power", {})
        auto_power = plug_auto_power.get(self.config_entry.entry_id)
        if auto_power is not None:
            charger_data = self.hass.data.get(DOMAIN, {}).get("chargers", {}).get(self.config_entry.entry_id)
            if charger_data is not None:
                charger_data["device_power"] = auto_power

        self._last_update = datetime.now(timezone.utc)
        self._last_command_time = now_mono

    async def _send_ocpp_command(self, limit: float, hub_entry, dynamic_control_on: bool, now_mono: float) -> None:
        """Send OCPP charging profile to an EVSE charger.

        Rate limiting is already applied at the allocated current level
        (in async_update), so `limit` arrives pre-smoothed.
        """
        # Skip OCPP commands for non-chargeable connector states
        connector_state = self.hass.states.get(self._connector_status_entity)
        connector_status = connector_state.state if connector_state else "unknown"
        if connector_status in ("Finishing", "Faulted"):
            _LOGGER.debug(
                "Skipping OCPP command for %s — connector is %s",
                self._attr_name, connector_status,
            )
            self._last_update = datetime.now(timezone.utc)
            self._last_command_time = now_mono
            return

        # Check if charger is following our profiles (auto-reset detection)
        await self._check_profile_compliance(limit, dynamic_control_on)

        profile_timeout = int(get_entry_value(self.config_entry, CONF_OCPP_PROFILE_TIMEOUT, DEFAULT_OCPP_PROFILE_TIMEOUT))
        stack_level = int(get_entry_value(self.config_entry, CONF_STACK_LEVEL, DEFAULT_STACK_LEVEL))
        profile_validity_mode = get_entry_value(self.config_entry, CONF_PROFILE_VALIDITY_MODE, DEFAULT_PROFILE_VALIDITY_MODE)

        # Get charge rate unit from config (A or W)
        charge_rate_unit = get_entry_value(self.config_entry, CONF_CHARGE_RATE_UNIT, DEFAULT_CHARGE_RATE_UNIT)

        # Legacy fallback: if still set to "auto" (pre-OCPP detection), try
        # querying the charger via OCPP and cache the result for future cycles.
        if charge_rate_unit not in (CHARGE_RATE_UNIT_AMPS, CHARGE_RATE_UNIT_WATTS):
            cached = getattr(self, "_cached_charge_rate_unit", None)
            if cached:
                charge_rate_unit = cached
            else:
                ocpp_device_id = self.config_entry.data.get(CONF_OCPP_DEVICE_ID)
                detected = await self._detect_charge_rate_unit_ocpp(ocpp_device_id)
                if detected:
                    charge_rate_unit = detected
                    self._cached_charge_rate_unit = detected
                    _LOGGER.info("OCPP-detected charge rate unit: %s for %s", detected, self._attr_name)
                else:
                    charge_rate_unit = CHARGE_RATE_UNIT_AMPS
                    _LOGGER.warning("Could not detect charge rate unit for %s, defaulting to Amperes", self._attr_name)

        # Convert limit based on charge rate unit
        if charge_rate_unit == CHARGE_RATE_UNIT_WATTS:
            voltage = hub_entry.data.get(CONF_PHASE_VOLTAGE, DEFAULT_PHASE_VOLTAGE)
            # Use car's actual active phases (not charger hardware phases)
            # to avoid 3x over-allocation for 1-phase cars on 3-phase EVSEs
            phases_for_profile = self._car_active_phases or self._phases or 1
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
            now = datetime.now(timezone.utc)
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
        charge_control_state = self.hass.states.get(self._charge_control_entity)
        connector_status_state = self.hass.states.get(self._connector_status_entity)
        connector_status = connector_status_state.state if connector_status_state else "unknown"

        car_plugged_in = connector_status not in ["Available", "unknown", "unavailable"]

        _LOGGER.debug(f"Charge control check: entity={self._connector_status_entity}, status={connector_status}, car_plugged_in={car_plugged_in}, limit={limit}A, switch_state={charge_control_state.state if charge_control_state else 'not found'}")

        if charge_control_state and charge_control_state.state == "off" and limit > 0 and car_plugged_in:
            _LOGGER.info(f"Charge control switch {self._charge_control_entity} is off but limit is {limit}A and car is plugged in (connector: {connector_status}) - turning on")
            try:
                await self.hass.services.async_call(
                    "switch", "turn_on",
                    {"entity_id": self._charge_control_entity}
                )
            except Exception as e:
                _LOGGER.warning(f"Failed to turn on charge_control switch {self._charge_control_entity}: {e}")
        elif charge_control_state and charge_control_state.state == "off" and limit > 0 and not car_plugged_in:
            _LOGGER.debug(f"Charge control switch {self._charge_control_entity} is off with limit {limit}A, but no car plugged in (connector: {connector_status}) - not turning on")

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

        self._last_commanded_limit = limit  # Store for next cycle's ramp + compliance
        self._last_update = datetime.now(timezone.utc)
        self._last_command_time = now_mono

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
            hub_data = run_hub_calculation(self)

            # Fire auto-detection notifications (inversion, phase mapping)
            for notif in hub_data.get("auto_detect_notifications", []):
                await self.hass.services.async_call(
                    "persistent_notification", "create",
                    {
                        "title": notif["title"],
                        "message": notif["message"],
                        "notification_id": notif["notification_id"],
                    },
                )
                _LOGGER.warning("AutoDetect notification: %s", notif["notification_id"])

            # Store charger-level calculation results
            self._phases = hub_data.get(CONF_PHASES)
            charger_active_phases = hub_data.get("charger_active_phases", {})
            self._car_active_phases = charger_active_phases.get(
                self.config_entry.entry_id, self._phases or 1,
            )
            current_distribution_mode = hub_data.get("distribution_mode")

            # Read per-charger operating mode from hub_data
            charger_modes = hub_data.get("charger_modes", {})
            self._operating_mode = charger_modes.get(self.config_entry.entry_id)

            # Detect mode changes — used to cancel pause AND bypass rate limiting
            mode_changed = (
                (self._prev_operating_mode is not None and self._operating_mode != self._prev_operating_mode) or
                (self._prev_distribution_mode is not None and current_distribution_mode != self._prev_distribution_mode)
            )

            # Cancel charge pause and grace timer when user changes operating or distribution mode
            if mode_changed:
                if self._pause_started_at is not None:
                    _LOGGER.info(
                        "Mode changed for %s (operating: %s→%s, distribution: %s→%s) — cancelling charge pause",
                        self._attr_name, self._prev_operating_mode, self._operating_mode,
                        self._prev_distribution_mode, current_distribution_mode,
                    )
                    self._pause_started_at = None
                if self._grace_started_at is not None:
                    _LOGGER.info("Mode changed for %s — cancelling grace timer", self._attr_name)
                    self._grace_started_at = None

            self._prev_operating_mode = self._operating_mode
            self._prev_distribution_mode = current_distribution_mode

            self._calc_used = hub_data.get("calc_used")

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
                # Available per-phase current (A)
                "available_current_a": hub_data.get("available_current_a"),
                "available_current_b": hub_data.get("available_current_b"),
                "available_current_c": hub_data.get("available_current_c"),
                # Current power readings
                "grid_power": hub_data.get("grid_power"),
                "solar_power": hub_data.get("solar_power"),
                # Available power breakdown
                "available_grid_power": hub_data.get("available_grid_power"),
                "available_solar_power": hub_data.get("available_solar_power"),
                "available_battery_power": hub_data.get("available_battery_power"),
                "total_site_available_power": hub_data.get("total_site_available_power"),
                "total_evse_power": hub_data.get("total_evse_power"),
                "last_update": datetime.now(timezone.utc),
            }

            # Use pre-computed charger targets from the calculation engine
            # These are the final allocations — the engine already handles distribution
            charger_targets = hub_data.get("charger_targets", {})

            if charger_targets:
                charger_names = hub_data.get("charger_names", {})
                charger_modes = hub_data.get("charger_modes", {})
                charger_avail = hub_data.get("charger_available", {})
                _LOGGER.debug("Charger targets: %s", ", ".join(
                    [f"{charger_names.get(k, k[-8:])}({charger_modes.get(k, '?')}): "
                     f"alloc={v:.1f}A avail={charger_avail.get(k, 0):.1f}A"
                     for k, v in charger_targets.items()]
                ))

            # Get this charger's raw allocated current from engine output
            raw_allocated = round(charger_targets.get(self.config_entry.entry_id, 0), 1)

            # Get available current (what this charger could get if active)
            charger_avail_data = hub_data.get("charger_available", {})
            self._available_current = round(charger_avail_data.get(self.config_entry.entry_id, 0), 1)

            # --- Smoothing pipeline: EMA → dead band → rate limit ---
            # On mode change or first run: reset and pass through immediately.
            if mode_changed or self._ema_current is None:
                self._ema_current = raw_allocated
                self._rate_limited_current = raw_allocated
                if mode_changed:
                    _LOGGER.debug("Mode changed for %s — smoothing reset (allocated=%.1fA)",
                                  self._attr_name, raw_allocated)
            elif raw_allocated == 0 or self._rate_limited_current == 0:
                # Transitions to/from zero pass through immediately (pause/resume)
                self._ema_current = raw_allocated
                self._rate_limited_current = raw_allocated
            else:
                # Step 1: EMA smoothing on the raw engine output
                self._ema_current = round(EMA_ALPHA * raw_allocated + (1 - EMA_ALPHA) * self._ema_current, 2)

                # Step 2: Dead band (Schmitt trigger) — ignore small oscillations
                if abs(self._ema_current - self._rate_limited_current) < DEAD_BAND:
                    pass  # Keep current value, skip rate limiter
                else:
                    # Step 3: Rate limiter — clamp the change per cycle
                    site_freq = get_entry_value(hub_entry, CONF_SITE_UPDATE_FREQUENCY, DEFAULT_SITE_UPDATE_FREQUENCY)
                    max_up = RAMP_UP_RATE * site_freq
                    max_down = RAMP_DOWN_RATE * site_freq
                    target = self._ema_current
                    delta = target - self._rate_limited_current
                    if delta > max_up:
                        target = self._rate_limited_current + max_up
                        _LOGGER.debug("Ramp UP for %s: %.1fA → %.1fA (ema=%.1fA, max +%.1fA/cycle)",
                                      self._attr_name, self._rate_limited_current, target, self._ema_current, max_up)
                    elif delta < -max_down:
                        target = self._rate_limited_current - max_down
                        _LOGGER.debug("Ramp DOWN for %s: %.1fA → %.1fA (ema=%.1fA, max -%.1fA/cycle)",
                                      self._attr_name, self._rate_limited_current, target, self._ema_current, max_down)
                    self._rate_limited_current = round(target, 1)

            self._allocated_current = self._rate_limited_current
            self._state = self._rate_limited_current

            # --- Grace timer for Solar/Excess modes (anti-flicker) ---
            # When Solar/Excess conditions drop below minimum but site limits still
            # allow charging, hold at min_current for a grace period before pausing.
            min_charge_current = get_entry_value(self.config_entry, CONF_EVSE_MINIMUM_CHARGE_CURRENT, DEFAULT_MIN_CHARGE_CURRENT)
            grace_period_minutes = get_entry_value(hub_entry, CONF_SOLAR_GRACE_PERIOD, DEFAULT_SOLAR_GRACE_PERIOD)
            grace_period_seconds = grace_period_minutes * 60

            if self._operating_mode in (OPERATING_MODE_SOLAR_ONLY, OPERATING_MODE_EXCESS) and grace_period_seconds > 0:
                if self._allocated_current < min_charge_current:
                    # Engine says stop — check if site physical limits allow grace
                    # Use charger_available as proxy for physical headroom
                    charger_avail = hub_data.get("charger_available", {})
                    physical_available = charger_avail.get(self.config_entry.entry_id, 0)
                    if physical_available >= min_charge_current:
                        # Site can support min_current — apply grace
                        if self._grace_started_at is None:
                            self._grace_started_at = datetime.now()
                            _LOGGER.debug("Grace timer started for %s (mode=%s, grace=%dm)",
                                          self._attr_name, self._operating_mode, grace_period_minutes)
                        elapsed = (datetime.now() - self._grace_started_at).total_seconds()
                        if elapsed < grace_period_seconds:
                            # Override: charge at min_current during grace
                            self._allocated_current = float(min_charge_current)
                            self._state = self._allocated_current
                        else:
                            # Grace expired — let engine's 0 through (pause will kick in)
                            _LOGGER.info("Grace timer expired for %s after %dm — allowing pause",
                                         self._attr_name, grace_period_minutes)
                            self._grace_started_at = None
                    else:
                        # Site limits don't support min_current — immediate stop
                        if self._grace_started_at is not None:
                            _LOGGER.info("Site limit violation for %s — cancelling grace timer", self._attr_name)
                            self._grace_started_at = None
                else:
                    # Conditions recovered — reset grace timer
                    if self._grace_started_at is not None:
                        _LOGGER.debug("Grace timer reset for %s — conditions recovered", self._attr_name)
                        self._grace_started_at = None
            else:
                # Not in Solar Only/Excess mode or grace disabled — reset
                if self._grace_started_at is not None:
                    self._grace_started_at = None

            # Update global allocations so the allocated current sensor can read them
            if DOMAIN not in self.hass.data:
                self.hass.data[DOMAIN] = {}
            if "charger_allocations" not in self.hass.data[DOMAIN]:
                self.hass.data[DOMAIN]["charger_allocations"] = {}
            self.hass.data[DOMAIN]["charger_allocations"][self.config_entry.entry_id] = self._allocated_current

            # --- Throttle: only send device commands at the charger update rate ---
            command_interval = get_entry_value(self.config_entry, CONF_UPDATE_FREQUENCY, DEFAULT_UPDATE_FREQUENCY)
            now_mono = time.monotonic()
            if now_mono - self._last_command_time < command_interval:
                _LOGGER.debug("Site refresh for %s (command send in %.0fs)",
                              self._attr_name, command_interval - (now_mono - self._last_command_time))
                return

            # Get charger-specific limits
            min_charge_current = get_entry_value(self.config_entry, CONF_EVSE_MINIMUM_CHARGE_CURRENT, DEFAULT_MIN_CHARGE_CURRENT)
            max_charge_current = get_entry_value(self.config_entry, CONF_EVSE_MAXIMUM_CHARGE_CURRENT, DEFAULT_MAX_CHARGE_CURRENT)

            # Check if dynamic control is disabled — if so, charge at max
            charger_rt = self.hass.data.get(DOMAIN, {}).get("chargers", {}).get(self.config_entry.entry_id, {})
            dynamic_control_on = charger_rt.get("dynamic_control", True)

            if not dynamic_control_on:
                limit = round(float(max_charge_current), 1)
                self._pause_started_at = None
                _LOGGER.debug("Dynamic control OFF for %s — using max current %sA", self._attr_name, limit)
            # Charge pause logic: hold at 0A for a configured duration when
            # allocated current drops below the charger's minimum
            elif self._allocated_current < min_charge_current:
                pause_duration_s = get_entry_value(self.config_entry, CONF_CHARGE_PAUSE_DURATION, DEFAULT_CHARGE_PAUSE_DURATION) * 60
                # Current below minimum — start or continue pause
                if self._pause_started_at is None:
                    self._pause_started_at = datetime.now()
                    _LOGGER.debug("Charge pause started for %s", self._attr_name)
                # While pausing (regardless of elapsed time), charger can't meet minimum
                limit = 0
            else:
                pause_duration_s = get_entry_value(self.config_entry, CONF_CHARGE_PAUSE_DURATION, DEFAULT_CHARGE_PAUSE_DURATION) * 60
                # Current >= minimum — reset pause, charge normally
                if self._pause_started_at is not None:
                    elapsed = (datetime.now() - self._pause_started_at).total_seconds()
                    if elapsed < pause_duration_s:
                        # Still within pause window — hold at 0
                        limit = 0
                    else:
                        # Pause expired and current is sufficient — resume
                        self._pause_started_at = None
                        limit = round(self._allocated_current, 1)
                else:
                    limit = round(self._allocated_current, 1)

            # Determine charging status reason
            connector_state = self.hass.states.get(self._connector_status_entity)
            connector_status = connector_state.state if connector_state else "unknown"

            if connector_status in ("Available", "unknown", "unavailable"):
                self._charging_status = "Not Connected"
            elif not dynamic_control_on:
                self._charging_status = "Dynamic Control Off"
            elif self._grace_started_at is not None and self._allocated_current >= min_charge_current:
                grace_min = get_entry_value(hub_entry, CONF_SOLAR_GRACE_PERIOD, DEFAULT_SOLAR_GRACE_PERIOD)
                elapsed = (datetime.now() - self._grace_started_at).total_seconds()
                remaining = max(0, int(grace_min * 60 - elapsed))
                self._charging_status = f"Grace: {remaining}s"
            elif self._pause_started_at is not None and limit == 0:
                pause_dur_s = get_entry_value(self.config_entry, CONF_CHARGE_PAUSE_DURATION, DEFAULT_CHARGE_PAUSE_DURATION) * 60
                elapsed = (datetime.now() - self._pause_started_at).total_seconds()
                remaining = max(0, int(pause_dur_s - elapsed))
                self._charging_status = f"Paused: {remaining}s"
            elif limit > 0:
                self._charging_status = "Charging"
            else:
                mode = self._operating_mode
                bat_soc = hub_data.get("battery_soc")
                bat_target = hub_data.get("battery_soc_target")
                bat_below_target = (bat_soc is not None and bat_target is not None
                                    and bat_soc < bat_target)
                if mode in (OPERATING_MODE_SOLAR_ONLY, OPERATING_MODE_SOLAR_PRIORITY) and bat_below_target:
                    self._charging_status = "Battery Priority"
                elif mode == OPERATING_MODE_SOLAR_ONLY:
                    self._charging_status = "Insufficient Solar"
                elif mode == OPERATING_MODE_SOLAR_PRIORITY:
                    self._charging_status = "Insufficient Solar"
                elif mode == OPERATING_MODE_EXCESS:
                    self._charging_status = "No Excess"
                else:
                    self._charging_status = "Insufficient Power"

            # Publish charging status for the status sensor to read
            if "charger_status" not in self.hass.data.get(DOMAIN, {}):
                self.hass.data.setdefault(DOMAIN, {})["charger_status"] = {}
            self.hass.data[DOMAIN]["charger_status"][self.config_entry.entry_id] = self._charging_status

            # Send device command
            device_type = self.config_entry.data.get(CONF_DEVICE_TYPE, DEVICE_TYPE_EVSE)
            if device_type == DEVICE_TYPE_PLUG:
                await self._send_plug_command(limit, hub_data, now_mono)
            else:
                await self._send_ocpp_command(limit, hub_entry, dynamic_control_on, now_mono)
        except Exception as e:
            _LOGGER.error(f"Error updating Dynamic OCPP EVSE Charger Sensor {self._attr_name}: {e}", exc_info=True)


class DynamicOcppEvseAllocatedCurrentSensor(ChargerEntityMixin, SensorEntity):
    """Sensor showing the allocated (commanded) current for a charger."""

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


class DynamicOcppEvseChargerStatusSensor(ChargerEntityMixin, SensorEntity):
    """Sensor showing the current charging status reason for a charger."""

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


class DynamicOcppEvseHubSensor(HubEntityMixin, SensorEntity):
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
                self._last_update = hub_data.get("last_update", datetime.now(timezone.utc))
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
        "requires_battery": True,
    },
    {
        "name_suffix": "Current Grid Power",
        "unique_id_suffix": "net_site_consumption",
        "hub_data_key": "grid_power",
        "unit": "W",
        "device_class": "power",
        "icon": "mdi:home-lightning-bolt-outline",
        "decimals": 0,
    },
    {
        "name_suffix": "Current Solar Power",
        "unique_id_suffix": "solar_power",
        "hub_data_key": "solar_power",
        "unit": "W",
        "device_class": "power",
        "icon": "mdi:solar-power-variant",
        "decimals": 0,
    },
    {
        "name_suffix": "Current Battery Power",
        "unique_id_suffix": "battery_power",
        "hub_data_key": "battery_power",
        "unit": "W",
        "device_class": "power",
        "icon": "mdi:battery-charging",
        "decimals": 0,
        "requires_battery": True,
    },
    {
        "name_suffix": "Available Current A",
        "unique_id_suffix": "site_available_current_phase_a",
        "hub_data_key": "available_current_a",
        "unit": "A",
        "device_class": "current",
        "icon": "mdi:current-ac",
        "decimals": 1,
    },
    {
        "name_suffix": "Available Current B",
        "unique_id_suffix": "site_available_current_phase_b",
        "hub_data_key": "available_current_b",
        "unit": "A",
        "device_class": "current",
        "icon": "mdi:current-ac",
        "decimals": 1,
        "requires_phase": "B",
    },
    {
        "name_suffix": "Available Current C",
        "unique_id_suffix": "site_available_current_phase_c",
        "hub_data_key": "available_current_c",
        "unit": "A",
        "device_class": "current",
        "icon": "mdi:current-ac",
        "decimals": 1,
        "requires_phase": "C",
    },
    {
        "name_suffix": "Available Grid Power",
        "unique_id_suffix": "site_grid_available_power",
        "hub_data_key": "available_grid_power",
        "unit": "W",
        "device_class": "power",
        "icon": "mdi:transmission-tower",
        "decimals": 0,
    },
    {
        "name_suffix": "Available Solar Power",
        "unique_id_suffix": "solar_available_power",
        "hub_data_key": "available_solar_power",
        "unit": "W",
        "device_class": "power",
        "icon": "mdi:solar-power",
        "decimals": 0,
    },
    {
        "name_suffix": "Available Battery Power",
        "unique_id_suffix": "battery_available_power",
        "hub_data_key": "available_battery_power",
        "unit": "W",
        "device_class": "power",
        "icon": "mdi:battery-arrow-up",
        "decimals": 0,
        "requires_battery": True,
    },
    {
        "name_suffix": "Total Managed Power",
        "unique_id_suffix": "total_evse_power",
        "hub_data_key": "total_evse_power",
        "unit": "W",
        "device_class": "power",
        "icon": "mdi:ev-station",
        "decimals": 0,
    },
]


class DynamicOcppEvseHubDataSensor(HubEntityMixin, SensorEntity):
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
