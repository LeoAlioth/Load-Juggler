import logging
import math
import time
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.core import callback
from datetime import datetime, timezone
from ..const import (
    CONF_LOAD_PRIORITY,
    CONF_CHARGE_PAUSE_DURATION,
    CONF_CONNECTED_TO_PHASE,
    CONF_DEVICE_TYPE,
    CONF_ENTITY_ID,
    CONF_EVSE_MAXIMUM_CHARGE_CURRENT,
    CONF_EVSE_MINIMUM_CHARGE_CURRENT,
    CONF_HUB_ENTRY_ID,
    CONF_PHASES,
    CONF_PHASE_VOLTAGE,
    CONF_SOLAR_GRACE_PERIOD,
    CONF_STATION_MIN_CHARGE_POWER,
    CONF_UPDATE_FREQUENCY,
    DEFAULT_LOAD_PRIORITY,
    DEFAULT_CHARGE_PAUSE_DURATION,
    DEFAULT_MAX_CHARGE_CURRENT,
    DEFAULT_MIN_CHARGE_CURRENT,
    DEFAULT_PHASE_VOLTAGE,
    DEFAULT_SOLAR_GRACE_PERIOD,
    DEFAULT_STATION_MIN_CHARGE_POWER,
    DEFAULT_UPDATE_FREQUENCY,
    DEVICE_TYPE_EVSE,
    DEVICE_TYPE_HOT_WATER_TANK,
    DEVICE_TYPE_PLUG,
    DEVICE_TYPE_POWER_STATION,
    DOMAIN,
    EVSE_MODE_EXCESS,
    EVSE_MODE_SOLAR_ONLY,
    EVSE_MODE_SOLAR_PRIORITY,
)
from ..helpers import get_entry_value
from ..ocpp_discovery import ocpp_charge_control_entity, ocpp_connector_status_entity
from .. import units
from .mixins import LoadEntityMixin, SiteFreshnessMixin
from ..registry import get_hub_for_load
from ..control.smoothing import apply_smoothing
from ..control.status import determine_charging_status
from ..control.compliance import check_profile_compliance
from ..control.ocpp import send_ocpp_command
from ..control.plug import send_plug_command
from ..control.hot_water_tank import send_hot_water_tank_command
from ..control.power_station import send_power_station_command

_LOGGER = logging.getLogger(__name__)


def soc_floor_reached(hub_data) -> bool:
    """Is the battery sitting at (or under) the floor the engine gated on?

    ``battery_soc_min`` in ``hub_data`` is the HYSTERESIS-WIDENED floor — the
    very number ``_source_limit`` compared against this cycle — so this answers
    "was the minimum SOC the reason the permit collapsed?" without the load
    layer having to know anything else about why.

    Unreadable either way counts as reached: an unknown SOC must not buy a
    ride-through that the protective case would refuse.
    """
    live_soc = hub_data.get("battery_soc")
    gated_floor = hub_data.get("battery_soc_min")
    if live_soc is None or gated_floor is None:
        return True
    return live_soc <= gated_floor


def grace_modes(binary_load, hub_data) -> tuple:
    """Which operating modes may hold a collapsed permit through the grace window.

    Solar Only and Excess always may. Solar Priority was excluded outright
    (decided 2026-08-17) because a grace hold cannot tell WHY the permit
    collapsed and would therefore also bridge a minimum-SOC shed, and that floor
    is protective — it has to act on the cycle it happens.

    That reason is answerable rather than fundamental for a BINARY load: the
    live SOC and the gated floor are both published, so the one cause that must
    never be bridged can be named here (``soc_floor_reached``). Above the floor
    a Solar Priority plug or tank now rides out the other causes — a brief
    inverter saturation, a cloud — instead of cycling its relay; at the floor it
    sheds exactly as before. Nothing holds a load on below its floor.

    Modulating loads are unchanged: in Solar Priority they fall back to a
    grid-backed minimum rather than to zero, so there is no collapse to bridge.
    """
    modes = (EVSE_MODE_SOLAR_ONLY.key, EVSE_MODE_EXCESS.key)
    if binary_load and not soc_floor_reached(hub_data):
        modes = (*modes, EVSE_MODE_SOLAR_PRIORITY.key)
    return modes



class LoadJugglerDeviceSensor(SiteFreshnessMixin, LoadEntityMixin, SensorEntity):
    """Representation of a managed device (EVSE, smart plug, etc.).

    This entity does NOT run the site calculation. Its hub's coordinator
    (sensor.py) runs it once per site cycle and then calls async_process()
    on every load registered under that hub, handing it the shared result.
    Platform polling stays off: a poll would dispatch commands a second time
    on the platform's own scan interval. (Nothing in this integration polls
    anymore — every entity is pushed by the site cycle.)

    Unlike the read-only sensors it does not subscribe to the coordinator
    either — being CALLED by the site cycle is its subscription, and it writes
    its own state at the end of every processed cycle.
    """

    _attr_should_poll = False
    _attr_native_unit_of_measurement = "A"
    _attr_device_class = SensorDeviceClass.CURRENT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:ev-station"

    def __init__(self, hass, config_entry, hub_entry, name, entity_id):
        """Initialize the sensor."""
        self._init_entity(
            hass,
            config_entry,
            f"{name} Available Current",
            f"{entity_id}_available_current",
        )
        self.hub_entry = hub_entry
        load_entity_id = config_entry.data.get(CONF_ENTITY_ID)
        # Classified out of the registries, shared with the engine (same cache),
        # so a renamed status entity and a multi-connector charger both resolve.
        self._connector_status_entity = ocpp_connector_status_entity(
            hass, config_entry
        )
        self._charge_control_entity = ocpp_charge_control_entity(hass, config_entry)
        self._state = None
        self._phases = None
        # Phase count actually seen drawing, from the engine's live per-load
        # measurement — surfaced as the "detected_phases" attribute and used to
        # encode Watts-mode OCPP limits (control/ocpp.py, control/compliance.py).
        self._car_active_phases = None
        self._operating_mode = None
        self._calc_used = None
        self._allocated_current = None
        self._available_current = None
        # UTC timestamp of the last cycle this load was processed without error.
        # None (not datetime.min) until the first one — a naive sentinel next to
        # the tz-aware values written later is a comparison landmine.
        self._last_update = None
        self._pause_started_at = None
        self._grace_started_at = None
        # Binary-load grace state: the last permit the engine actually granted
        # (what the hold re-offers) and a latch so a spent grace window cannot
        # re-arm on the next cycle and duty-cycle the load forever.
        self._binary_last_permit = 0.0
        self._grace_exhausted = False
        self._prev_operating_mode = None
        self._prev_distribution_mode = None
        self._last_set_current = 0
        self._last_set_power = None
        self._ema_current = None
        self._schmitt_current = None
        self._schmitt_state = "rising"
        self._rate_limited_current = None
        self._last_commanded_limit = None
        self._last_compliance_limit = None
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

    async def async_added_to_hass(self):
        """Register as this hub's load processor.

        The hub coordinator drives every registered processor once per site
        cycle. Registration (not a coordinator reference) is the whole link:
        a load can be set up before its hub's coordinator exists — the next
        hub tick simply picks it up. The entry is released with the entity.
        """
        await super().async_added_to_hass()
        hub_entry_id = self.config_entry.data.get(CONF_HUB_ENTRY_ID)
        processors = (
            self.hass.data.setdefault(DOMAIN, {})
            .setdefault("load_processors", {})
            .setdefault(hub_entry_id, {})
        )
        processors[self.config_entry.entry_id] = self

        @callback
        def _unregister() -> None:
            processors.pop(self.config_entry.entry_id, None)

        self.async_on_remove(_unregister)

    @property
    def native_value(self):
        """The current this load is permitted to draw, in A."""
        return self._state

    @property
    def available(self) -> bool:
        """Available while this load's OWN last processed cycle is recent.

        Keyed on `_last_update` rather than on the hub's publication: this
        entity is served by the site cycle one load at a time, and a load whose
        processing raised (or which was never reached) has a stale permit even
        though hub_data is fresh. None until the first clean cycle, so the
        sensor starts unavailable instead of reporting a permit nobody granted.
        """
        return self._is_fresh(self._last_update)

    @property
    def extra_state_attributes(self):
        """Return load-specific attributes only (site-level data is on hub sensor)."""
        pause_remaining = None
        if self._pause_started_at is not None:
            pause_duration_min = get_entry_value(
                self.config_entry,
                CONF_CHARGE_PAUSE_DURATION,
                DEFAULT_CHARGE_PAUSE_DURATION,
            )
            elapsed = time.monotonic() - self._pause_started_at
            pause_remaining = max(0, round(pause_duration_min * 60 - elapsed))

        grace_remaining = None
        if self._grace_started_at is not None:
            grace_period_min = get_entry_value(
                self.config_entry, CONF_SOLAR_GRACE_PERIOD, DEFAULT_SOLAR_GRACE_PERIOD
            )
            elapsed = time.monotonic() - self._grace_started_at
            grace_remaining = max(0, round(grace_period_min * 60 - elapsed))

        attrs = {
            CONF_PHASES: self._phases,
            "detected_phases": self._car_active_phases,
            "allocated_current": self._allocated_current,
            "last_update": self._last_update,
            "pause_active": self._pause_started_at is not None,
            "pause_remaining_seconds": pause_remaining,
            "grace_active": self._grace_started_at is not None,
            "grace_remaining_seconds": grace_remaining,
            "last_set_current": self._last_set_current,
            "last_set_power": self._last_set_power,
            # Published attribute name predates the charger → load rename and
            # is public API for automations/templates — kept as it shipped.
            "charger_priority": get_entry_value(
                self.config_entry, CONF_LOAD_PRIORITY, DEFAULT_LOAD_PRIORITY
            ),
            "hub_entry_id": self.config_entry.data.get(CONF_HUB_ENTRY_ID),
            "auto_reset_mismatch_count": self._mismatch_count,
            "last_auto_reset": self._last_auto_reset_at,
            "profile_reset_count": self._profile_reset_count,
            "last_hard_reset": self._last_hard_reset_at,
        }
        return attrs

    async def async_process(self, hub_data):
        """Apply one site result to this load: state, timers and commands.

        Called by the hub coordinator once per site cycle, after the single
        site calculation. Everything here is per-load — the site result is
        read-only input. Errors are contained so one misbehaving load cannot
        stop its siblings from being served.
        """
        try:
            await self._async_apply(hub_data)
            # Advance on every clean cycle, not only on the cycles that actually
            # dispatch a command (the control/ modules also stamp this): the
            # per-load update_frequency gate returns from _async_apply without
            # sending, and "last_update" is meant to say when this load was last
            # processed.
            self._last_update = datetime.now(timezone.utc)
        except Exception as e:
            _LOGGER.error(
                f"Error updating Load Juggler load sensor {self._attr_name}: {e}",
                exc_info=True,
            )
        # Polling is off and there is no coordinator listener, so this is the
        # one place the entity's state reaches HA. Skipped before the entity is
        # registered (a hub tick can precede HA adding the entity).
        if self.hass is not None and self.entity_id:
            self.async_write_ha_state()

    async def _async_apply(self, hub_data):
        """The body of async_process — see its docstring."""
        hub_entry = get_hub_for_load(self.hass, self.config_entry.entry_id)
        if not hub_entry:
            _LOGGER.error("Hub not found for load: %s", self._attr_name)
            return

        self.hub_entry = hub_entry

        self._phases = hub_data.get(CONF_PHASES)
        load_active_phases = hub_data.get("load_active_phases", {})
        self._car_active_phases = load_active_phases.get(
            self.config_entry.entry_id,
            self._phases or 1,
        )
        load_phase_masks = hub_data.get("load_phase_masks", {})
        current_distribution_mode = hub_data.get("distribution_mode")

        load_modes = hub_data.get("load_modes", {})
        self._operating_mode = load_modes.get(self.config_entry.entry_id)

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
            # A spent grace window belongs to the mode that spent it.
            self._grace_exhausted = False

        self._prev_operating_mode = self._operating_mode
        self._prev_distribution_mode = current_distribution_mode

        self._calc_used = hub_data.get("calc_used")

        # The site-level republish into hass.data is the hub coordinator's job
        # (one writer per cycle) — see publish_hub_data in entities/hub.py.

        load_targets = hub_data.get("load_targets", {})

        if load_targets:
            load_names = hub_data.get("load_names", {})
            load_modes = hub_data.get("load_modes", {})
            load_avail = hub_data.get("load_available", {})
            _LOGGER.debug(
                "Load targets: %s",
                ", ".join(
                    [
                        f"{load_names.get(k, k[-8:])}({load_modes.get(k, '?')}): "
                        f"alloc={v:.1f}A avail={load_avail.get(k, 0):.1f}A"
                        for k, v in load_targets.items()
                    ]
                ),
            )

        # allocated_current = the load's real footprint (measured draw).
        # It is what the "Allocated Current" sensor shows, for every
        # device type — no smoothing, it is a measurement.
        self._allocated_current = round(
            load_targets.get(self.config_entry.entry_id, 0), 1
        )

        # available_current = the permit the engine grants this device,
        # up to its rated/max. It drives the device command. For an EVSE
        # the OCPP charge limit is the permit, smoothed to avoid
        # oscillation; binary loads (plug, tank) use it directly.
        load_avail_data = hub_data.get("load_available", {})
        raw_permit = round(
            load_avail_data.get(self.config_entry.entry_id, 0), 1
        )

        device_type = self.config_entry.data.get(
            CONF_DEVICE_TYPE, DEVICE_TYPE_EVSE
        )
        if device_type == DEVICE_TYPE_EVSE:
            self._available_current = apply_smoothing(
                self, raw_permit, mode_changed, hub_entry
            )
        elif device_type == DEVICE_TYPE_POWER_STATION:
            # The station's charge speed is rate-limited like an EVSE's
            # current — a saturated Excess pool would otherwise command
            # 0 → max in a single cycle. One divergence: the pipeline
            # resumes from 0 at the full permit (right for an EVSE coming
            # back from a pause); the station instead resumes at its
            # MINIMUM charge power and ramps up from there.
            if (
                self._rate_limited_current == 0
                and raw_permit > 0
                and not mode_changed
            ):
                load_rt = self._load_runtime()
                min_power = load_rt.get(
                    "station_min_charge_power"
                ) or get_entry_value(
                    self.config_entry,
                    CONF_STATION_MIN_CHARGE_POWER,
                    DEFAULT_STATION_MIN_CHARGE_POWER,
                )
                voltage = get_entry_value(
                    hub_entry, CONF_PHASE_VOLTAGE, DEFAULT_PHASE_VOLTAGE
                )
                phases_count = len(
                    get_entry_value(self.config_entry, CONF_CONNECTED_TO_PHASE, "A")
                    or "A"
                )
                resume_current = min(
                    raw_permit, min_power / (voltage * phases_count)
                )
                self._ema_current = resume_current
                self._schmitt_current = resume_current
                self._rate_limited_current = resume_current
            self._available_current = apply_smoothing(
                self, raw_permit, mode_changed, hub_entry
            )
        else:
            self._available_current = raw_permit
        self._state = self._available_current

        # The pause/grace floor MUST be the same number the engine floored this
        # load's permit with, or allocation and pause decisions disagree: the
        # engine would keep granting the runtime minimum while this processor
        # measured it against the static one and paused anyway (issue #37).
        # Both branches therefore read the live runtime slider first and fall
        # back to the config value exactly as engine/hub_calculation.py does.
        load_rt = self._load_runtime()
        if device_type == DEVICE_TYPE_POWER_STATION:
            # The station's floor is its minimum charge POWER, not a current —
            # see _build_power_station_load().
            _min_power = load_rt.get(
                "station_min_charge_power"
            ) or get_entry_value(
                self.config_entry,
                CONF_STATION_MIN_CHARGE_POWER,
                DEFAULT_STATION_MIN_CHARGE_POWER,
            )
            _voltage = get_entry_value(
                hub_entry, CONF_PHASE_VOLTAGE, DEFAULT_PHASE_VOLTAGE
            )
            _phases = len(
                get_entry_value(self.config_entry, CONF_CONNECTED_TO_PHASE, "A")
                or "A"
            )
            min_charge_current = _min_power / (_voltage * _phases)
        else:
            # Mirrors _build_evse_load(): runtime "Min Current" slider, then
            # the configured minimum. Plugs and tanks never publish
            # "min_current" (they are power-rated), so they keep resolving to
            # the config value.
            min_charge_current = load_rt.get("min_current") or get_entry_value(
                self.config_entry,
                CONF_EVSE_MINIMUM_CHARGE_CURRENT,
                DEFAULT_MIN_CHARGE_CURRENT,
            )
        grace_period_minutes = get_entry_value(
            self.config_entry, CONF_SOLAR_GRACE_PERIOD, DEFAULT_SOLAR_GRACE_PERIOD
        )
        grace_period_seconds = grace_period_minutes * 60

        # Binary loads (plug, tank) run no smoothing pipeline: their permit IS
        # the raw permit, so the EVSE hold gate below — "the smoothed permit dipped
        # under the minimum but the engine still physically offers it" — compares a
        # number with itself and can never be true. The grace hold was therefore
        # unreachable for every plug and tank. They get the same idea stated on
        # their own terms: the load had a permit last cycle and it has now
        # collapsed. Any permit > 0 means ON for a binary load, so the hold
        # re-offers the load's own last permit rather than an EVSE minimum current.
        #
        # What the hold bridges is any collapse of the permit while in these
        # modes — a brief inverter saturation, a cloud, an SOC dip past target.
        # This layer cannot see WHY the engine withdrew the permit, and the whole
        # point of grace is that short-lived reasons should not cycle the relay.
        # Sustained ones still shed: the window expires exactly once (the
        # exhausted latch), and only a genuine permit re-arms it.
        binary_load = device_type in (DEVICE_TYPE_PLUG, DEVICE_TYPE_HOT_WATER_TANK)
        if raw_permit > 0:
            self._binary_last_permit = raw_permit
            self._grace_exhausted = False
            if binary_load and self._grace_started_at is not None:
                # A binary load's permit IS the whole answer, so a permit back
                # above 0 is the "conditions recovered" reset the EVSE branch
                # below performs against its minimum current.
                _LOGGER.debug(
                    "Grace timer reset for %s — permit recovered", self._attr_name
                )
                self._grace_started_at = None

        # Solar Priority was excluded from this list (decided 2026-08-17) on the
        # grounds that a grace hold cannot tell WHY the permit collapsed, so it
        # would also bridge minimum-SOC sheds — and the minimum SOC is a
        # protective floor that must act immediately.
        #
        # That reason turns out to be answerable rather than fundamental: the
        # live SOC and the floor the engine actually gated on are both published
        # in hub_data, and that floor is already the hysteresis-widened one, so
        # this layer CAN name the one cause it must never bridge. A Solar
        # Priority binary load therefore rides out every other collapse — a
        # brief inverter saturation, a cloud — while an SOC shed still acts on
        # the cycle it happens, exactly as before. Nothing holds a load on below
        # its floor; the hold is simply no longer forbidden above it.
        if (
            self._operating_mode in grace_modes(binary_load, hub_data)
            and grace_period_seconds > 0
        ):
            if self._available_current < min_charge_current:
                load_avail = hub_data.get("load_available", {})
                physical_available = load_avail.get(
                    self.config_entry.entry_id, 0
                )
                # For an EVSE, grace holds only while the engine still
                # physically offers the minimum — a vanished permit means a
                # site limit, and 6 A of grid draw is real money. A power
                # station's permit IS the excess pool, which collapses in
                # every brief export dip — exactly what grace exists to
                # bridge — and its floor is a ~200 W trickle, so it rides
                # the grace window whenever it was actually charging (the
                # was-charging gate stops an idle station from cycling
                # 200 W on/off through the night). This is what stops the
                # reserve flapping over BLE on marginal days.
                if (
                    device_type == DEVICE_TYPE_POWER_STATION
                    and load_rt.get("station_charging")
                ) or (
                    binary_load
                    and self._binary_last_permit > 0
                    and not self._grace_exhausted
                ) or (
                    device_type != DEVICE_TYPE_POWER_STATION
                    and not binary_load
                    and physical_available >= min_charge_current
                ):
                    if self._grace_started_at is None:
                        self._grace_started_at = time.monotonic()
                        _LOGGER.debug(
                            "Grace timer started for %s (mode=%s, grace=%dm)",
                            self._attr_name,
                            self._operating_mode,
                            grace_period_minutes,
                        )
                    elapsed = time.monotonic() - self._grace_started_at
                    if elapsed < grace_period_seconds:
                        self._available_current = float(
                            self._binary_last_permit
                            if binary_load
                            else min_charge_current
                        )
                    else:
                        _LOGGER.info(
                            "Grace timer expired for %s after %dm — allowing pause",
                            self._attr_name,
                            grace_period_minutes,
                        )
                        self._grace_started_at = None
                        if binary_load:
                            # Do not re-arm next cycle: for a binary load the
                            # permit is the on/off answer, so a re-arming window
                            # would switch it back on for another grace period,
                            # forever.
                            self._grace_exhausted = True
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
        if "load_allocations" not in self.hass.data[DOMAIN]:
            self.hass.data[DOMAIN]["load_allocations"] = {}
        # "Allocated Current" reflects the real footprint for every device
        # type — _allocated_current is the engine's measured draw.
        self.hass.data[DOMAIN]["load_allocations"][
            self.config_entry.entry_id
        ] = self._allocated_current

        # Effective priority rank from the engine — the order this device
        # is served when power is contended (mode urgency, then priority).
        if "load_ranks" not in self.hass.data[DOMAIN]:
            self.hass.data[DOMAIN]["load_ranks"] = {}
        self.hass.data[DOMAIN]["load_ranks"][
            self.config_entry.entry_id
        ] = hub_data.get("load_rank", {}).get(self.config_entry.entry_id)

        if "load_phase_masks" not in self.hass.data[DOMAIN]:
            self.hass.data[DOMAIN]["load_phase_masks"] = {}
        self.hass.data[DOMAIN]["load_phase_masks"][
            self.config_entry.entry_id
        ] = load_phase_masks.get(self.config_entry.entry_id, "")

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

        # min_charge_current is the device-type-aware floor computed above
        # (a power station's is min_power / (V × phases), an EVSE's is its
        # configured minimum) — deliberately not re-read here, since a
        # re-read would resolve to the EVSE default 6 A for a station and
        # falsely trip the pause branch below.
        max_charge_current = get_entry_value(
            self.config_entry,
            CONF_EVSE_MAXIMUM_CHARGE_CURRENT,
            DEFAULT_MAX_CHARGE_CURRENT,
        )

        load_rt = self._load_runtime()
        dynamic_control_on = load_rt.get("dynamic_control", True)

        if device_type == DEVICE_TYPE_HOT_WATER_TANK:
            # The tank is a binary load whose thermostat — not the engine —
            # decides moment-to-moment draw. While the thermostat is
            # satisfied the engine classes the tank inactive and allocates
            # it 0, so the heating-permitted gate follows the *available*
            # current instead: heating stays permitted whenever the site
            # has room for the tank, and is only forbidden when it does
            # not. The EVSE min-current pause threshold does not apply.
            limit = round(self._available_current, 1)
            self._pause_started_at = None
        elif device_type == DEVICE_TYPE_PLUG:
            # A plug is a binary load — the permit is the on/off answer
            # (its rated current when granted, 0 when denied). The EVSE
            # min-current pause threshold does not apply.
            # Round the permit UP to the next 0.1 A instead of nearest, so
            # plugs with a very low power rating (e.g. 10 W → 0.04 A) still
            # come out > 0 and send the turn-on command.
            limit = math.ceil(self._available_current * 10) / 10 if self._available_current > 0 else 0.0
            self._pause_started_at = None
        elif not dynamic_control_on:
            limit = round(float(max_charge_current), 1)
            self._pause_started_at = None
            _LOGGER.debug(
                "Dynamic control OFF for %s — using max current %sA",
                self._attr_name,
                limit,
            )
        elif self._available_current < min_charge_current:
            pause_duration_s = (
                get_entry_value(
                    self.config_entry,
                    CONF_CHARGE_PAUSE_DURATION,
                    DEFAULT_CHARGE_PAUSE_DURATION,
                )
                * 60
            )
            if self._pause_started_at is None:
                self._pause_started_at = time.monotonic()
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
                elapsed = time.monotonic() - self._pause_started_at
                if elapsed < pause_duration_s:
                    limit = 0
                else:
                    self._pause_started_at = None
                    limit = round(self._available_current, 1)
            else:
                limit = round(self._available_current, 1)

        connector_state = self.hass.states.get(self._connector_status_entity)
        connector_status = units.state_or_unknown(connector_state)

        self._charging_status = determine_charging_status(
            self,
            hub_data,
            limit,
            connector_status,
            dynamic_control_on,
            min_charge_current,
        )

        if "load_status" not in self.hass.data.get(DOMAIN, {}):
            self.hass.data.setdefault(DOMAIN, {})["load_status"] = {}
        self.hass.data[DOMAIN]["load_status"][self.config_entry.entry_id] = (
            self._charging_status
        )

        if device_type == DEVICE_TYPE_PLUG:
            # Dynamic Control off → hands off: leave the plug in whatever
            # state the user set, like an un-managed switch.
            if dynamic_control_on:
                await send_plug_command(self, limit, hub_data, now_mono)
        elif device_type == DEVICE_TYPE_HOT_WATER_TANK:
            # Dynamic Control off → Load Juggler does not touch the climate
            # entity at all. The tank then behaves as a normal, un-managed
            # thermostat fully under the user's control.
            if dynamic_control_on:
                await send_hot_water_tank_command(
                    self, limit, hub_data, now_mono
                )
        elif device_type == DEVICE_TYPE_POWER_STATION:
            # Dynamic Control off → leave the station's own charge speed and
            # reserve exactly as the user set them.
            if dynamic_control_on:
                await send_power_station_command(
                    self, limit, hub_entry, now_mono
                )
        else:
            await check_profile_compliance(self, limit, dynamic_control_on)
            await send_ocpp_command(
                self, limit, hub_entry, dynamic_control_on, now_mono
            )
