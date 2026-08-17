import logging
from homeassistant.components.sensor import SensorEntity
from datetime import datetime, timezone
from ..const import *
from ..helpers import get_entry_value
from .mixins import HubEntityMixin

_LOGGER = logging.getLogger(__name__)


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
        """Read site-wide data from hass.data.

        The hub's own coordinator (sensor.py) runs the site calculation once
        per site_update_frequency and republishes hub_data — with no loads
        configured too — so this is a pure read. It never runs the engine
        itself: a second writer produced a differently-shaped hub_data and
        double-advanced the engine's cycle-counted state.
        """
        try:
            hub_entry_id = self.config_entry.entry_id
            hub_data = (
                self.hass.data.get(DOMAIN, {}).get("hub_data", {}).get(hub_entry_id, {})
            )
            if not hub_data:
                return

            self._total_site_available_power = hub_data.get(
                "total_site_available_power"
            )
            self._grid_stale = hub_data.get("grid_stale", False)
            self._last_update = hub_data.get("last_update") or datetime.now(
                timezone.utc
            )
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
        "name_suffix": "Grid Remaining Current",
        "unique_id_suffix": "site_grid_available_current",
        "hub_data_key": "available_grid_current",
        "unit": "A",
        "device_class": "current",
        "icon": "mdi:transmission-tower",
        "decimals": 1,
    },
    {
        "name_suffix": "Solar Remaining Current",
        "unique_id_suffix": "site_solar_available_current",
        "hub_data_key": "available_solar_current",
        "unit": "A",
        "device_class": "current",
        "icon": "mdi:solar-power",
        "decimals": 1,
    },
    {
        "name_suffix": "Battery Remaining Current",
        "unique_id_suffix": "site_battery_available_current",
        "hub_data_key": "available_battery_current",
        "unit": "A",
        "device_class": "current",
        "icon": "mdi:battery-arrow-up",
        "decimals": 1,
        "requires_battery": True,
    },
    {
        "name_suffix": "Inverter Remaining Current",
        "unique_id_suffix": "site_inverter_available_current",
        "hub_data_key": "available_inverter_current",
        "unit": "A",
        "device_class": "current",
        "icon": "mdi:flash",
        "decimals": 1,
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
    # PV clipping forecast — advisory battery headroom. The kWh sensors get no
    # device_class: "energy" implies total/total_increasing statistics, and
    # these are advisory values that fall as well as rise.
    {
        "name_suffix": "Forecast Clippable Energy",
        "unique_id_suffix": "forecast_clippable_energy",
        "hub_data_key": "forecast_clipped_kwh",
        "unit": "kWh",
        "device_class": None,
        "icon": "mdi:content-cut",
        "decimals": 2,
        "requires_forecast": True,
    },
    {
        "name_suffix": "Forecast Storable Energy",
        "unique_id_suffix": "forecast_storable_energy",
        "hub_data_key": "forecast_absorbable_kwh",
        "unit": "kWh",
        "device_class": None,
        "icon": "mdi:battery-plus-variant",
        "decimals": 2,
        "requires_forecast": True,
    },
    {
        "name_suffix": "Battery Headroom Deficit",
        "unique_id_suffix": "forecast_headroom_deficit",
        "hub_data_key": "forecast_headroom_deficit_kwh",
        "unit": "kWh",
        "device_class": None,
        "icon": "mdi:battery-alert",
        "decimals": 2,
        "requires_forecast": True,
    },
    # The recommended max-SOC and charge-limit sensors live on the inverter
    # entries (entities/inverter.py) — the advice is per battery, and that is
    # where the future write-control will act. The fleet values remain in
    # hub_data (forecast_battery_max_soc / forecast_charge_limit_w) for
    # automations.
]


# Every hub_data key republished into hass.data for the hub entities (and the
# config-flow Overview page) to read. Projected from HUB_SENSOR_DEFINITIONS so
# a new hub sensor republishes automatically, plus keys read by non-sensor
# consumers.
_HUB_REPUBLISH_KEYS = frozenset(
    d["hub_data_key"] for d in HUB_SENSOR_DEFINITIONS
) | {
    "battery_soc_min",
    "battery_soc_target",
    "total_site_available_power",
    "total_export_power",
    "excess_available",
    "excess_margin_power",
    "inverters",
    # Unmanaged (household) draw — no hub sensor (yet); read by the Overview
    # options page and available to automations.
    "household_power",
    # Fleet forecast advice — no hub sensor anymore (the per-battery sensors
    # live on the inverter entries) but kept in hub_data for automations.
    "forecast_battery_max_soc",
    "forecast_charge_limit_w",
}


def publish_hub_data(hass, hub_entry_id, hub_data):
    """Store the trimmed site result in hass.data for the hub's consumers.

    The single writer is the hub coordinator (sensor.py), once per site cycle.
    Returns the published dict.
    """
    domain_data = hass.data.setdefault(DOMAIN, {})
    published = {key: hub_data.get(key) for key in _HUB_REPUBLISH_KEYS}
    published.update(
        {
            "last_update": datetime.now(timezone.utc),
            "grid_stale": hub_data.get("grid_stale", False),
            "group_data": hub_data.get("group_data", {}),
            "hub_status": hub_data.get("hub_status", "OK"),
            "hub_warnings": hub_data.get("hub_warnings", []),
        }
    )
    domain_data.setdefault("hub_data", {})[hub_entry_id] = published
    return published


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
