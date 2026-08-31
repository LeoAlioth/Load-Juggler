"""Per-inverter entities — sensors on each inverter device.

An inverter config entry is a power source linked to a hub, optionally
carrying its own battery. The engine aggregates the hub's inverter fleet each
cycle and publishes a per-inverter section into hub_data
(``hub_data["inverters"][entry_id]``); these sensors read their own entry's
section, mirroring how circuit-group sensors read ``group_data``.

The charge-control status sensor is also where the write-control loop ticks:
it runs once per site cycle with the entry in hand, so the writes ride the same
clock as the readings that justify them. The SOC-control sensor is the same
shape for the battery SOC ceiling: each write-control is driven by the sensor
that reports it, so a control ticks exactly when its own target is configured,
and the coordinator's one-at-a-time await of its workers is what keeps a single
write in flight.
"""

import logging
import time

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)

from ..const import (
    CHARGE_LIMIT_UNIT_AMPS,
    CONF_CHARGE_LIMIT_ENTITY_ID,
    CONF_CHARGE_LIMIT_UNIT,
    CONF_SOC_LIMIT_NORMAL_ENTITY_ID,
    DEFAULT_CHARGE_LIMIT_UNIT,
    INVERTER_RT_APPLIED,
    INVERTER_RT_LAST_WRITE,
    INVERTER_RT_SOC_LAST_WRITE,
    INVERTER_RT_SOC_STATUS,
    INVERTER_RT_STATUS,
)
from ..control.inverter import (
    CONTROL_STATE_OFF,
    INVERTER_RT_NORMAL,
    INVERTER_RT_RECOMMENDED,
    INVERTER_RT_REGISTER,
    INVERTER_RT_SOC_DESIRED,
    INVERTER_RT_SOC_NORMAL,
    INVERTER_RT_SOC_RECOMMENDED,
    INVERTER_RT_SOC_SLOTS,
    send_inverter_charge_limit,
    send_inverter_soc_limit,
)
from ..helpers import get_entry_value
from .mixins import (
    InverterEntityMixin,
    SiteCycleConsumerMixin,
    SiteCycleWorkerMixin,
    SiteFreshnessMixin,
)

_LOGGER = logging.getLogger(__name__)

INVERTER_SENSOR_DEFINITIONS = [
    {
        "unique_id_suffix": "solar_production",
        "data_key": "solar_w",
        "unit": "W",
        "device_class": SensorDeviceClass.POWER,
        "icon": "mdi:solar-power-variant",
        "decimals": 0,
    },
    {
        "unique_id_suffix": "battery_soc",
        "data_key": "battery_soc",
        "unit": "%",
        "device_class": SensorDeviceClass.BATTERY,
        "icon": "mdi:battery-80",
        "decimals": 1,
        "requires_battery": True,
    },
    {
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
        "unique_id_suffix": "forecast_battery_max_soc",
        "data_key": "forecast_battery_max_soc",
        "unit": "%",
        "device_class": SensorDeviceClass.BATTERY,
        "icon": "mdi:battery-lock",
        "decimals": 0,
        "requires_forecast": True,
    },
    {
        "unique_id_suffix": "forecast_charge_limit",
        "data_key": "forecast_charge_limit_w",
        "unit": "W",
        "device_class": SensorDeviceClass.POWER,
        "icon": "mdi:battery-charging-wireless",
        "decimals": 0,
        "requires_forecast": True,
    },
    # OBSERVE-ONLY: how this array's forecast is actually performing, as
    # measured actual ÷ forecast energy over today's unconstrained intervals.
    # 100% means the forecast is exactly right. Nothing acts on it — it exists
    # so a season of evidence can decide whether a learned gain is worth
    # applying (see calculations/calibration.py and dev/TODO.md). No
    # device_class: this is a ratio in percent, not a battery level, and
    # SensorDeviceClass.BATTERY would put a battery icon on it everywhere.
    {
        "unique_id_suffix": "forecast_accuracy",
        "data_key": "forecast_accuracy_pct",
        "unit": "%",
        "icon": "mdi:target-variant",
        "decimals": 1,
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
    # (and therefore the entity_id) is unaffected by a rename. The entity half
    # comes from the translations (entity.sensor.<key>.name), keyed by the
    # definition's unique_id_suffix — both are stable identifiers for the same
    # sensor, so one field serves as both.
    _attr_has_entity_name = True
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, hass, config_entry, entity_id, defn):
        self._init_entity(
            hass,
            config_entry,
            None,
            f"{entity_id}_{defn['unique_id_suffix']}",
        )
        self._attr_translation_key = defn["unique_id_suffix"]
        self._defn = defn
        self._attr_native_unit_of_measurement = defn["unit"]
        # Optional: a ratio in percent has no fitting HA device class, and
        # borrowing one (BATTERY, POWER_FACTOR) would mislabel it everywhere.
        self._attr_device_class = defn.get("device_class")
        self._attr_icon = defn["icon"]
        self._attr_native_value = None

    def _read_site_data(self):
        """This cycle's figure, or unknown when this inverter has none.

        Same contract as the hub data sensors: None from the producer means "no
        measurement this cycle" — this inverter's production sensor is
        unreadable with nothing to hold, so the fleet substituted 0 W for the
        calculation and refuses to publish it. Clearing shows `unknown`;
        holding the last value would freeze a stale reading that looks live,
        which is precisely the fabrication the None exists to prevent.

        An empty section (before the first cycle, or an inverter the hub has
        not aggregated yet) leaves the value untouched.
        """
        own = self._my_inverter_data()
        if not own:
            return
        value = own.get(self._defn["data_key"])
        self._attr_native_value = (
            None if value is None else round(float(value), self._defn["decimals"])
        )


class LoadJugglerInverterChargeControlSensor(
    SiteCycleWorkerMixin, InverterEntityMixin, SensorEntity
):
    """The applied battery charge limit — and the loop that performs the writes.

    A MEASUREMENT of the target register: the value the inverter's charge-limit
    number entity currently holds, in that register's own units (DC amps or
    watts, per ``CONF_CHARGE_LIMIT_UNIT``). That graphs the whole story
    continuously — flat at the normal limit, dipping while the forecast holds the
    battery back, restored on release — and earns long-term statistics, which the
    text state it used to publish ("Limiting to 17.0A") could never do. The
    register is read once per cycle by the control below; None (unknown) when it
    cannot be read at all.

    Our own standing moved to attributes, ``control_state`` first: ``off`` (the
    switch is not armed), ``idle`` (armed, nothing to hold back) or ``limiting``.
    Releasing is a log line rather than a state, since a one-cycle status nobody
    sees is not worth the extra state.

    Its per-cycle work is an AWAIT — a Modbus register write — not a read, so it
    joins the site cycle as a *worker* rather than as a coordinator listener
    (see SiteCycleWorkerMixin). That keeps the writes on the same clock as the
    forecast that produces them, keeps exactly one place talking to the
    inverter's register, and gets the serialization the old platform poll used
    to provide: the coordinator awaits its workers one at a time.

    Still unconditionally available, unlike every reader on this device — and now
    that it does publish a reading, that is a deliberate re-decision rather than
    the old answer carried over. The freshness gate exists because a stale
    *reading* lies: a held 0 W of solar reads as a live 0 W within seconds,
    because solar moves on its own. A charge-limit register does not. It changes
    only when something writes it, and while the control is armed we are the only
    writer — so the last value we read stays factually true for as long as nobody
    writes it, which is exactly the property the gate assumes a reading lacks.
    The failure that does matter here, an unreadable register, is reported in the
    value itself (None → unknown) rather than by blanking the entity, and the
    standing is in ``control_state``, which is honest before the first cycle and
    on a site with no forecast at all. A dead engine is already reported by the
    hub's status sensor and by every gated reader beside this one; a duplicate of
    that signal would cost this sensor the continuity it exists to provide, and
    would blank a live register whenever an unrelated producer went stale.

    The residual: if the site cycle dies, nothing re-reads the register, so the
    last value is held and its statistics flatline until the integration is
    reloaded — visible only as every other sensor on the device going unavailable
    beside it.
    """

    _attr_icon = "mdi:battery-clock"
    _attr_has_entity_name = True
    _attr_translation_key = "charge_control"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, hass, config_entry, entity_id):
        # unique_id unchanged from the text-state era ("…_charge_control_status"):
        # same logical entity, so it keeps its entity_id and its history. Old
        # recorded text states simply predate the numeric ones; statistics start
        # here.
        self._init_entity(
            hass, config_entry, None, f"{entity_id}_charge_control_status"
        )
        # The register's unit is a per-entry choice (Deye exposes DC amps, others
        # watts), so the device class follows it. Both CHARGE_LIMIT_UNIT_* values
        # are already HA's own unit strings for those classes.
        self._unit = (
            get_entry_value(config_entry, CONF_CHARGE_LIMIT_UNIT, None)
            or DEFAULT_CHARGE_LIMIT_UNIT
        )
        amps = self._unit == CHARGE_LIMIT_UNIT_AMPS
        self._attr_native_unit_of_measurement = self._unit
        self._attr_device_class = (
            SensorDeviceClass.CURRENT if amps else SensorDeviceClass.POWER
        )
        # Amps: tenths, the resolution we write. Watts: whole watts.
        self._decimals = 1 if amps else 0
        self._attr_suggested_display_precision = self._decimals
        self._attr_native_value = None

    @property
    def extra_state_attributes(self):
        """The standing behind the number, for the graph's tooltip.

        Everything the control already knows, and nothing it doesn't: the three
        limit values are all in this sensor's own unit, so they can be read
        against the state directly.
        """
        inverter_rt = self._inverter_runtime()
        last_write = inverter_rt.get(INVERTER_RT_LAST_WRITE)
        return {
            "control_state": inverter_rt.get(INVERTER_RT_STATUS, CONTROL_STATE_OFF),
            "target_entity": get_entry_value(
                self.config_entry, CONF_CHARGE_LIMIT_ENTITY_ID, None
            ),
            # What we last wrote (cleared on release), what the control wants the
            # register at, and what a release restores it to.
            "applied_value": inverter_rt.get(INVERTER_RT_APPLIED),
            "recommended_value": inverter_rt.get(INVERTER_RT_RECOMMENDED),
            "normal_value": inverter_rt.get(INVERTER_RT_NORMAL),
            # Monotonic seconds are meaningless outside this process, so report
            # the age instead. None until we have written at all.
            "seconds_since_write": (
                None if last_write is None else round(time.monotonic() - last_write)
            ),
            "unit": self._unit,
        }

    async def _async_site_cycle_work(self, hub_data):
        """Push this cycle's advice to the inverter, then report the outcome.

        Called by the hub coordinator once per site cycle, after the result has
        been published — the advice this consumes is part of that publication.
        The directional pacing, deadband and the upward slew limit all live in
        ``control/inverter.py`` and are wall-clock based, so they are unaffected
        by how often this runs: a faster cadence feeds the downward persistence
        window more samples of the same wall-clock window, not a shorter one.

        The hub entry rides along because the slew step is a site-level number
        (the Excess trigger margin), the way ``control/ocpp.py`` and
        ``control/power_station.py`` are handed the site voltage.
        """
        # None both when the forecast is off and when it has released the
        # limit — the control treats them the same way, as "restore".
        section = self._inverter_section(hub_data)
        advice_w = section.get("forecast_charge_limit_w")
        # The forecast's charge GATE, which the control's downward persistence
        # window needs in order to tell the cap ENGAGING (protective, written at
        # once) from a steady-state correction (paced). Missing means a hub that
        # published no gate state, and the control degrades to writing
        # reductions immediately — see ``send_inverter_charge_limit``.
        await send_inverter_charge_limit(
            self.hass,
            self.config_entry,
            self._hub_entry,
            advice_w,
            time.monotonic(),
            section.get("forecast_charge_limiting"),
        )
        self._read_control_status()

    def _read_control_status(self):
        """Adopt the register value the control last read back for this inverter.

        Not a second read of the register: the control's own read is the only one,
        recorded in the runtime dict on every call — including the calls the
        pacing makes return early — so this stays a dict lookup. Missing (no
        target entity configured, or before the first cycle) and unreadable both
        land on None, which HA renders as unknown; the standing that explains
        which one it is rides along in ``control_state``.
        """
        value = self._inverter_runtime().get(INVERTER_RT_REGISTER)
        self._attr_native_value = (
            None if value is None else round(float(value), self._decimals)
        )

    async def async_update(self):
        """Re-read the reported value — deliberately WITHOUT writing.

        Polling is off, so no clock calls this; it runs when someone invokes
        ``homeassistant.update_entity`` on this sensor, and when the HA test tier
        drives the entity directly. It only re-reads what the last cycle
        recorded. Writing here would put a second, unserialized writer on the
        register that any automation could trigger at any rate — able to overlap
        the cycle's own write and to spend the min-interval budget outside the
        one place that owns it.
        """
        self._read_control_status()


class LoadJugglerInverterSocControlSensor(
    SiteCycleWorkerMixin, SiteFreshnessMixin, InverterEntityMixin, SensorEntity
):
    """The battery SOC ceiling being enforced — and the loop that enforces it.

    A percentage, matching the *Recommended Battery Max SOC* sensor's own device
    class and unit so the two can be read against each other on one axis: the
    recommendation, and what actually got enforced after the min() with the
    normal ceiling. None (unknown) while the switch is off — nothing is being
    enforced then, and 100 would be a claim about the slots we are not making.

    Its own worker, not a passenger on the charge-control sensor's. The two
    write-controls are configured independently — an inverter can expose TOU SOC
    slots and no charge-current register, or the reverse — so an inverter that
    configures only this one has no charge-control sensor to ride, and a control
    that silently never ticked would be the worst possible failure here. What the
    single-worker arrangement was protecting is preserved anyway, and by the
    coordinator rather than by luck: it awaits its workers one at a time, so no
    two writes are ever in flight together, exactly as it already does for the
    several workers a multi-inverter site has. The two controls write disjoint
    entities and read disjoint advice, so their order does not matter — which is
    just as well, since worker order is insertion order and not promised.

    Like its sibling it does no entity-side reads of its own: the slots are read
    by ``control/inverter.py`` once per call and handed over through the runtime
    dict (``INVERTER_RT_SOC_*``).

    Availability DOES follow the site cycle, unlike the charge-control sensor —
    and for the reason that sensor spells out, in reverse. That one measures a
    device register, which stays factually true for as long as nobody writes it.
    This one reports *our own intention*, which exists only while the cycle
    computing it runs: a held "enforcing 80 %" from a dead engine would be a
    claim about a control that is no longer controlling anything. So it goes
    unavailable with every other reader on the device instead.
    """

    _attr_icon = "mdi:battery-lock"
    _attr_has_entity_name = True
    _attr_translation_key = "soc_control"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_native_unit_of_measurement = "%"
    _attr_suggested_display_precision = 0

    def __init__(self, hass, config_entry, entity_id):
        self._init_entity(
            hass, config_entry, None, f"{entity_id}_soc_control_status"
        )
        self._attr_native_value = None

    @property
    def extra_state_attributes(self):
        """The two inputs, the per-slot outcome, and the write age.

        The slot map is the fan-out made visible: which entities this control
        drives and what each of them last read back, so a slot the user forgot
        to include — or one that is unavailable and being skipped — is visible
        without reading the log.
        """
        inverter_rt = self._inverter_runtime()
        last_write = inverter_rt.get(INVERTER_RT_SOC_LAST_WRITE)
        return {
            "control_state": inverter_rt.get(
                INVERTER_RT_SOC_STATUS, CONTROL_STATE_OFF
            ),
            # What the slots idle at (the normal entity's live value, or 100),
            # and what the clipping forecast is asking for. The state is the
            # min() of the two.
            "normal_value": inverter_rt.get(INVERTER_RT_SOC_NORMAL),
            "recommended_value": inverter_rt.get(INVERTER_RT_SOC_RECOMMENDED),
            # entity_id → last read-back, None where a slot is unreadable.
            "slot_values": dict(inverter_rt.get(INVERTER_RT_SOC_SLOTS) or {}),
            "normal_entity": get_entry_value(
                self.config_entry, CONF_SOC_LIMIT_NORMAL_ENTITY_ID, None
            ),
            # Monotonic seconds mean nothing outside this process, so report the
            # age. None until this control has written at all.
            "seconds_since_write": (
                None if last_write is None else round(time.monotonic() - last_write)
            ),
        }

    async def _async_site_cycle_work(self, hub_data):
        """Drive this cycle's SOC ceiling, then report what is being enforced.

        Called by the hub coordinator once per site cycle, after the result has
        been published — the advice this consumes is part of that publication,
        and it is the same number the *Recommended Battery Max SOC* sensor
        beside us reports. None means the forecast has nothing to say, which for
        this control is not a release but "track the normal ceiling".

        The pacing and the per-slot deadband live in ``control/inverter.py`` and
        are wall-clock based, so how often this runs changes how often the
        *check* happens, not how often the slots are written.
        """
        advice_soc = self._inverter_section(hub_data).get("forecast_battery_max_soc")
        await send_inverter_soc_limit(
            self.hass, self.config_entry, advice_soc, time.monotonic()
        )
        self._read_control_status()

    def _read_control_status(self):
        """Adopt the ceiling the control is enforcing for this inverter.

        A dict lookup, like the charge-control sensor's read: the slots are read
        by the control and by nothing else, so this entity never talks to the
        inverter. None when the switch is off, when the normal ceiling is
        unreadable and writes are deferred, or before the first cycle.
        """
        desired = self._inverter_runtime().get(INVERTER_RT_SOC_DESIRED)
        self._attr_native_value = None if desired is None else round(float(desired), 1)

    async def async_update(self):
        """Re-read what is being enforced — deliberately WITHOUT writing.

        Same contract as the charge-control sensor's: ``update_entity`` is a
        service any automation can call at any rate, and writing from here would
        be a second, unserialized writer on entities the cycle owns.
        """
        self._read_control_status()
