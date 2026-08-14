"""Per-inverter entities — sensors on each inverter device.

An inverter config entry is a power source linked to a hub, optionally
carrying its own battery. The engine aggregates the hub's inverter fleet each
cycle and publishes a per-inverter section into hub_data
(``hub_data["inverters"][entry_id]``); these sensors read their own entry's
section, mirroring how circuit-group sensors read ``group_data``.
"""

import logging

from homeassistant.components.sensor import SensorEntity

from ..const import *
from .mixins import InverterEntityMixin

_LOGGER = logging.getLogger(__name__)

INVERTER_SENSOR_DEFINITIONS = [
    {
        "name_suffix": "Solar Production",
        "unique_id_suffix": "solar_production",
        "data_key": "solar_w",
        "unit": "W",
        "device_class": "power",
        "icon": "mdi:solar-power-variant",
        "decimals": 0,
    },
    {
        "name_suffix": "Battery SOC",
        "unique_id_suffix": "battery_soc",
        "data_key": "battery_soc",
        "unit": "%",
        "device_class": "battery",
        "icon": "mdi:battery-80",
        "decimals": 1,
        "requires_battery": True,
    },
    {
        "name_suffix": "Battery Power",
        "unique_id_suffix": "battery_power",
        "data_key": "battery_power",
        "unit": "W",
        "device_class": "power",
        "icon": "mdi:battery-charging",
        "decimals": 0,
        "requires_battery": True,
    },
    # PV clipping forecast advice for THIS battery: the fleet-uniform SOC
    # ceiling, and this battery's share of the fleet charge limit (split by
    # charge cap). The future write-control pushes these to the inverter.
    {
        "name_suffix": "Recommended Battery Max SOC",
        "unique_id_suffix": "forecast_battery_max_soc",
        "data_key": "forecast_battery_max_soc",
        "unit": "%",
        "device_class": "battery",
        "icon": "mdi:battery-lock",
        "decimals": 0,
        "requires_forecast": True,
    },
    {
        "name_suffix": "Recommended Battery Charge Limit",
        "unique_id_suffix": "forecast_charge_limit",
        "data_key": "forecast_charge_limit_w",
        "unit": "W",
        "device_class": "power",
        "icon": "mdi:battery-charging-wireless",
        "decimals": 0,
        "requires_forecast": True,
    },
]


class LoadJugglerInverterDataSensor(InverterEntityMixin, SensorEntity):
    """Generic per-inverter data sensor driven by a definition dict."""

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
            hub_entry_id = self.config_entry.data.get(CONF_HUB_ENTRY_ID)
            hub_data = (
                self.hass.data.get(DOMAIN, {})
                .get("hub_data", {})
                .get(hub_entry_id, {})
            )
            my_data = hub_data.get("inverters", {}).get(self.config_entry.entry_id)
            if not my_data:
                return
            value = my_data.get(self._defn["data_key"])
            if value is not None:
                self._state = round(float(value), self._defn["decimals"])
        except Exception as e:
            _LOGGER.error(f"Error updating {self._attr_name}: {e}", exc_info=True)
