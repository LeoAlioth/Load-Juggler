"""Per-inverter entities — sensors on each inverter device.

An inverter config entry is a power source linked to a hub, optionally
carrying its own battery. The engine aggregates the hub's inverter fleet each
cycle and publishes a per-inverter section into hub_data
(``hub_data["inverters"][entry_id]``); these sensors read their own entry's
section, mirroring how circuit-group sensors read ``group_data``.

The charge-control status sensor is also where the write-control loop ticks:
it already runs every scan cycle with the entry in hand, so the writes ride
the same clock as the readings that justify them.
"""

import logging
import time

from homeassistant.components.sensor import SensorEntity

from ..const import *
from ..control.inverter import send_inverter_charge_limit
from ..helpers import get_entry_value
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

    # HA composes the friendly name as "<device name> <entity name>", so
    # renaming the inverter device renames every sensor on it. The unique_id
    # (and therefore the entity_id) is unaffected by a rename.
    _attr_has_entity_name = True

    def __init__(self, hass, config_entry, name, entity_id, defn):
        self.hass = hass
        self.config_entry = config_entry
        self._defn = defn
        self._attr_name = defn["name_suffix"]
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


class LoadJugglerInverterChargeControlSensor(InverterEntityMixin, SensorEntity):
    """Charge write-control status — and the loop that performs the writes.

    States: ``Off`` (switch not armed), ``Not limiting`` (armed, but nothing
    to hold back right now) and ``Limiting to <value>`` while a limit is
    applied. Releasing a limit is a log line rather than a state, since a
    one-cycle status nobody sees is not worth the extra state.

    Driving the writes from a sensor update keeps them on the same cadence as
    the forecast that produces them, and means there is exactly one place that
    talks to the inverter's register.
    """

    _attr_icon = "mdi:battery-clock"
    _attr_has_entity_name = True

    def __init__(self, hass, config_entry, name, entity_id):
        self.hass = hass
        self.config_entry = config_entry
        self._attr_name = "Charge Control"
        self._attr_unique_id = f"{entity_id}_charge_control_status"
        self._state = "Off"

    @property
    def state(self):
        return self._state

    @property
    def extra_state_attributes(self):
        inverter_rt = (
            self.hass.data.get(DOMAIN, {})
            .get("inverters", {})
            .get(self.config_entry.entry_id, {})
        )
        return {
            "target_entity": get_entry_value(
                self.config_entry, CONF_CHARGE_LIMIT_ENTITY_ID, None
            ),
            "applied_value": inverter_rt.get(INVERTER_RT_APPLIED),
            "unit": get_entry_value(
                self.config_entry, CONF_CHARGE_LIMIT_UNIT, DEFAULT_CHARGE_LIMIT_UNIT
            ),
        }

    async def async_update(self):
        try:
            hub_entry_id = self.config_entry.data.get(CONF_HUB_ENTRY_ID)
            hub_data = (
                self.hass.data.get(DOMAIN, {})
                .get("hub_data", {})
                .get(hub_entry_id, {})
            )
            my_data = hub_data.get("inverters", {}).get(self.config_entry.entry_id, {})
            # None both when the forecast is off and when it has released the
            # limit — the control treats them the same way, as "restore".
            advice_w = my_data.get("forecast_charge_limit_w")
            await send_inverter_charge_limit(
                self.hass, self.config_entry, advice_w, time.monotonic()
            )
            inverter_rt = (
                self.hass.data.get(DOMAIN, {})
                .get("inverters", {})
                .get(self.config_entry.entry_id, {})
            )
            self._state = inverter_rt.get(INVERTER_RT_STATUS, "Off")
        except Exception as e:
            _LOGGER.error(
                "Error updating %s: %s", self._attr_name, e, exc_info=True
            )
