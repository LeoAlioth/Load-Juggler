import logging
from homeassistant.components.sensor import SensorEntity
from datetime import datetime, timezone
from .dynamic_ocpp_evse import run_hub_calculation
from .const import *
from .helpers import get_entry_value
from .entity_mixins import HubEntityMixin

_LOGGER = logging.getLogger(__name__)


class LoadJugglerHubSensor(HubEntityMixin, SensorEntity):
    """Hub-level sensor showing site-wide information."""

    def __init__(self, hass, config_entry, name, entity_id):
        """Initialize the hub sensor."""
        self.hass = hass
        self.config_entry = config_entry
        self._attr_name = f"{name} Site Available Power"
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

            if hub_data:
                self._total_site_available_power = hub_data.get(
                    "total_site_available_power"
                )
                self._grid_stale = hub_data.get("grid_stale", False)
                self._last_update = hub_data.get(
                    "last_update", datetime.now(timezone.utc)
                )
            else:
                _LOGGER.debug(
                    "No charger data available for hub %s, running calculation directly",
                    self._attr_name,
                )

                class MockSensor:
                    def __init__(self, hass, hub_entry):
                        self.hass = hass
                        self.hub_entry = hub_entry

                mock_sensor = MockSensor(self.hass, self.config_entry)
                hub_data = run_hub_calculation(mock_sensor)
                self._total_site_available_power = hub_data.get(
                    "total_site_available_power"
                )
                self._grid_stale = hub_data.get("grid_stale", False)
                self._last_update = hub_data.get(
                    "last_update", datetime.now(timezone.utc)
                )
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
