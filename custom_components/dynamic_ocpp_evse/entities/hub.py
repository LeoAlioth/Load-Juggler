import logging
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from datetime import datetime, timezone
from ..const import DOMAIN
from ..helpers import get_entry_value
from .mixins import HubEntityMixin, SiteCycleConsumerMixin

_LOGGER = logging.getLogger(__name__)


class LoadJugglerHubSensor(SiteCycleConsumerMixin, HubEntityMixin, SensorEntity):
    """Hub-level sensor showing site-wide information.

    The hub's own coordinator (sensor.py) runs the site calculation once per
    site_update_frequency and republishes hub_data — with no loads configured
    too — and this sensor is a pure reader of that result, pushed by the
    coordinator. It never runs the engine itself: a second writer produced a
    differently-shaped hub_data and double-advanced the engine's cycle-counted
    state.
    """

    _attr_native_unit_of_measurement = "W"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:home-lightning-bolt"

    def __init__(self, hass, config_entry, name, entity_id):
        """Initialize the hub sensor."""
        self._init_entity(
            hass,
            config_entry,
            f"{name} Site Remaining Power",
            f"{entity_id}_site_info",
        )
        self._total_site_available_power = None
        self._grid_stale = False
        self._last_update = datetime.min

    @property
    def native_value(self):
        """Remaining site power in W, or None when the engine published none.

        Deliberately NOT 0.0 for "no value": 0 W of remaining power is a real
        and alarming reading (the site is at its limit), and reporting it when
        the truth is "nothing has been calculated yet" is a lie that both a
        dashboard and an automation act on. Unknown says unknown — and the
        freshness gate on `available` covers the case where the producer has
        stopped rather than never started.
        """
        if self._total_site_available_power is None:
            return None
        return round(self._total_site_available_power, 0)

    @property
    def extra_state_attributes(self):
        attrs = {"last_update": self._last_update}
        if self._grid_stale:
            attrs["grid_stale"] = True
        return attrs

    def _read_site_data(self):
        hub_data = self._hub_data()
        if not hub_data:
            return
        self._total_site_available_power = hub_data.get("total_site_available_power")
        self._grid_stale = hub_data.get("grid_stale", False)
        self._last_update = hub_data.get("last_update") or datetime.now(timezone.utc)


class LoadJugglerHubStatusSensor(
    SiteCycleConsumerMixin, HubEntityMixin, SensorEntity
):
    """Hub-level sensor showing site configuration status and warnings.

    A text sensor: no unit, no device_class, and no state_class. HA validates
    those against a numeric state and logs an error for every update of a
    sensor that claims to be a measurement while publishing "OK".
    """

    def __init__(self, hass, config_entry, name, entity_id):
        """Initialize the hub status sensor."""
        self._init_entity(
            hass, config_entry, f"{name} Status", f"{entity_id}_hub_status"
        )
        self._attr_native_value = "Initializing"
        self._attr_icon = "mdi:timer-sand"
        self._warnings = []

    @property
    def extra_state_attributes(self):
        attrs = {}
        if self._warnings:
            attrs["warnings"] = self._warnings
        return attrs

    def _read_site_data(self):
        hub_data = self._hub_data()
        if not hub_data:
            return
        self._attr_native_value = hub_data.get("hub_status", "OK")
        self._warnings = hub_data.get("hub_warnings", [])
        self._attr_icon = (
            "mdi:check-circle-outline"
            if self._attr_native_value == "OK"
            else "mdi:alert-circle-outline"
        )


HUB_SENSOR_DEFINITIONS = [
    {
        "name_suffix": "Battery SOC",
        "unique_id_suffix": "battery_soc",
        "hub_data_key": "battery_soc",
        "unit": "%",
        "device_class": SensorDeviceClass.BATTERY,
        "icon": "mdi:battery-80",
        "decimals": 1,
        "requires_battery": True,
    },
    {
        "name_suffix": "Current Grid Power",
        "unique_id_suffix": "net_site_consumption",
        "hub_data_key": "grid_power",
        "unit": "W",
        "device_class": SensorDeviceClass.POWER,
        "icon": "mdi:home-lightning-bolt-outline",
        "decimals": 0,
    },
    {
        "name_suffix": "Current Solar Power",
        "unique_id_suffix": "solar_power",
        "hub_data_key": "solar_power",
        "unit": "W",
        "device_class": SensorDeviceClass.POWER,
        "icon": "mdi:solar-power-variant",
        "decimals": 0,
    },
    {
        "name_suffix": "Current Battery Power",
        "unique_id_suffix": "battery_power",
        "hub_data_key": "battery_power",
        "unit": "W",
        "device_class": SensorDeviceClass.POWER,
        "icon": "mdi:battery-charging",
        "decimals": 0,
        "requires_battery": True,
    },
    {
        "name_suffix": "Remaining Current A",
        "unique_id_suffix": "site_available_current_phase_a",
        "hub_data_key": "available_current_a",
        "unit": "A",
        "device_class": SensorDeviceClass.CURRENT,
        "icon": "mdi:current-ac",
        "decimals": 1,
    },
    {
        "name_suffix": "Remaining Current B",
        "unique_id_suffix": "site_available_current_phase_b",
        "hub_data_key": "available_current_b",
        "unit": "A",
        "device_class": SensorDeviceClass.CURRENT,
        "icon": "mdi:current-ac",
        "decimals": 1,
        "requires_phase": "B",
    },
    {
        "name_suffix": "Remaining Current C",
        "unique_id_suffix": "site_available_current_phase_c",
        "hub_data_key": "available_current_c",
        "unit": "A",
        "device_class": SensorDeviceClass.CURRENT,
        "icon": "mdi:current-ac",
        "decimals": 1,
        "requires_phase": "C",
    },
    {
        "name_suffix": "Grid Remaining Current",
        "unique_id_suffix": "site_grid_available_current",
        "hub_data_key": "available_grid_current",
        "unit": "A",
        "device_class": SensorDeviceClass.CURRENT,
        "icon": "mdi:transmission-tower",
        "decimals": 1,
    },
    {
        "name_suffix": "Solar Remaining Current",
        "unique_id_suffix": "site_solar_available_current",
        "hub_data_key": "available_solar_current",
        "unit": "A",
        "device_class": SensorDeviceClass.CURRENT,
        "icon": "mdi:solar-power",
        "decimals": 1,
    },
    {
        "name_suffix": "Battery Remaining Current",
        "unique_id_suffix": "site_battery_available_current",
        "hub_data_key": "available_battery_current",
        "unit": "A",
        "device_class": SensorDeviceClass.CURRENT,
        "icon": "mdi:battery-arrow-up",
        "decimals": 1,
        "requires_battery": True,
    },
    {
        "name_suffix": "Inverter Remaining Current",
        "unique_id_suffix": "site_inverter_available_current",
        "hub_data_key": "available_inverter_current",
        "unit": "A",
        "device_class": SensorDeviceClass.CURRENT,
        "icon": "mdi:flash",
        "decimals": 1,
    },
    {
        "name_suffix": "Grid Remaining Power",
        "unique_id_suffix": "site_grid_available_power",
        "hub_data_key": "available_grid_power",
        "unit": "W",
        "device_class": SensorDeviceClass.POWER,
        "icon": "mdi:transmission-tower",
        "decimals": 0,
    },
    {
        "name_suffix": "Solar Remaining Power",
        "unique_id_suffix": "solar_available_power",
        "hub_data_key": "available_solar_power",
        "unit": "W",
        "device_class": SensorDeviceClass.POWER,
        "icon": "mdi:solar-power",
        "decimals": 0,
    },
    {
        "name_suffix": "Battery Remaining Power",
        "unique_id_suffix": "battery_available_power",
        "hub_data_key": "available_battery_power",
        "unit": "W",
        "device_class": SensorDeviceClass.POWER,
        "icon": "mdi:battery-arrow-up",
        "decimals": 0,
        "requires_battery": True,
    },
    {
        "name_suffix": "Current Managed Power",
        "unique_id_suffix": "total_evse_power",
        "hub_data_key": "total_evse_power",
        "unit": "W",
        "device_class": SensorDeviceClass.POWER,
        "icon": "mdi:ev-station",
        "decimals": 0,
    },
    # PV clipping forecast — advisory battery headroom. The kWh sensors carry
    # device_class ENERGY with state_class TOTAL (developer decision,
    # 2026-08-17): HA rejects ENERGY + MEASUREMENT, and TOTAL is the class for
    # "an amount that can both increase and decrease" — which is exactly what
    # a remaining-today advisory figure does. TOTAL keeps them out of the
    # energy dashboard's metered pipeline (no last_reset, nothing accumulates).
    {
        "name_suffix": "Forecast Clippable Energy",
        "unique_id_suffix": "forecast_clippable_energy",
        "hub_data_key": "forecast_clipped_kwh",
        "unit": "kWh",
        "device_class": SensorDeviceClass.ENERGY,
        "state_class": SensorStateClass.TOTAL,
        "icon": "mdi:content-cut",
        "decimals": 2,
        "requires_forecast": True,
    },
    {
        "name_suffix": "Forecast Storable Energy",
        "unique_id_suffix": "forecast_storable_energy",
        "hub_data_key": "forecast_absorbable_kwh",
        "unit": "kWh",
        "device_class": SensorDeviceClass.ENERGY,
        "state_class": SensorStateClass.TOTAL,
        "icon": "mdi:battery-plus-variant",
        "decimals": 2,
        "requires_forecast": True,
    },
    {
        "name_suffix": "Battery Headroom Deficit",
        "unique_id_suffix": "forecast_headroom_deficit",
        "hub_data_key": "forecast_headroom_deficit_kwh",
        "unit": "kWh",
        "device_class": SensorDeviceClass.ENERGY,
        "state_class": SensorStateClass.TOTAL,
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


class LoadJugglerHubDataSensor(
    SiteCycleConsumerMixin, HubEntityMixin, SensorEntity
):
    """Generic hub data sensor driven by a definition dict.

    Every one of these is a numeric reading. W/A/% sensors carry state_class
    MEASUREMENT (the default here); the advisory kWh sensors override it to
    ENERGY + TOTAL per their definitions — HA rejects ENERGY + MEASUREMENT,
    and TOTAL is the class for an amount that rises and falls.
    """

    def __init__(self, hass, config_entry, name, entity_id, defn):
        self._init_entity(
            hass,
            config_entry,
            f"{name} {defn['name_suffix']}",
            f"{entity_id}_{defn['unique_id_suffix']}",
        )
        self._defn = defn
        self._attr_native_unit_of_measurement = defn["unit"]
        self._attr_device_class = defn["device_class"]
        self._attr_state_class = defn.get(
            "state_class", SensorStateClass.MEASUREMENT
        )
        self._attr_icon = defn["icon"]
        self._attr_native_value = None

    def _read_site_data(self):
        """Publish this cycle's figure, or unknown when there isn't one.

        A key that arrives as None means the producer ran and reported that it
        has no measurement — a sensor unreadable with nothing to hold, so the
        engine substituted a safety value internally and refuses to publish it
        (see engine/hub_result.py). Clearing the value is what turns that into
        `unknown`; HOLDING the last one would freeze a stale reading that looks
        live, which for a figure like solar production is exactly the lie the
        substitution was suppressed to avoid.

        Before the first cycle there is no hub_data at all and nothing to
        report either way; availability (SiteFreshnessMixin) is the separate
        question of whether the producer is still running.
        """
        hub_data = self._hub_data()
        if not hub_data:
            return
        value = hub_data.get(self._defn["hub_data_key"])
        self._attr_native_value = (
            None if value is None else round(float(value), self._defn["decimals"])
        )
