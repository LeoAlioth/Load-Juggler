"""Per-inverter entities — sensors on each inverter device.

An inverter config entry is a power source linked to a hub, optionally
carrying its own battery. The engine aggregates the hub's inverter fleet each
cycle and publishes a per-inverter section into hub_data
(``hub_data["inverters"][entry_id]``); these sensors read their own entry's
section, mirroring how circuit-group sensors read ``group_data``.

The charge-control status sensor is also where the write-control loop ticks:
it runs once per site cycle with the entry in hand, so the writes ride the same
clock as the readings that justify them.
"""

import logging
import time

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)

from ..const import *
from ..control.inverter import send_inverter_charge_limit
from ..helpers import get_entry_value
from .mixins import (
    InverterEntityMixin,
    SiteCycleConsumerMixin,
    SiteCycleWorkerMixin,
)

_LOGGER = logging.getLogger(__name__)

INVERTER_SENSOR_DEFINITIONS = [
    {
        "name_suffix": "Solar Production",
        "unique_id_suffix": "solar_production",
        "data_key": "solar_w",
        "unit": "W",
        "device_class": SensorDeviceClass.POWER,
        "icon": "mdi:solar-power-variant",
        "decimals": 0,
    },
    {
        "name_suffix": "Battery SOC",
        "unique_id_suffix": "battery_soc",
        "data_key": "battery_soc",
        "unit": "%",
        "device_class": SensorDeviceClass.BATTERY,
        "icon": "mdi:battery-80",
        "decimals": 1,
        "requires_battery": True,
    },
    {
        "name_suffix": "Battery Power",
        "unique_id_suffix": "battery_power",
        "data_key": "battery_power",
        "unit": "W",
        "device_class": SensorDeviceClass.POWER,
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
        "device_class": SensorDeviceClass.BATTERY,
        "icon": "mdi:battery-lock",
        "decimals": 0,
        "requires_forecast": True,
    },
    {
        "name_suffix": "Recommended Battery Charge Limit",
        "unique_id_suffix": "forecast_charge_limit",
        "data_key": "forecast_charge_limit_w",
        "unit": "W",
        "device_class": SensorDeviceClass.POWER,
        "icon": "mdi:battery-charging-wireless",
        "decimals": 0,
        "requires_forecast": True,
    },
]


class LoadJugglerInverterDataSensor(
    SiteCycleConsumerMixin, InverterEntityMixin, SensorEntity
):
    """Generic per-inverter data sensor driven by a definition dict.

    A pure reader of the hub's published fleet aggregate, so it is pushed by
    the hub coordinator and available only while that publication is fresh.
    All of these are numeric instantaneous readings (W, %) — measurement.
    """

    # HA composes the friendly name as "<device name> <entity name>", so
    # renaming the inverter device renames every sensor on it. The unique_id
    # (and therefore the entity_id) is unaffected by a rename.
    _attr_has_entity_name = True
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, hass, config_entry, name, entity_id, defn):
        self._init_entity(
            hass,
            config_entry,
            defn["name_suffix"],
            f"{entity_id}_{defn['unique_id_suffix']}",
        )
        self._defn = defn
        self._attr_native_unit_of_measurement = defn["unit"]
        self._attr_device_class = defn["device_class"]
        self._attr_icon = defn["icon"]
        self._attr_native_value = None

    def _read_site_data(self):
        value = self._my_inverter_data().get(self._defn["data_key"])
        if value is not None:
            self._attr_native_value = round(float(value), self._defn["decimals"])


class LoadJugglerInverterChargeControlSensor(
    SiteCycleWorkerMixin, InverterEntityMixin, SensorEntity
):
    """Charge write-control status — and the loop that performs the writes.

    States: ``Off`` (switch not armed), ``Not limiting`` (armed, but nothing
    to hold back right now) and ``Limiting to <value>`` while a limit is
    applied. Releasing a limit is a log line rather than a state, since a
    one-cycle status nobody sees is not worth the extra state.

    Its per-cycle work is an AWAIT — a Modbus register write — not a read, so it
    joins the site cycle as a *worker* rather than as a coordinator listener
    (see SiteCycleWorkerMixin). That keeps the writes on the same clock as the
    forecast that produces them, keeps exactly one place talking to the
    inverter's register, and gets the serialization the old platform poll used
    to provide: the coordinator awaits its workers one at a time.

    Unconditionally available, unlike every reader on this device. The freshness
    gate exists because a stale *reading* lies — a held 0 W reads as a live 0 W.
    This sensor holds no reading: it states the standing of our own control, a
    side effect that persists in the inverter. "Off" (the switch is not armed)
    is the honest answer before the first cycle and on a site with no forecast
    at all, and a held "Limiting to 50.0A" stays true if the engine stops,
    because the register really does still hold 50.0 A. A dead engine is
    reported by the hub's own status sensor and by every gated reader beside
    this one; buying a duplicate of that signal with a permanently unavailable
    sensor would be a net loss.

    A text sensor: no unit, device_class or state_class.
    """

    _attr_icon = "mdi:battery-clock"
    _attr_has_entity_name = True

    def __init__(self, hass, config_entry, name, entity_id):
        self._init_entity(
            hass, config_entry, "Charge Control", f"{entity_id}_charge_control_status"
        )
        self._attr_native_value = "Off"

    @property
    def extra_state_attributes(self):
        inverter_rt = self._inverter_runtime()
        return {
            "target_entity": get_entry_value(
                self.config_entry, CONF_CHARGE_LIMIT_ENTITY_ID, None
            ),
            "applied_value": inverter_rt.get(INVERTER_RT_APPLIED),
            "unit": get_entry_value(
                self.config_entry, CONF_CHARGE_LIMIT_UNIT, DEFAULT_CHARGE_LIMIT_UNIT
            ),
        }

    async def _async_site_cycle_work(self, hub_data):
        """Push this cycle's advice to the inverter, then report the outcome.

        Called by the hub coordinator once per site cycle, after the result has
        been published — the advice this consumes is part of that publication.
        The pacing, deadband and once-only release all live in
        ``control/inverter.py`` and are wall-clock based, so they are unaffected
        by how often this runs.
        """
        # None both when the forecast is off and when it has released the
        # limit — the control treats them the same way, as "restore".
        advice_w = self._inverter_section(hub_data).get("forecast_charge_limit_w")
        await send_inverter_charge_limit(
            self.hass, self.config_entry, advice_w, time.monotonic()
        )
        self._read_control_status()

    def _read_control_status(self):
        """Adopt the status the control last recorded for this inverter."""
        self._attr_native_value = self._inverter_runtime().get(
            INVERTER_RT_STATUS, "Off"
        )

    async def async_update(self):
        """Re-read the reported status — deliberately WITHOUT writing.

        Polling is off, so no clock calls this; it runs when someone invokes
        ``homeassistant.update_entity`` on this sensor, and when the HA test tier
        drives the entity directly. It only re-reads what the last cycle
        recorded. Writing here would put a second, unserialized writer on the
        register that any automation could trigger at any rate — able to overlap
        the cycle's own write and to spend the min-interval budget outside the
        one place that owns it.
        """
        self._read_control_status()
