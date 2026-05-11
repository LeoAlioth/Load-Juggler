import logging
import time
from homeassistant.components.sensor import SensorEntity
from datetime import datetime, timezone
from .dynamic_ocpp_evse import run_hub_calculation
from .const import *
from .helpers import get_entry_value
from .entity_mixins import ChargerEntityMixin
from . import get_hub_for_charger
from .smoothing import apply_smoothing
from .status import determine_charging_status
from .compliance import check_profile_compliance
from .ocpp import send_ocpp_command
from .plug import send_plug_command

_LOGGER = logging.getLogger(__name__)


class LoadJugglerDeviceSensor(ChargerEntityMixin, SensorEntity):
    """Representation of a managed device (EVSE, smart plug, etc.)."""

    def __init__(self, hass, config_entry, hub_entry, name, entity_id, coordinator):
        """Initialize the sensor."""
        self.hass = hass
        self.config_entry = config_entry
        self.hub_entry = hub_entry
        self._attr_name = f"{name} Available Current"
        self._attr_unique_id = f"{entity_id}_available_current"
        charger_entity_id = config_entry.data.get(CONF_ENTITY_ID)
        self._connector_status_entity = f"sensor.{charger_entity_id}_status_connector"
        self._charge_control_entity = f"switch.{charger_entity_id}_charge_control"
        self._state = None
        self._phases = None
        self._car_active_phases = None
        self._detected_phases = None
        self._operating_mode = None
        self._calc_used = None
        self._allocated_current = None
        self._available_current = None
        self._last_update = datetime.min
        self._pause_started_at = None
        self._grace_started_at = None
        self._prev_operating_mode = None
        self._prev_distribution_mode = None
        self._last_set_current = 0
        self._last_set_power = None
        self._ema_current = None
        self._schmitt_current = None
        self._schmitt_state = "rising"
        self._rate_limited_current = None
        self._last_commanded_limit = None
        self._last_command_time: float = -float("inf")
        self._mismatch_count = 0
        self._last_auto_reset_at = None
        self._profile_reset_count = 0
        self._last_hard_reset_at = None
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
            pause_duration_min = get_entry_value(
                self.config_entry,
                CONF_CHARGE_PAUSE_DURATION,
                DEFAULT_CHARGE_PAUSE_DURATION,
            )
            elapsed = (datetime.now() - self._pause_started_at).total_seconds()
            pause_remaining = max(0, round(pause_duration_min * 60 - elapsed))

        grace_remaining = None
        if self._grace_started_at is not None:
            grace_period_min = get_entry_value(
                self.config_entry, CONF_SOLAR_GRACE_PERIOD, DEFAULT_SOLAR_GRACE_PERIOD
            )
            elapsed = (datetime.now() - self._grace_started_at).total_seconds()
            grace_remaining = max(0, round(grace_period_min * 60 - elapsed))

        attrs = {
            "state_class": "measurement",
            CONF_PHASES: self._phases,
            "detected_phases": self._detected_phases,
            "allocated_current": self._allocated_current,
            "last_update": self._last_update,
            "pause_active": self._pause_started_at is not None,
            "pause_remaining_seconds": pause_remaining,
            "grace_active": self._grace_started_at is not None,
            "grace_remaining_seconds": grace_remaining,
            "last_set_current": self._last_set_current,
            "last_set_power": self._last_set_power,
            "charger_priority": get_entry_value(
                self.config_entry, CONF_CHARGER_PRIORITY, DEFAULT_CHARGER_PRIORITY
            ),
            "hub_entry_id": self.config_entry.data.get(CONF_HUB_ENTRY_ID),
            "auto_reset_mismatch_count": self._mismatch_count,
            "last_auto_reset": self._last_auto_reset_at,
            "profile_reset_count": self._profile_reset_count,
            "last_hard_reset": self._last_hard_reset_at,
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
            hub_entry = get_hub_for_charger(self.hass, self.config_entry.entry_id)
            if not hub_entry:
                _LOGGER.error("Hub not found for charger: %s", self._attr_name)
                return

            self.hub_entry = hub_entry
            hub_data = run_hub_calculation(self)

            for notif in hub_data.get("auto_detect_notifications", []):
                await self.hass.services.async_call(
                    "persistent_notification",
                    "create",
                    {
                        "title": notif["title"],
                        "message": notif["message"],
                        "notification_id": notif["notification_id"],
                    },
                )
                _LOGGER.warning("AutoDetect notification: %s", notif["notification_id"])

            self._phases = hub_data.get(CONF_PHASES)
            charger_active_phases = hub_data.get("charger_active_phases", {})
            self._car_active_phases = charger_active_phases.get(
                self.config_entry.entry_id,
                self._phases or 1,
            )
            current_distribution_mode = hub_data.get("distribution_mode")

            charger_modes = hub_data.get("charger_modes", {})
            self._operating_mode = charger_modes.get(self.config_entry.entry_id)

            mode_changed = (
                self._prev_operating_mode is not None
                and self._operating_mode != self._prev_operating_mode
            ) or (
                self._prev_distribution_mode is not None
                and current_distribution_mode != self._prev_distribution_mode
            )

            if mode_changed:
                if self._pause_started_at is not None:
                    _LOGGER.info(
                        "Mode changed for %s (operating: %s→%s, distribution: %s→%s) — cancelling charge pause",
                        self._attr_name,
                        self._prev_operating_mode,
                        self._operating_mode,
                        self._prev_distribution_mode,
                        current_distribution_mode,
                    )
                    self._pause_started_at = None
                if self._grace_started_at is not None:
                    _LOGGER.info(
                        "Mode changed for %s — cancelling grace timer", self._attr_name
                    )
                    self._grace_started_at = None

            self._prev_operating_mode = self._operating_mode
            self._prev_distribution_mode = current_distribution_mode

            self._calc_used = hub_data.get("calc_used")

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
                "available_current_a": hub_data.get("available_current_a"),
                "available_current_b": hub_data.get("available_current_b"),
                "available_current_c": hub_data.get("available_current_c"),
                "grid_power": hub_data.get("grid_power"),
                "solar_power": hub_data.get("solar_power"),
                "available_grid_power": hub_data.get("available_grid_power"),
                "available_solar_power": hub_data.get("available_solar_power"),
                "available_battery_power": hub_data.get("available_battery_power"),
                "total_site_available_power": hub_data.get(
                    "total_site_available_power"
                ),
                "total_evse_power": hub_data.get("total_evse_power"),
                "last_update": datetime.now(timezone.utc),
                "grid_stale": hub_data.get("grid_stale", False),
                "group_data": hub_data.get("group_data", {}),
                "hub_status": hub_data.get("hub_status", "OK"),
                "hub_warnings": hub_data.get("hub_warnings", []),
            }

            charger_targets = hub_data.get("charger_targets", {})

            if charger_targets:
                charger_names = hub_data.get("charger_names", {})
                charger_modes = hub_data.get("charger_modes", {})
                charger_avail = hub_data.get("charger_available", {})
                _LOGGER.debug(
                    "Charger targets: %s",
                    ", ".join(
                        [
                            f"{charger_names.get(k, k[-8:])}({charger_modes.get(k, '?')}): "
                            f"alloc={v:.1f}A avail={charger_avail.get(k, 0):.1f}A"
                            for k, v in charger_targets.items()
                        ]
                    ),
                )

            raw_allocated = round(charger_targets.get(self.config_entry.entry_id, 0), 1)

            charger_avail_data = hub_data.get("charger_available", {})
            self._available_current = round(
                charger_avail_data.get(self.config_entry.entry_id, 0), 1
            )

            self._allocated_current = apply_smoothing(
                self, raw_allocated, mode_changed, hub_entry
            )
            self._state = self._available_current

            min_charge_current = get_entry_value(
                self.config_entry,
                CONF_EVSE_MINIMUM_CHARGE_CURRENT,
                DEFAULT_MIN_CHARGE_CURRENT,
            )
            grace_period_minutes = get_entry_value(
                self.config_entry, CONF_SOLAR_GRACE_PERIOD, DEFAULT_SOLAR_GRACE_PERIOD
            )
            grace_period_seconds = grace_period_minutes * 60

            if (
                self._operating_mode
                in (OPERATING_MODE_SOLAR_ONLY, OPERATING_MODE_EXCESS)
                and grace_period_seconds > 0
            ):
                if self._allocated_current < min_charge_current:
                    charger_avail = hub_data.get("charger_available", {})
                    physical_available = charger_avail.get(
                        self.config_entry.entry_id, 0
                    )
                    if physical_available >= min_charge_current:
                        if self._grace_started_at is None:
                            self._grace_started_at = datetime.now()
                            _LOGGER.debug(
                                "Grace timer started for %s (mode=%s, grace=%dm)",
                                self._attr_name,
                                self._operating_mode,
                                grace_period_minutes,
                            )
                        elapsed = (
                            datetime.now() - self._grace_started_at
                        ).total_seconds()
                        if elapsed < grace_period_seconds:
                            self._allocated_current = float(min_charge_current)
                        else:
                            _LOGGER.info(
                                "Grace timer expired for %s after %dm — allowing pause",
                                self._attr_name,
                                grace_period_minutes,
                            )
                            self._grace_started_at = None
                    else:
                        if self._grace_started_at is not None:
                            _LOGGER.info(
                                "Site limit violation for %s — cancelling grace timer",
                                self._attr_name,
                            )
                            self._grace_started_at = None
                else:
                    if self._grace_started_at is not None:
                        _LOGGER.debug(
                            "Grace timer reset for %s — conditions recovered",
                            self._attr_name,
                        )
                        self._grace_started_at = None
            else:
                if self._grace_started_at is not None:
                    self._grace_started_at = None

            if DOMAIN not in self.hass.data:
                self.hass.data[DOMAIN] = {}
            if "charger_allocations" not in self.hass.data[DOMAIN]:
                self.hass.data[DOMAIN]["charger_allocations"] = {}
            self.hass.data[DOMAIN]["charger_allocations"][
                self.config_entry.entry_id
            ] = self._allocated_current

            command_interval = get_entry_value(
                self.config_entry, CONF_UPDATE_FREQUENCY, DEFAULT_UPDATE_FREQUENCY
            )
            now_mono = time.monotonic()
            if now_mono - self._last_command_time < command_interval:
                _LOGGER.debug(
                    "Site refresh for %s (command send in %.0fs)",
                    self._attr_name,
                    command_interval - (now_mono - self._last_command_time),
                )
                return

            min_charge_current = get_entry_value(
                self.config_entry,
                CONF_EVSE_MINIMUM_CHARGE_CURRENT,
                DEFAULT_MIN_CHARGE_CURRENT,
            )
            max_charge_current = get_entry_value(
                self.config_entry,
                CONF_EVSE_MAXIMUM_CHARGE_CURRENT,
                DEFAULT_MAX_CHARGE_CURRENT,
            )

            charger_rt = (
                self.hass.data.get(DOMAIN, {})
                .get("chargers", {})
                .get(self.config_entry.entry_id, {})
            )
            dynamic_control_on = charger_rt.get("dynamic_control", True)

            if not dynamic_control_on:
                limit = round(float(max_charge_current), 1)
                self._pause_started_at = None
                _LOGGER.debug(
                    "Dynamic control OFF for %s — using max current %sA",
                    self._attr_name,
                    limit,
                )
            elif self._allocated_current < min_charge_current:
                pause_duration_s = (
                    get_entry_value(
                        self.config_entry,
                        CONF_CHARGE_PAUSE_DURATION,
                        DEFAULT_CHARGE_PAUSE_DURATION,
                    )
                    * 60
                )
                if self._pause_started_at is None:
                    self._pause_started_at = datetime.now()
                    _LOGGER.debug("Charge pause started for %s", self._attr_name)
                limit = 0
            else:
                pause_duration_s = (
                    get_entry_value(
                        self.config_entry,
                        CONF_CHARGE_PAUSE_DURATION,
                        DEFAULT_CHARGE_PAUSE_DURATION,
                    )
                    * 60
                )
                if self._pause_started_at is not None:
                    elapsed = (datetime.now() - self._pause_started_at).total_seconds()
                    if elapsed < pause_duration_s:
                        limit = 0
                    else:
                        self._pause_started_at = None
                        limit = round(self._allocated_current, 1)
                else:
                    limit = round(self._allocated_current, 1)

            connector_state = self.hass.states.get(self._connector_status_entity)
            connector_status = connector_state.state if connector_state else "unknown"

            self._charging_status = determine_charging_status(
                self,
                hub_data,
                limit,
                connector_status,
                dynamic_control_on,
                min_charge_current,
            )

            if "charger_status" not in self.hass.data.get(DOMAIN, {}):
                self.hass.data.setdefault(DOMAIN, {})["charger_status"] = {}
            self.hass.data[DOMAIN]["charger_status"][self.config_entry.entry_id] = (
                self._charging_status
            )

            device_type = self.config_entry.data.get(CONF_DEVICE_TYPE, DEVICE_TYPE_EVSE)
            if device_type == DEVICE_TYPE_PLUG:
                await send_plug_command(self, limit, hub_data, now_mono)
            else:
                await check_profile_compliance(self, limit, dynamic_control_on)
                await send_ocpp_command(
                    self, limit, hub_entry, dynamic_control_on, now_mono
                )
        except Exception as e:
            _LOGGER.error(
                f"Error updating Load Juggler Charger Sensor {self._attr_name}: {e}",
                exc_info=True,
            )
