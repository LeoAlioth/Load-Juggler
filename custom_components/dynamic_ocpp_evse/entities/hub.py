import logging
from homeassistant.components.sensor import SensorEntity
from datetime import datetime, timezone
from ..engine.hub_calculation import run_hub_calculation
from ..const import *
from ..helpers import get_entry_value
from .mixins import HubEntityMixin

_LOGGER = logging.getLogger(__name__)

# Charger/load sensors refresh hub_data every scan cycle (SCAN_INTERVAL = 10s).
# If hub_data has not been refreshed within this window, no load is driving the
# calculation (e.g. a hub with no loads configured), so the hub sensor must run
# the calculation itself instead of reading stale data.
_HUB_DATA_STALE_SECONDS = 30


class LoadJugglerHubSensor(HubEntityMixin, SensorEntity):
    """Hub-level sensor showing site-wide information."""

    def __init__(self, hass, config_entry, name, entity_id):
        """Initialize the hub sensor."""
        self.hass = hass
        self.config_entry = config_entry
        self._attr_name = f"{name} Site Remaining Power"
        self._attr_unique_id = f"{entity_id}_site_info"
        self._total_site_available_power = None
        self._grid_stale = False
        self._last_update = datetime.min

    @property
    def state(self):
        if self._total_site_available_power is not None:
            return round(self._total_site_available_power, 0)
        return 0.0

    @property
    def extra_state_attributes(self):
        attrs = {
            "state_class": "measurement",
            "last_update": self._last_update,
        }
        if self._grid_stale:
            attrs["grid_stale"] = True
        return attrs

    @property
    def icon(self):
        return "mdi:home-lightning-bolt"

    @property
    def unit_of_measurement(self):
        return "W"

    @property
    def device_class(self):
        return "power"

    async def async_update(self):
        """Update hub sensor with site-wide data from hass.data or by running calculation."""
        try:
            hub_entry_id = self.config_entry.entry_id
            hub_data = (
                self.hass.data.get(DOMAIN, {}).get("hub_data", {}).get(hub_entry_id, {})
            )

            # hub_data is kept fresh by load sensors, which recalculate every
            # scan cycle. When it is missing or stale, no load is driving the
            # calculation (e.g. a hub with no loads), so run it here ourselves.
            now = datetime.now(timezone.utc)
            last_update = hub_data.get("last_update") if hub_data else None
            is_stale = (
                last_update is None
                or (now - last_update).total_seconds() > _HUB_DATA_STALE_SECONDS
            )

            if hub_data and not is_stale:
                self._total_site_available_power = hub_data.get(
                    "total_site_available_power"
                )
                self._grid_stale = hub_data.get("grid_stale", False)
                self._last_update = last_update or now
            else:
                _LOGGER.debug(
                    "No fresh load data for hub %s, running calculation directly",
                    self._attr_name,
                )

                class MockSensor:
                    def __init__(self, hass, hub_entry):
                        self.hass = hass
                        self.hub_entry = hub_entry

                mock_sensor = MockSensor(self.hass, self.config_entry)
                hub_data = run_hub_calculation(mock_sensor)
                hub_data["last_update"] = now
                self._total_site_available_power = hub_data.get(
                    "total_site_available_power"
                )
                self._grid_stale = hub_data.get("grid_stale", False)
                self._last_update = now
                if DOMAIN not in self.hass.data:
                    self.hass.data[DOMAIN] = {}
                if "hub_data" not in self.hass.data[DOMAIN]:
                    self.hass.data[DOMAIN]["hub_data"] = {}
                self.hass.data[DOMAIN]["hub_data"][hub_entry_id] = hub_data
        except Exception as e:
            _LOGGER.error(
                f"Error updating hub sensor {self._attr_name}: {e}", exc_info=True
            )


class LoadJugglerHubStatusSensor(HubEntityMixin, SensorEntity):
    """Hub-level sensor showing site configuration status and warnings."""

    def __init__(self, hass, config_entry, name, entity_id):
        """Initialize the hub status sensor."""
        self.hass = hass
        self.config_entry = config_entry
        self._attr_name = f"{name} Status"
        self._attr_unique_id = f"{entity_id}_hub_status"
        self._state = "Initializing"
        self._warnings = []

    @property
    def state(self):
        return self._state

    @property
    def extra_state_attributes(self):
        attrs = {}
        if self._warnings:
            attrs["warnings"] = self._warnings
        return attrs

    @property
    def icon(self):
        if self._state == "OK":
            return "mdi:check-circle-outline"
        if self._state == "Initializing":
            return "mdi:timer-sand"
        return "mdi:alert-circle-outline"

    async def async_update(self):
        """Read hub status from hass.data."""
        try:
            hub_data = (
                self.hass.data.get(DOMAIN, {})
                .get("hub_data", {})
                .get(self.config_entry.entry_id, {})
            )
            if hub_data:
                self._state = hub_data.get("hub_status", "OK")
                self._warnings = hub_data.get("hub_warnings", [])
        except Exception as e:
            _LOGGER.error(f"Error updating hub status sensor: {e}", exc_info=True)


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
        "name_suffix": "Remaining Current A",
        "unique_id_suffix": "site_available_current_phase_a",
        "hub_data_key": "available_current_a",
        "unit": "A",
        "device_class": "current",
        "icon": "mdi:current-ac",
        "decimals": 1,
    },
    {
        "name_suffix": "Remaining Current B",
        "unique_id_suffix": "site_available_current_phase_b",
        "hub_data_key": "available_current_b",
        "unit": "A",
        "device_class": "current",
        "icon": "mdi:current-ac",
        "decimals": 1,
        "requires_phase": "B",
    },
    {
        "name_suffix": "Remaining Current C",
        "unique_id_suffix": "site_available_current_phase_c",
        "hub_data_key": "available_current_c",
        "unit": "A",
        "device_class": "current",
        "icon": "mdi:current-ac",
        "decimals": 1,
        "requires_phase": "C",
    },
    {
        "name_suffix": "Grid Remaining Power",
        "unique_id_suffix": "site_grid_available_power",
        "hub_data_key": "available_grid_power",
        "unit": "W",
        "device_class": "power",
        "icon": "mdi:transmission-tower",
        "decimals": 0,
    },
    {
        "name_suffix": "Solar Remaining Power",
        "unique_id_suffix": "solar_available_power",
        "hub_data_key": "available_solar_power",
        "unit": "W",
        "device_class": "power",
        "icon": "mdi:solar-power",
        "decimals": 0,
    },
    {
        "name_suffix": "Battery Remaining Power",
        "unique_id_suffix": "battery_available_power",
        "hub_data_key": "available_battery_power",
        "unit": "W",
        "device_class": "power",
        "icon": "mdi:battery-arrow-up",
        "decimals": 0,
        "requires_battery": True,
    },
    {
        "name_suffix": "Current Managed Power",
        "unique_id_suffix": "total_evse_power",
        "hub_data_key": "total_evse_power",
        "unit": "W",
        "device_class": "power",
        "icon": "mdi:ev-station",
        "decimals": 0,
    },
]


class LoadJugglerHubDataSensor(HubEntityMixin, SensorEntity):
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
            hub_data = (
                self.hass.data.get(DOMAIN, {})
                .get("hub_data", {})
                .get(self.config_entry.entry_id, {})
            )
            key = self._defn["hub_data_key"]
            if hub_data and key in hub_data and hub_data[key] is not None:
                self._state = round(float(hub_data[key]), self._defn["decimals"])
        except Exception as e:
            _LOGGER.error(f"Error updating {self._attr_name}: {e}", exc_info=True)
