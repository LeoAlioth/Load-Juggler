"""Load Juggler - LoadContext builders: config entries + HA states -> loads.

One builder per managed device type (OCPP EVSE, smart plug, power station, hot
water tank), each turning a load's config entry and its live entity states into
the ``LoadContext`` the calculation engine distributes power to, plus
``_add_loads_to_site()`` which walks the hub's registered loads and dispatches
to the right builder, and ``_build_circuit_groups()`` for the shared-breaker
groups. Reads come through engine/readers.py; nothing here decides allocations.

Split out of hub_calculation.py, which now consumes these builders rather than
defining them.
"""

# PEP 604 unions (``float | None``) appear in this module's signatures. Nothing
# here evaluates annotations at runtime (no dataclasses, NamedTuple/TypedDict or
# get_type_hints calls), so deferring them keeps the module importable on the
# Python 3.9 interpreters the standalone test runners use (same arrangement as
# engine/auto_detect.py).
from __future__ import annotations

import logging
import math
import time
from datetime import datetime, timezone

from ..calculations import LoadContext, CircuitGroup
from ..calculations.models import INACTIVE_STATUSES
from ..const import (
    CONF_CHARGER_L1_PHASE,
    CONF_CHARGER_L2_PHASE,
    CONF_CHARGER_L3_PHASE,
    CONF_LOAD_PRIORITY,
    CONF_CIRCUIT_GROUP_CURRENT_LIMIT,
    CONF_CIRCUIT_GROUP_MEMBERS,
    CONF_CLIMATE_ENTITY_ID,
    CONF_CONNECTED_TO_PHASE,
    CONF_DEVICE_TYPE,
    CONF_ENTITY_ID,
    CONF_EVSE_CURRENT_IMPORT_ENTITY_ID,
    CONF_EVSE_CURRENT_IMPORT_L1_ENTITY_ID,
    CONF_EVSE_CURRENT_IMPORT_L2_ENTITY_ID,
    CONF_EVSE_CURRENT_IMPORT_L3_ENTITY_ID,
    CONF_EVSE_MAXIMUM_CHARGE_CURRENT,
    CONF_EVSE_CURRENT_OFFERED_ENTITY_ID,
    CONF_EVSE_MINIMUM_CHARGE_CURRENT,
    CONF_EVSE_POWER_IMPORT_ENTITY_ID,
    CONF_EVSE_POWER_OFFERED_ENTITY_ID,
    CONF_HEATING_ELEMENT_POWER,
    CONF_NAME,
    CONF_PHASES,
    CONF_PLUG_MAX_CURRENT,
    CONF_PLUG_POWER_MONITOR_ENTITY_ID,
    CONF_PLUG_POWER_RATING,
    CONF_PLUG_SWITCH_ENTITY_ID,
    CONF_STATION_AC_INPUT_ENTITY_ID,
    CONF_STATION_RESERVE_ENTITY_ID,
    CONF_STATION_AC_OUTPUT_ENTITY_ID,
    CONF_STATION_BATTERY_LEVEL_ENTITY_ID,
    CONF_STATION_CHARGE_LIMIT_ENTITY_ID,
    CONF_STATION_CHARGE_SPEED_ENTITY_ID,
    CONF_STATION_MAX_CHARGE_POWER,
    CONF_STATION_MIN_CHARGE_POWER,
    CONF_TANK_NORMAL_TEMPERATURE,
    CONF_TANK_POWER_ENTITY_ID,
    CONF_TANK_PRIORITIZE_BELOW_NORMAL,
    DEFAULT_LOAD_PRIORITY,
    DEFAULT_CIRCUIT_GROUP_CURRENT_LIMIT,
    DEFAULT_HEATING_ELEMENT_POWER,
    DEFAULT_MAX_CHARGE_CURRENT,
    DEFAULT_MIN_CHARGE_CURRENT,
    DEFAULT_OPERATING_MODE_EVSE,
    DEFAULT_OPERATING_MODE_HOT_WATER_TANK,
    DEFAULT_OPERATING_MODE_PLUG,
    DEFAULT_OPERATING_MODE_POWER_STATION,
    DEFAULT_PLUG_MAX_CURRENT,
    DEFAULT_PLUG_POWER_RATING,
    DEFAULT_STATION_CHARGE_LIMIT,
    DEFAULT_STATION_MAX_CHARGE_POWER,
    DEFAULT_STATION_MIN_CHARGE_POWER,
    DEFAULT_TANK_NORMAL_TEMPERATURE,
    DEFAULT_TANK_PRIORITIZE_BELOW_NORMAL,
    DEVICE_TYPE_EVSE,
    DEVICE_TYPE_HOT_WATER_TANK,
    DEVICE_TYPE_PLUG,
    DEVICE_TYPE_POWER_STATION,
    DOMAIN,
    ENTRY_TYPE,
    ENTRY_TYPE_LOAD,
    EVSE_RT_COMMANDED_LIMIT,
    EVSE_RT_COMMANDED_RATE_UNIT,
    EVSE_RT_READOUT_WATCH,
    LEG_DRAWING_CURRENT,
    SETTLE_DRAW_SECONDS,
    SETTLE_DRAW_TOLERANCE,
    SETTLE_PERMIT_MARGIN,
    STATION_MODE_STANDARD,
    SUSPENDED_EV_IDLE_TIMEOUT,
    WATTS_PROFILE_TOLERANCE,
    behavior_for,
    resolve_operating_mode,
    resolve_tank_mode_priority,
    tank_boost_is_opportunistic,
    BEHAVIOR_BINARY_EXCESS,
    BEHAVIOR_EXCESS,
    CONF_TANK_AWAY_TEMPERATURE,
    CONF_TANK_BOOST_TEMPERATURE,
    DEFAULT_TANK_AWAY_TEMPERATURE,
    DEFAULT_TANK_BOOST_TEMPERATURE,
    TANK_MODE_FREEZE_PROTECTION,
    TANK_MODE_NORMAL,
)
from ..helpers import get_entry_value
from ..ocpp_discovery import ocpp_connector_status_entity
from ..registry import get_loads_for_hub, get_groups_for_hub
from .. import units
from . import readout_watch
from .readers import (
    _PHASE_LABELS,
    _UNAVAILABLE,
    _clamp_reported_phase_draw,
    _coerce,
    _fv,
    _read_entity,
    _read_phase_attr,
)

_LOGGER = logging.getLogger(__name__)


def _offered_reading(hass, entry):
    """The charger's offered current (or power) as a bare number, or None.

    Only ever used as the stuck-readout watch's COMPANION: a second reading
    from the same charger's meter reports, whose changes say how often those
    reports arrive while the draw reading itself has not yet shown enough of
    them - see engine/readout_watch.py. The unit does not matter; only whether
    the value moved.
    """
    for key in (CONF_EVSE_CURRENT_OFFERED_ENTITY_ID, CONF_EVSE_POWER_OFFERED_ENTITY_ID):
        entity_id = get_entry_value(entry, key, None)
        if entity_id:
            return _coerce(_read_entity(hass, entity_id, None), None)
    return None


def _fmt_legs(legs) -> str:
    return "/".join(f"{v:.1f}" for v in legs)


def _watch_readout(hass, entry, load, load_rt, connector_status):
    """Run the stuck-readout watch on this EVSE and, while it judges the
    reading stuck, control the load BLIND. The rules, and why they cannot trip
    on a car that is merely steady or drawing less than offered, are in
    engine/readout_watch.py.

    This is the per-load half: the reading's own tracker and the frozen-HIGH
    evidence (the reading claims more than the limit in force). The frozen-LOW
    evidence needs every managed draw on the site and runs after all loads are
    built - ``_watch_readouts_against_household`` below. This half records the
    cycle's inputs for it in ``watch["cycle"]``.

    Blind mode, for as long as the episode lasts:

    * l1..l3 become the ASSUMED draw: the limit the charger last accepted on
      the legs the verdict decided carry current, 0 A on the others and 0 A
      throughout a no-energy status. Deliberately NOT capped at the load's
      max-current slider: lowered mid-episode, the charger still holds the old
      limit until the next command lands, and the footprint must not assume it
      already obeys. On the feedback loop the assumption is exact for a car
      that follows its limit - which is what ends the hunting a frozen-low
      reading causes; a car that sits below it makes the household read low by
      the difference, the same exposure the settled-draw rule already accepts.
    * ``draw_blind`` makes the footprint the larger of the allocation and that
      assumption (target_calculator._pool_deduction), and stops the frozen
      number from ever counting as a settled draw - which, for a reading
      pinned at 0, would otherwise book a charging car at NO footprint at all.
    * ``draw_estimate`` publishes it AS an estimate: Current Managed Power,
      the household and the charger's own draw carry the assumed figure,
      marked estimated on their entities (engine/hub_result.py,
      entities/readout.py) - rather than going unknown for what may be the
      whole session.

    One warning when an episode starts, one info line when it ends - never one
    per cycle.
    """
    watch = load_rt.setdefault(EVSE_RT_READOUT_WATCH, {})
    now = time.monotonic()
    started = watch.get("stuck_since")
    watch["cycle"] = None

    # Nothing to judge: a load handed back to the user (its draw is household),
    # a monitor that produced no reading or only part of one (the unreadable-
    # monitor handling above owns those), or no car at all (its draw is
    # already 0). The learned gaps survive; the episode does not.
    if not load.dynamic_control:
        reason = "Dynamic Control is off"
    elif load.unmetered or load.draw_assumed:
        reason = "the readout is unavailable"
    elif connector_status == "Available":
        reason = "the connector is Available (no car)"
    else:
        reason = None
    if reason is not None:
        if readout_watch.reset(watch):
            _LOGGER.info(
                "EVSE %s: leaving blind mode after %.0f s - %s",
                load.entity_id,
                now - (started if started is not None else now),
                reason,
            )
        for key in ("stuck_at", "assumed"):
            watch.pop(key, None)
        watch["normal_gap_s"] = readout_watch.normal_gap(watch)
        return

    commanded = load_rt.get(EVSE_RT_COMMANDED_LIMIT)
    margin = SETTLE_PERMIT_MARGIN
    if commanded and load_rt.get(EVSE_RT_COMMANDED_RATE_UNIT) == "W":
        margin += WATTS_PROFILE_TOLERANCE * float(commanded)
    event = readout_watch.observe(
        watch,
        now=now,
        legs=(load.l1_current, load.l2_current, load.l3_current),
        status=connector_status,
        commanded=commanded,
        margin=margin,
        drawing_current=LEG_DRAWING_CURRENT,
        companion=_offered_reading(hass, entry),
    )
    watch["normal_gap_s"] = readout_watch.normal_gap(watch)
    watch["cycle"] = {"status": connector_status, "commanded": commanded}

    if event == "left":
        _LOGGER.info(
            "EVSE %s: current readout is moving again (now %s A) - leaving "
            "blind mode after %.0f s",
            load.entity_id,
            _fmt_legs((load.l1_current, load.l2_current, load.l3_current)),
            now - (started if started is not None else now),
        )
        for key in ("stuck_at", "assumed"):
            watch.pop(key, None)
    if readout_watch.is_stuck(watch):
        _go_blind(load, watch, connector_status, commanded, event == "entered", now)


def _go_blind(load, watch, status, commanded, entered, now):
    """Put the assumed draw in place of the stuck reading, for this cycle.

    ``entered`` says this is the cycle the episode began, which is the one that
    gets the warning - whichever evidence path reached the verdict.
    """
    assumed = readout_watch.assumed_legs(watch, status, commanded)
    if entered:
        watch["stuck_at"] = datetime.now(timezone.utc)
        silent_for = now - watch.get("changed_at", now)
        gap = watch.get("stuck_gap")
        cadence = (
            f"this reading normally changes at least every {gap:.0f} s"
            if gap is not None
            else "this reading has not been seen to change since start-up"
        )
        legs = "/".join(
            f"L{i + 1}" for i, on in enumerate(watch.get("stuck_legs") or ()) if on
        ) or "no leg"
        now_txt = (
            f"0 A while the connector is {status}"
            if status in readout_watch.NO_ENERGY_STATUSES
            else f"{_fmt_legs(assumed)} A now"
        )
        if watch.get("stuck_how") == readout_watch.HOUSEHOLD_LOCKSTEP:
            _LOGGER.warning(
                "EVSE %s: current readout looks stuck - it has read %s A "
                "(L1/L2/L3), unchanged for %.0f s, while the house load "
                "measured at %s followed the last %d changes of this charger's "
                "limit, up and down, on phase(s) %s - that is the charger's "
                "own draw, not the house's; %s. Controlling blind until it "
                "moves again, assuming the charger draws what it was told on "
                "%s (%s).",
                load.entity_id,
                _fmt_legs(watch.get("stuck_value") or ()),
                silent_for,
                watch.get("stuck_source") or "the grid",
                watch.get("stuck_run") or 0,
                ",".join(watch.get("stuck_phases") or ()),
                cadence,
                legs,
                now_txt,
            )
        else:
            in_force = watch.get("stuck_limit")
            _LOGGER.warning(
                "EVSE %s: current readout looks stuck - it has read %s A "
                "(L1/L2/L3), unchanged for %.0f s, while the charger may "
                "deliver at most %.1f A (%s); %s. Controlling blind until it "
                "moves again, assuming the charger draws what it was told on "
                "%s (%s).",
                load.entity_id,
                _fmt_legs(watch.get("stuck_value") or ()),
                silent_for,
                in_force if in_force is not None else 0.0,
                (
                    f"connector {status}, no energy flowing"
                    if status in readout_watch.NO_ENERGY_STATUSES
                    else "its commanded limit"
                ),
                cadence,
                legs,
                now_txt,
            )
    load.l1_current, load.l2_current, load.l3_current = assumed
    load.draw_blind = True
    load.draw_settled = False
    load.draw_estimate = {
        "load": load.entity_id,
        "evidence": watch.get("stuck_how"),
        "since": watch.get("stuck_at"),
    }
    watch["assumed"] = assumed
    _LOGGER.debug(
        "EVSE %s: blind - reading frozen at %s A, assuming %s A",
        load.entity_id,
        _fmt_legs(watch.get("stuck_value") or ()),
        _fmt_legs(assumed),
    )


def _watch_readouts_against_household(hass, site, supply_phases, unusable,
                                      source="the grid"):
    """The frozen-LOW evidence path, for every EVSE on the site.

    Runs once all loads are built (after any phase remap) and BEFORE the
    feedback loop, on the household as the engine is about to reconstruct it:
    ``supply_phases`` - what the site draws with our loads in it, per phase
    (the signed grid reading, or off-grid the inverter output; see
    hub_calculation._supply_per_phase) - minus every managed draw, blind loads
    at their assumed draw and everything else as read. A charger whose reading
    is stuck low shows up there as house load that steps with our own
    commands to it (engine/readout_watch.observe_household).

    A phase with no usable reading - None, or ``unusable`` (an unreadable CT
    standing on the breaker assumption) - is skipped rather than judged.

    A load judged stuck here goes blind on this very cycle; from the next one
    its builder does it, ahead of the settle and SuspendedEV logic.
    """
    draws = [0.0, 0.0, 0.0]
    for load in site.loads:
        if load.dynamic_control:
            for i, amps in enumerate(load.get_site_phase_draw()):
                draws[i] += amps
    household = {
        phase: (
            None
            if supply_phases[i] is None or unusable[i]
            else supply_phases[i] - draws[i]
        )
        for i, phase in enumerate(_PHASE_LABELS)
    }
    loads_rt = hass.data.get(DOMAIN, {}).get("loads", {})
    now = time.monotonic()
    for load in site.loads:
        if load.device_type != DEVICE_TYPE_EVSE:
            continue
        watch = (loads_rt.get(load.load_id) or {}).get(EVSE_RT_READOUT_WATCH)
        cycle = watch.get("cycle") if watch else None
        if not cycle:
            continue
        legs = max(1, min(3, int(load.phases or 1)))
        event = readout_watch.observe_household(
            watch,
            now=now,
            household=household,
            leg_phases=(load.l1_phase, load.l2_phase, load.l3_phase)[:legs],
            status=cycle["status"],
            commanded=cycle["commanded"],
            drawing_current=LEG_DRAWING_CURRENT,
        )
        if event == "entered":
            watch["stuck_source"] = source
            _go_blind(load, watch, cycle["status"], cycle["commanded"], True, now)


def _build_evse_load(hass, entry, voltage, load_entity_id, priority,
                     settle_seconds=SETTLE_DRAW_SECONDS):
    """Build a LoadContext for an OCPP EVSE load."""
    load_rt = hass.data[DOMAIN]["loads"].get(entry.entry_id, {})
    config_min = get_entry_value(
        entry, CONF_EVSE_MINIMUM_CHARGE_CURRENT, DEFAULT_MIN_CHARGE_CURRENT
    )
    config_max = get_entry_value(
        entry, CONF_EVSE_MAXIMUM_CHARGE_CURRENT, DEFAULT_MAX_CHARGE_CURRENT
    )
    min_current = load_rt.get("min_current") or config_min
    max_current = load_rt.get("max_current") or config_max
    # The sliders refuse to cross each other (number.py), but a state restored
    # from an install that predates that guard still can. An inverted interval
    # makes every permit nonsensical, so collapse it - downwards, so a bad pair
    # can never authorise MORE current than the configured maximum. (The power
    # station builder below resolves its own inverted pair the other way; its
    # min is a trickle floor, not a hardware limit.)
    if min_current > max_current:
        _LOGGER.warning(
            "%s: min_current %.1fA is above max_current %.1fA - using %.1fA for both",
            load_entity_id, min_current, max_current, max_current,
        )
        min_current = max_current

    phases = int(get_entry_value(entry, CONF_PHASES, 3) or 3)

    # Read connector status from the charger's own status sensor - resolved
    # from the registries by metric classification (and cached per entry setup),
    # never composed from the charge point id: that guess is wrong for a renamed
    # status entity and on every multi-connector charger.
    connector_status_entity = ocpp_connector_status_entity(hass, entry)
    connector_status_state = hass.states.get(connector_status_entity)
    connector_status = (
        connector_status_state.state if connector_status_state else "Unknown"
    )

    # Read L1/L2/L3 → site phase mapping
    l1_phase = get_entry_value(entry, CONF_CHARGER_L1_PHASE, "A")
    l2_phase = get_entry_value(entry, CONF_CHARGER_L2_PHASE, "B")
    l3_phase = get_entry_value(entry, CONF_CHARGER_L3_PHASE, "C")

    # Resolve the per-load operating mode from runtime data.
    mode = resolve_operating_mode(
        DEVICE_TYPE_EVSE,
        load_rt.get("operating_mode", DEFAULT_OPERATING_MODE_EVSE.key),
    )

    load = LoadContext(
        load_id=entry.entry_id,
        entity_id=load_entity_id,
        min_current=min_current,
        max_current=max_current,
        phases=phases,
        priority=priority,
        connector_status=connector_status,
        # "Hands off" reaches the calculation too - see LoadContext.
        dynamic_control=load_rt.get("dynamic_control", True),
        operating_mode=mode.key,
        mode_behavior=behavior_for(mode),
        mode_priority=mode.priority,
        rated_current=max_current,
        excess_claim_current=(
            min_current
            if behavior_for(mode) == BEHAVIOR_EXCESS
            and connector_status not in INACTIVE_STATUSES
            else 0.0
        ),
        l1_phase=l1_phase,
        l2_phase=l2_phase,
        l3_phase=l3_phase,
    )

    # Get OCPP current draw for this load with fallback chain:
    # 1. Current Import per-phase entities (sensor.{id}_current_import_l1/l2/l3)
    # 2. Current Import entity (per-phase attributes or total)
    # 3. Power Active Import (convert W → A)
    # Options-first (get_entry_value): re-pointing the charger at another OCPP
    # device on the options page rewrites the whole sensor set into options, and
    # the entry's static data half keeps whatever setup found.
    evse_import = get_entry_value(entry, CONF_EVSE_CURRENT_IMPORT_ENTITY_ID, None)
    evse_import_l1 = get_entry_value(entry, CONF_EVSE_CURRENT_IMPORT_L1_ENTITY_ID, None)
    evse_import_l2 = get_entry_value(entry, CONF_EVSE_CURRENT_IMPORT_L2_ENTITY_ID, None)
    evse_import_l3 = get_entry_value(entry, CONF_EVSE_CURRENT_IMPORT_L3_ENTITY_ID, None)
    evse_power_import = get_entry_value(entry, CONF_EVSE_POWER_IMPORT_ENTITY_ID, None)
    current_draw = None

    # Try per-phase current import entities first (separate sensors for each phase)
    if evse_import_l1 or evse_import_l2 or evse_import_l3:
        raw_vals = [
            _read_entity(hass, entity, None) if entity else None
            for entity in (evse_import_l1, evse_import_l2, evse_import_l3)
        ]
        l1_val, l2_val, l3_val = (_coerce(raw, None) for raw in raw_vals)

        if l1_val is not None or l2_val is not None or l3_val is not None:
            load.l1_current = l1_val if l1_val is not None else 0
            load.l2_current = l2_val if l2_val is not None else 0
            load.l3_current = l3_val if l3_val is not None else 0
            # A phase whose OWN sensor is configured but unreadable contributes
            # an invented 0 to a draw the other phases made look measured - so
            # the total is fabricated even though this path produced a reading.
            if any(raw is _UNAVAILABLE for raw in raw_vals):
                load.draw_assumed = True
            current_draw = "current_import_l1l2l3"
            _LOGGER.debug(
                "EVSE %s: Using per-phase current import entities: L1=%.1f L2=%.1f L3=%.1f",
                load_entity_id,
                load.l1_current,
                load.l2_current,
                load.l3_current,
            )

    # Try Current Import entity with per-phase attributes or total
    if current_draw is None and evse_import:
        evse_state = hass.states.get(evse_import)
        if not units.is_unavailable(evse_state):
            try:
                attrs = evse_state.attributes
                l1 = _read_phase_attr(
                    attrs, ("l1_current", "l1", "phase_1", "current_phase_1")
                )
                l2 = _read_phase_attr(
                    attrs, ("l2_current", "l2", "phase_2", "current_phase_2")
                )
                l3 = _read_phase_attr(
                    attrs, ("l3_current", "l3", "phase_3", "current_phase_3")
                )

                if l1 is not None or l2 is not None or l3 is not None:
                    load.l1_current = l1 or 0
                    load.l2_current = l2 or 0
                    load.l3_current = l3 or 0
                    _clamp_reported_phase_draw(
                        load, entry, load_entity_id, max_current
                    )
                    current_draw = "current_import_attr"
                else:
                    # A single total-ish reading copied onto every active phase
                    # needs the same clamp: if the entity really carries the
                    # site total, replicating it would triple-book the draw.
                    current_import = float(evse_state.state)
                    load.l1_current = current_import
                    if phases >= 2:
                        load.l2_current = current_import
                    if phases >= 3:
                        load.l3_current = current_import
                    _clamp_reported_phase_draw(
                        load, entry, load_entity_id, max_current
                    )
                    current_draw = "current_import_total"
            except (ValueError, TypeError):
                pass

    # Fallback to Power Active Import if no current import data available
    if current_draw is None and evse_power_import:
        power_state = hass.states.get(evse_power_import)
        if not units.is_unavailable(power_state):
            try:
                # kW-aware: an OCPP integration reporting kW would otherwise
                # make a charging car look like it draws ~nothing, and the
                # engine would hand its allocation to something else too.
                power_w = units.to_watts(
                    float(power_state.state),
                    power_state.attributes.get("unit_of_measurement"),
                    voltage,
                )
                if power_w > 0 and voltage > 0:
                    # Convert W → A (total power across all phases)
                    power_per_phase = power_w / phases
                    current_per_phase = power_per_phase / voltage
                    load.l1_current = current_per_phase
                    if phases >= 2:
                        load.l2_current = current_per_phase
                    if phases >= 3:
                        load.l3_current = current_per_phase
                    current_draw = "power_import"
                    _LOGGER.debug(
                        "EVSE %s: Using Power Active Import fallback: %.1fW → %.1fA per phase",
                        load_entity_id,
                        power_w,
                        current_per_phase,
                    )
            except (ValueError, TypeError):
                pass

    # An EMPTY connector cannot be drawing current, whatever its meter says.
    # "Available" is OCPP's own word for "no car", so this is a fact about the
    # connector rather than an inference about the reading.
    #
    # Chargers commonly stop sending MeterValues when a session ends, and Home
    # Assistant holds a sensor's last state until something newer arrives - so
    # `current_import` freezes at whatever the car was taking when it was
    # unplugged. Seen live on the SE17K Elvi (2026-09-11): 13.2 A reported into
    # an empty connector for hours. That phantom draw is added back by the
    # loads-off reconstruction, and one stale reading moved four published
    # figures at once - grid headroom read the WHOLE breaker (every phase's
    # consumption went to 0 once 13.2 A was subtracted from a 1.8 A import), a
    # solar pool of 11.4 A appeared on a phase that had none, export-with-
    # loads-off read 3 898 W against a real 856 W, and household clamped at 0
    # where the arithmetic wanted -2 215 W. A load engaging on that phantom
    # pool would have made the site import.
    #
    # Deliberately NOT a staleness timeout. A car charging steadily holds
    # `current_import` at one value for minutes, so "has not changed recently"
    # cannot tell stale from steady and would shed healthy sessions. And
    # deliberately only on "Available": an unreadable status is not evidence of
    # an empty connector, and inventing a zero there would repeat the grid-CT
    # mistake in the other direction. Finishing and Faulted keep their draw
    # too - a car may still be connected.
    if connector_status == "Available" and (
        load.l1_current or load.l2_current or load.l3_current
    ):
        _LOGGER.debug(
            "EVSE %s: connector is Available (no car) but the meter reports "
            "%.1f/%.1f/%.1f A - treating the draw as 0",
            load_entity_id,
            load.l1_current, load.l2_current, load.l3_current,
        )
        load.l1_current = load.l2_current = load.l3_current = 0.0

    if current_draw:
        _LOGGER.debug(
            "EVSE %s: Current draw source: %s", load_entity_id, current_draw
        )

    # No current-import source found - the engine cannot see this EVSE's real
    # draw, so its footprint falls back to its permit (it may reserve more than
    # it uses). Plugs and tanks always carry a correct draw and are never
    # flagged unmetered.
    load.unmetered = current_draw is None
    # ...and when monitors ARE configured, "no source" means every one of them
    # is unreadable: the 0 A this load carries into the cycle is invented, not
    # a car sitting idle. A charger with no monitor configured at all is a
    # different, deliberate case (nothing was ever measured to lose), and the
    # published figures for it are unchanged.
    if current_draw is None and any(
        (evse_import, evse_import_l1, evse_import_l2, evse_import_l3,
         evse_power_import)
    ):
        load.draw_assumed = True

    # Stuck readout: a reading that goes on claiming a draw the charger has
    # since been told it may not deliver is replaced by the draw it WAS told
    # (blind mode). Ahead of the settle and SuspendedEV logic below, so both
    # judge the draw the engine will actually use. Takes the RAW connector
    # status: the SuspendedEV -> Finishing substitution below is the engine's
    # verdict on the session, not a report from the charger.
    _watch_readout(hass, entry, load, load_rt, connector_status)

    # Draw-settle detection: the measured draw is trusted as the EVSE's real
    # footprint - freeing the unused gap to lower-priority loads - only when
    # two conditions hold: it has held steady for SETTLE_DRAW_SECONDS
    # *and* it is measurably below the permit we offered last cycle. A car
    # drawing essentially what we offered (util ≈ 1.0) is using all of it, so
    # we keep treating the permit as its footprint. A still-ramping car keeps
    # changing and stays unsettled. Unmetered loads have no draw to settle.
    measured_draw = max(load.l1_current, load.l2_current, load.l3_current)
    # Blind: the "draw" is our own command echoed back, so it settling proves
    # nothing about the car - the footprint rule for blind loads lives in
    # target_calculator._pool_deduction instead.
    #
    # And the window runs only while the connector is Charging, opening afresh
    # each time charging (re)starts. A draw can settle at the car's own ceiling
    # only while energy flows; the 0 A of an empty connector, a car waiting to
    # start (Preparing) or one paused (SuspendedEV, SuspendedEVSE) is steady
    # for that reason alone. Counted, 15 s of it settled at 0 A and stayed
    # settled into the first Charging cycle, while the meter still read 0: a
    # footprint of 0 A instead of the car's minimum, and pass 2 then permitted
    # the minimum ON TOP of the whole pool - 23 A against a 17 A allowance on
    # a 25 A breaker with an 8 A house (dev/tests/test_start_settle.py).
    if load.unmetered or load.draw_blind or connector_status != "Charging":
        load.draw_settled = False
        load_rt.pop("_settle_last_draw", None)
        load_rt.pop("_settle_since", None)
    else:
        last_draw = load_rt.get("_settle_last_draw")
        if last_draw is not None and abs(measured_draw - last_draw) <= SETTLE_DRAW_TOLERANCE:
            # Timestamped rather than counted: a count of cycles is a duration
            # only once you know the refresh rate. Same shape as the
            # SuspendedEV idle marker above.
            if "_settle_since" not in load_rt:
                load_rt["_settle_since"] = time.monotonic()
        else:
            load_rt.pop("_settle_since", None)
        load_rt["_settle_last_draw"] = measured_draw
        since = load_rt.get("_settle_since")
        steady = since is not None and time.monotonic() - since >= settle_seconds
        under_permit = (
            measured_draw + SETTLE_PERMIT_MARGIN
            < load_rt.get("_last_permit", 0)
        )
        load.draw_settled = steady and under_permit

    # SuspendedEV grace period: car may briefly pause during normal charging (BMS
    # balancing). Only treat as inactive after SUSPENDED_EV_IDLE_TIMEOUT seconds
    # of continuous SuspendedEV + near-zero draw.
    total_draw = load.l1_current + load.l2_current + load.l3_current
    if connector_status == "SuspendedEV" and total_draw < 1.0:
        if "_suspended_ev_since" not in load_rt:
            load_rt["_suspended_ev_since"] = time.monotonic()
        idle_duration = time.monotonic() - load_rt["_suspended_ev_since"]
        if idle_duration >= SUSPENDED_EV_IDLE_TIMEOUT:
            _LOGGER.debug(
                "EVSE %s: SuspendedEV idle for %.0fs (>%ds) - treating as inactive",
                load_entity_id,
                idle_duration,
                SUSPENDED_EV_IDLE_TIMEOUT,
            )
            load.connector_status = "Finishing"
    else:
        load_rt.pop("_suspended_ev_since", None)

    _LOGGER.debug(
        "  EVSE %s [%s]: %s-%sA %dph(hw) L1->%s/L2->%s/L3->%s mask=%s(%dph) "
        "prio=%d [%s] draw=L1:%s/L2:%s/L3:%s",
        load_entity_id,
        mode.key,
        _fv(min_current),
        _fv(max_current),
        phases,
        l1_phase,
        l2_phase,
        l3_phase,
        load.active_phases_mask,
        len(load.active_phases_mask) if load.active_phases_mask else 0,
        priority,
        load.connector_status,
        _fv(load.l1_current),
        _fv(load.l2_current),
        _fv(load.l3_current),
    )
    return load


def _phase_draw(draw_w, connected_to_phase, voltage):
    """Distribute a binary load's total draw (W) across its connected phases.

    Returns a dict of LoadContext kwargs (l1/l2/l3 phase + current) so the
    load's actual draw is counted in Total Managed Power and subtracted by
    the consumption feedback loop, exactly like an EVSE's metered draw.
    """
    chars = list(connected_to_phase) or ["A"]
    phases = len(chars)
    per_phase = draw_w / (voltage * phases) if voltage > 0 and phases > 0 else 0
    return {
        "l1_phase": chars[0],
        "l2_phase": chars[1] if phases > 1 else "B",
        "l3_phase": chars[2] if phases > 2 else "C",
        "l1_current": per_phase if phases >= 1 else 0,
        "l2_current": per_phase if phases >= 2 else 0,
        "l3_current": per_phase if phases >= 3 else 0,
    }


def _build_plug_load(hass, entry, voltage, load_entity_id, priority):
    """Build a LoadContext for a smart load (plug) device."""
    load_rt = hass.data[DOMAIN]["loads"].get(entry.entry_id, {})
    slider_power = load_rt.get("device_power", None)
    config_power = get_entry_value(
        entry, CONF_PLUG_POWER_RATING, DEFAULT_PLUG_POWER_RATING
    )
    plug_max_current = get_entry_value(
        entry, CONF_PLUG_MAX_CURRENT, DEFAULT_PLUG_MAX_CURRENT
    )
    # Set power: the runtime slider if set, else the configured rating.
    power_rating = (
        slider_power if slider_power is not None and slider_power > 0 else config_power
    )

    connected_to_phase = get_entry_value(entry, CONF_CONNECTED_TO_PHASE, "A") or "A"
    phases = len(connected_to_phase)

    plug_switch_entity = entry.data.get(CONF_PLUG_SWITCH_ENTITY_ID)
    plug_switch_state = (
        hass.states.get(plug_switch_entity) if plug_switch_entity else None
    )
    power_monitor_entity = get_entry_value(
        entry, CONF_PLUG_POWER_MONITOR_ENTITY_ID, None
    )

    power_draw = None
    monitor_unreadable = False
    if power_monitor_entity:
        raw_power_draw = _read_entity(
            hass, power_monitor_entity, 0, unit="W"
        )  # Convert kW→W if needed
        # A configured monitor that cannot be read leaves this plug's draw at
        # 0 W. That is the conservative figure for the feedback loop and stays,
        # but it is invented - see LoadContext.draw_assumed at the end.
        monitor_unreadable = raw_power_draw is _UNAVAILABLE
        power_draw = _coerce(raw_power_draw)

    # On/off: the switch is authoritative when present; without a switch the
    # power monitor decides; with neither, assume the load is on.
    if plug_switch_state is not None:
        on = plug_switch_state.state == "on"
    elif power_monitor_entity:
        on = power_draw is not None and power_draw > 10
    else:
        on = True
    connector_status = "Charging" if on else "Available"

    # Learn the device's real power from the monitor - but only while the plug
    # is on AND the reading is steady. A transient reading (a switch-off dip, a
    # compressor inrush spike) must not overwrite the configured rating, so we
    # require N consecutive readings within ±20 % of the *first* one before
    # committing the value. The candidate and its run length live in load_rt
    # so they survive across calculation cycles.
    _POWER_STABLE_CYCLES = 3
    _POWER_STABLE_TOLERANCE = 0.20
    if power_monitor_entity and on and power_draw and power_draw > 10:
        candidate = load_rt.get("power_candidate")
        if candidate is None or candidate <= 0:
            # First reading of a run - remember it as the yardstick to compare
            # the next cycles against.
            load_rt["power_candidate"] = power_draw
            load_rt["power_stable_count"] = 1
        elif abs(power_draw - candidate) <= candidate * _POWER_STABLE_TOLERANCE:
            stable_count = load_rt.get("power_stable_count", 0) + 1
            load_rt["power_stable_count"] = stable_count
            if stable_count >= _POWER_STABLE_CYCLES:
                power_rating = power_draw
                load_rt["device_power"] = math.ceil(power_draw / 10) * 10
        else:
            # The reading moved off the candidate - the run is broken, restart
            # counting against the new value.
            load_rt["power_candidate"] = power_draw
            load_rt["power_stable_count"] = 1
    else:
        load_rt["power_candidate"] = None
        load_rt["power_stable_count"] = 0

    # Clamp to 0.1 A so the value survives the calculator's round(x, 1) and the
    # plug is not permanently locked off due to a very low power rating.
    equivalent_current = max(0.1, power_rating / (voltage * phases)) if voltage > 0 else 0

    # Actual draw - the measured draw while the plug is on (else the set power
    # if there is no monitor), 0 when off. Populates the load's per-phase
    # currents so the plug counts toward Total Managed Power and the feedback.
    if power_monitor_entity:
        actual_draw_w = power_draw if (on and power_draw and power_draw > 0) else 0
    else:
        actual_draw_w = power_rating if on else 0

    # Resolve the per-load operating mode from runtime data.
    mode = resolve_operating_mode(
        DEVICE_TYPE_PLUG,
        load_rt.get("operating_mode", DEFAULT_OPERATING_MODE_PLUG.key),
    )

    load = LoadContext(
        load_id=entry.entry_id,
        entity_id=load_entity_id,
        min_current=equivalent_current,
        max_current=equivalent_current,
        phases=phases,
        priority=priority,
        active_phases_mask=connected_to_phase,
        connector_status=connector_status,
        # "Hands off" reaches the calculation too - see LoadContext.
        dynamic_control=load_rt.get("dynamic_control", True),
        device_type=DEVICE_TYPE_PLUG,
        operating_mode=mode.key,
        mode_behavior=behavior_for(mode),
        mode_priority=mode.priority,
        rated_current=plug_max_current,
        excess_claim_current=(
            equivalent_current if behavior_for(mode) == BEHAVIOR_BINARY_EXCESS else 0.0
        ),
        draw_assumed=monitor_unreadable,
        **_phase_draw(actual_draw_w, connected_to_phase, voltage),
    )
    _LOGGER.debug(
        "  Plug %s [%s]: %.0fW on %s prio=%d [%s]%s",
        load_entity_id,
        mode.key,
        power_rating,
        connected_to_phase,
        priority,
        connector_status,
        " (metered)" if power_monitor_entity else "",
    )
    return load


def _station_at_its_reserve(hass, entry, load_rt, soc) -> bool:
    """Whether the station's SOC has reached the backup reserve it is holding -
    the point where it stops drawing from the wall regardless of the commanded
    speed. Judged against the higher of the live reserve entity and the reserve
    the control last wrote. Unknown SOC or reserve → not at it (the old behaviour)."""
    if soc is None:
        return False
    reserve = _coerce(
        _read_entity(
            hass, get_entry_value(entry, CONF_STATION_RESERVE_ENTITY_ID, None), None
        ),
        None,
    )
    written = load_rt.get("station_reserve")
    candidates = [r for r in (reserve, written) if r is not None]
    if not candidates:
        return False
    try:
        # The higher of the two: a reserve we have just raised to charge may
        # not have reached the device yet (BLE), and until it has, the live
        # entity still shows the lower normal level - which is not a full
        # station, only a write in flight.
        return float(soc) >= max(float(r) for r in candidates)
    except (TypeError, ValueError):
        return False


def _build_power_station_load(hass, entry, voltage, load_entity_id, priority):
    """Build a LoadContext for a portable power station (modulating load).

    The station charges at a commandable rate, so to the engine it is an EVSE
    without the OCPP: min/max current from the *configured* watt bounds (not the
    device's own, so it can be held below what the hardware allows), and the
    allocation is written back as an AC charging speed.

    Its managed draw is the charging component only - ``ac_input - ac_output``.
    Whatever is plugged into the station passes through to its outputs and is
    ordinary household consumption, not ours: counting it here would let the
    feedback loop add it back as available surplus.
    """
    load_rt = hass.data[DOMAIN]["loads"].get(entry.entry_id, {})

    # Charge bounds: runtime sliders win over the configured values, mirroring
    # the EVSE's min/max current.
    config_min = get_entry_value(
        entry, CONF_STATION_MIN_CHARGE_POWER, DEFAULT_STATION_MIN_CHARGE_POWER
    )
    config_max = get_entry_value(
        entry, CONF_STATION_MAX_CHARGE_POWER, DEFAULT_STATION_MAX_CHARGE_POWER
    )
    min_power = load_rt.get("station_min_charge_power") or config_min
    max_power = load_rt.get("station_max_charge_power") or config_max
    if max_power < min_power:
        max_power = min_power

    connected_to_phase = get_entry_value(entry, CONF_CONNECTED_TO_PHASE, "A") or "A"
    phases = len(connected_to_phase)
    denom = voltage * phases
    min_current = min_power / denom if denom > 0 else 0
    max_current = max_power / denom if denom > 0 else 0

    speed_entity = entry.data.get(CONF_STATION_CHARGE_SPEED_ENTITY_ID)
    soc = _coerce(
        _read_entity(
            hass, get_entry_value(entry, CONF_STATION_BATTERY_LEVEL_ENTITY_ID, None), None
        ),
        None,
    )
    charge_limit = _coerce(
        _read_entity(
            hass, get_entry_value(entry, CONF_STATION_CHARGE_LIMIT_ENTITY_ID, None), None
        ),
        None,
    )
    if charge_limit is None:
        charge_limit = DEFAULT_STATION_CHARGE_LIMIT

    # Status. The station is inactive - and its power goes back to other loads -
    # once it has reached its own charge limit. It is *unavailable* when the
    # control entity is gone: these integrations talk BLE, which allows one
    # connection at a time, so opening the vendor app silently takes control
    # away from Home Assistant. Continuing to allocate power to a station we
    # cannot command would strand that power.
    speed_state = hass.states.get(speed_entity) if speed_entity else None
    if units.is_unavailable(speed_state):
        connector_status = "Unavailable"
    elif soc is not None and soc >= charge_limit:
        connector_status = "Available"
    else:
        connector_status = "Charging"

    # Managed draw: the charging component of the wall draw. Falls back to the
    # commanded speed when the AC sensors aren't configured, and only while the
    # station was last told to charge - an idle station draws nothing.
    ac_in_entity = get_entry_value(entry, CONF_STATION_AC_INPUT_ENTITY_ID, None)
    ac_out_entity = get_entry_value(entry, CONF_STATION_AC_OUTPUT_ENTITY_ID, None)
    raw_ac_in = _read_entity(hass, ac_in_entity, None, unit="W")
    raw_ac_out = _read_entity(hass, ac_out_entity, None, unit="W")
    ac_in = _coerce(raw_ac_in, None)
    ac_out = _coerce(raw_ac_out, None)
    # Both sensors configured is the only case that can produce a MEASURED
    # draw, so it is the only case where losing one of them costs a
    # measurement: the commanded-speed fallback below is an estimate of what we
    # asked for, not of what the wall is delivering (conversion losses, a
    # station tapering near full). With the sensors absent by configuration the
    # fallback is the designed answer and nothing is flagged.
    draw_assumed = bool(ac_in_entity and ac_out_entity) and _UNAVAILABLE in (
        raw_ac_in,
        raw_ac_out,
    )
    if ac_in is not None and ac_out is not None:
        actual_draw_w = max(0.0, ac_in - abs(ac_out))
    elif load_rt.get("station_charging") and not _station_at_its_reserve(
        hass, entry, load_rt, soc
    ):
        # _read_entity parses and unit-converts; _coerce only maps the
        # unavailable sentinel, so the raw state string must not go through it.
        actual_draw_w = _coerce(_read_entity(hass, speed_entity, 0, unit="W"), 0) or 0
    else:
        # Idle - or commanded to charge but already at the reserve, where the
        # station's own gate stops the wall draw whatever speed we wrote. Adding
        # the commanded speed back then would credit the site with surplus it
        # does not have (a full station kept the Excess verdict on, 2026-09-03).
        actual_draw_w = 0

    mode = resolve_operating_mode(
        DEVICE_TYPE_POWER_STATION,
        load_rt.get("operating_mode", DEFAULT_OPERATING_MODE_POWER_STATION.key),
    )
    # Storm reserve overrides the mode: filling a backup reserve only from
    # surplus is not a reserve, so it competes as a must-run load.
    if load_rt.get("station_storm_reserve"):
        mode = STATION_MODE_STANDARD

    load = LoadContext(
        load_id=entry.entry_id,
        entity_id=load_entity_id,
        min_current=min_current,
        max_current=max_current,
        phases=phases,
        priority=priority,
        active_phases_mask=connected_to_phase,
        connector_status=connector_status,
        # "Hands off" reaches the calculation too - see LoadContext.
        dynamic_control=load_rt.get("dynamic_control", True),
        device_type=DEVICE_TYPE_POWER_STATION,
        operating_mode=mode.key,
        mode_behavior=behavior_for(mode),
        mode_priority=mode.priority,
        rated_current=max_current,
        excess_claim_current=(
            min_current
            if behavior_for(mode) == BEHAVIOR_EXCESS
            and connector_status not in INACTIVE_STATUSES
            else 0.0
        ),
        draw_assumed=draw_assumed,
        **_phase_draw(actual_draw_w, connected_to_phase, voltage),
    )
    _LOGGER.debug(
        "  Station %s [%s]: %.0f-%.0fW on %s prio=%d soc=%s%% limit=%s%% "
        "draw=%.0fW [%s]",
        load_entity_id,
        mode.key,
        min_power,
        max_power,
        connected_to_phase,
        priority,
        _fv(soc),
        _fv(charge_limit),
        actual_draw_w,
        connector_status,
    )
    return load


def _build_hot_water_tank_load(hass, entry, voltage, load_entity_id, priority):
    """Build a LoadContext for a hot water tank (thermostat-driven binary load).

    To the engine the tank is a smart load (plug): a fixed-power binary draw.
    The thermostat (a climate or water_heater entity) owns temperature regulation; the HA layer reads its
    hvac_action and writes the setpoint. Tank operating modes (Freeze
    Protection / Normal / Solar Priority / Solar Excess) map to engine modes
    here.
    """
    load_rt = hass.data[DOMAIN]["loads"].get(entry.entry_id, {})

    # Connector status from the thermostat's hvac_action (set below, once a
    # water heater's is read off its power sensor): "idle" means the tank is
    # satisfied - mark it inactive so the engine reallocates that power.
    # Anything else is treated as an active load.
    climate_entity = entry.data.get(CONF_CLIMATE_ENTITY_ID)
    climate_state = hass.states.get(climate_entity) if climate_entity else None
    hvac_action = (
        climate_state.attributes.get("hvac_action") if climate_state else None
    )

    # Set power: the runtime slider if set, else the configured element
    # power. A configured tank power sensor overrides it with the live draw
    # while the element is heating, and is written back so the slider learns.
    #
    # The heating gate is essential: standby electronics or a circulation pump
    # keep the sensor at a few watts with the element off, and learning from
    # that would shrink the tank's equivalent_current to the 0.1 A floor -
    # a 2 kW load booked as free. With no hvac_action to confirm heating we
    # keep the configured rating rather than learn from an unknown state.
    element_power = get_entry_value(
        entry, CONF_HEATING_ELEMENT_POWER, DEFAULT_HEATING_ELEMENT_POWER
    )
    slider_power = load_rt.get("device_power")
    power_rating = slider_power if slider_power else element_power
    power_entity = get_entry_value(entry, CONF_TANK_POWER_ENTITY_ID, None)
    live = None
    power_unreadable = False
    if power_entity:
        raw_live = _read_entity(hass, power_entity, 0, unit="W")
        # Configured but unreadable: the 0 W below is invented, so a heating
        # tank must not be published as drawing nothing (draw_assumed).
        power_unreadable = raw_live is _UNAVAILABLE
        live = _coerce(raw_live)
    # A water heater reports no hvac_action, so its power sensor says whether
    # the element is heating, on the same 10 W line the learning below uses.
    # Without one nothing does, and the tank counts as calling for heat.
    if climate_entity and climate_entity.startswith("water_heater.") and climate_state:
        if climate_state.state == "off":
            hvac_action = "off"
        elif power_entity and not power_unreadable:
            hvac_action = "heating" if live and live > 10 else "idle"
    load_rt["tank_hvac_action"] = hvac_action
    connector_status = "Available" if hvac_action == "idle" else "Charging"
    if live and live > 10 and hvac_action == "heating":
        power_rating = live
        load_rt["device_power"] = round(live, 0)

    connected_to_phase = get_entry_value(entry, CONF_CONNECTED_TO_PHASE, "A") or "A"
    phases = len(connected_to_phase)

    equivalent_current = power_rating / (voltage * phases) if voltage > 0 else 0

    # Actual draw - the element only consumes while the thermostat is calling
    # for heat. Use the live power sensor if configured, else the element
    # rating while hvac_action is "heating". Populates per-phase currents so
    # the tank counts toward Total Managed Power and the feedback loop.
    if power_entity:
        actual_draw_w = live if (live and live > 0) else 0
    else:
        actual_draw_w = power_rating if hvac_action == "heating" else 0

    # Resolve the tank's operating mode. Its behavior (Freeze Protection /
    # Normal are must-run Full Power; Solar Priority follows the sun) is mapped
    # centrally in const/modes.py. resolve_tank_setpoint() independently picks
    # *which* setpoint (away/normal/boost) to aim at - the mode behavior only
    # decides how the tank competes for power, not whether it runs.
    mode = resolve_operating_mode(
        DEVICE_TYPE_HOT_WATER_TANK,
        load_rt.get("operating_mode", DEFAULT_OPERATING_MODE_HOT_WATER_TANK.key),
    )

    # Cold-tank promotion: a Solar Priority tank below its normal temperature is
    # bumped to the Normal urgency tier so it beats other solar-priority loads
    # in contention. Only the tier is raised - the behavior stays Solar Priority,
    # so the tank still draws from solar + above-min battery and never deep-cycles
    # the bank below its minimum SOC. Toggleable per tank (default on).
    raw_temp = (
        climate_state.attributes.get("current_temperature") if climate_state else None
    )
    try:
        current_temp = float(raw_temp) if raw_temp is not None else None
    except (TypeError, ValueError):
        current_temp = None
    normal_temp = load_rt.get("tank_normal_temperature") or get_entry_value(
        entry, CONF_TANK_NORMAL_TEMPERATURE, DEFAULT_TANK_NORMAL_TEMPERATURE
    )
    boost_temp = load_rt.get("tank_boost_temperature") or get_entry_value(
        entry, CONF_TANK_BOOST_TEMPERATURE, DEFAULT_TANK_BOOST_TEMPERATURE
    )
    away_temp = load_rt.get("tank_away_temperature") or get_entry_value(
        entry, CONF_TANK_AWAY_TEMPERATURE, DEFAULT_TANK_AWAY_TEMPERATURE
    )

    # Surplus demotion: a tank aiming at boost is heating on energy the site
    # would otherwise dump, so it competes at the Excess tier instead of its
    # mode's own. The label is whatever the command layer last wrote - one cycle
    # stale, which is the honest reading. Resolving it here instead would have to
    # use PRE-feedback export (loads are built before the feedback loop and
    # the excess latch), and that figure is already depressed by the tank's own
    # draw, so a boosting tank would look like it wasn't. The lag only shifts
    # allocation order, and only while the site is contended.
    setpoint_label = load_rt.get("tank_setpoint_label")

    mode_priority, elevated = resolve_tank_mode_priority(
        mode.key,
        mode.priority,
        current_temp,
        normal_temp,
        get_entry_value(
            entry,
            CONF_TANK_PRIORITIZE_BELOW_NORMAL,
            DEFAULT_TANK_PRIORITIZE_BELOW_NORMAL,
        ),
        setpoint_label,
    )
    load_rt["tank_priority_elevated"] = elevated

    # ...and its BEHAVIOR the same way, from the same label. The tier demotion
    # above says a boosting tank is opportunistic; this is what makes the
    # allocator agree, so the tank is sized by the surplus it claims against
    # instead of taking its rating from the physical pool regardless. Below its
    # mode's own floor temperature it stays full-power and unconditional - see
    # tank_boost_is_opportunistic for why that guard is not optional.
    opportunistic = tank_boost_is_opportunistic(
        mode.key,
        setpoint_label,
        current_temp,
        away_temp if mode.key == TANK_MODE_FREEZE_PROTECTION.key else normal_temp,
    )
    mode_behavior = BEHAVIOR_BINARY_EXCESS if opportunistic else behavior_for(mode)

    load = LoadContext(
        load_id=entry.entry_id,
        entity_id=load_entity_id,
        min_current=equivalent_current,
        max_current=equivalent_current,
        phases=phases,
        priority=priority,
        active_phases_mask=connected_to_phase,
        connector_status=connector_status,
        # "Hands off" reaches the calculation too - see LoadContext.
        dynamic_control=load_rt.get("dynamic_control", True),
        device_type=DEVICE_TYPE_HOT_WATER_TANK,
        operating_mode=mode.key,
        mode_behavior=mode_behavior,
        mode_priority=mode_priority,
        rated_current=equivalent_current,
        # The verdict starts this tank (its setpoint jumps to boost) whenever
        # its mode rides surplus and it is below the boost temperature - so it
        # claims its rating from the verdict-on cycle, thermostat idle or not.
        excess_claim_current=(
            equivalent_current
            if mode.key in (TANK_MODE_FREEZE_PROTECTION.key, TANK_MODE_NORMAL.key)
            and current_temp is not None
            and current_temp < boost_temp
            else 0.0
        ),
        draw_assumed=power_unreadable,
        **_phase_draw(actual_draw_w, connected_to_phase, voltage),
    )
    _LOGGER.debug(
        "  Tank %s [%s]: %.0fW on %s prio=%d tier=%d (%s) [%s]",
        load_entity_id,
        mode.key,
        power_rating,
        connected_to_phase,
        priority,
        mode_priority,
        setpoint_label,
        connector_status,
    )
    return load


def _add_loads_to_site(hass, site, hub_entry_id, load_entries=None,
                       settle_seconds=SETTLE_DRAW_SECONDS):
    """Build LoadContext objects for all loads and add them to the site.

    ``load_entries`` overrides the hub's registered loads (used by tests and
    by any caller that already knows the entries); None reads the registry.
    ``settle_seconds`` is the hub's draw-settle dial (Filters page); the
    default is the constant it overrides.
    """
    if load_entries is None:
        loads = get_loads_for_hub(hass, hub_entry_id)
    else:
        loads = load_entries

    for entry in loads:
        device_type = entry.data.get(CONF_DEVICE_TYPE, DEVICE_TYPE_EVSE)
        load_entity_id = entry.data.get(CONF_ENTITY_ID, f"load_{entry.entry_id}")
        priority = get_entry_value(
            entry, CONF_LOAD_PRIORITY, DEFAULT_LOAD_PRIORITY
        )

        if device_type == DEVICE_TYPE_PLUG:
            load = _build_plug_load(
                hass, entry, site.voltage, load_entity_id, priority
            )
        elif device_type == DEVICE_TYPE_HOT_WATER_TANK:
            load = _build_hot_water_tank_load(
                hass, entry, site.voltage, load_entity_id, priority
            )
        elif device_type == DEVICE_TYPE_POWER_STATION:
            load = _build_power_station_load(
                hass, entry, site.voltage, load_entity_id, priority
            )
        else:
            load = _build_evse_load(
                hass, entry, site.voltage, load_entity_id, priority,
                settle_seconds=settle_seconds,
            )

        # Clamp active_phases_mask to only include phases that exist on the site
        site_phases = {
            p
            for p, v in zip(
                _PHASE_LABELS,
                (site.consumption.a, site.consumption.b, site.consumption.c),
            )
            if v is not None
        }
        mask_phases = (
            set(load.active_phases_mask) if load.active_phases_mask else set()
        )
        if mask_phases and not mask_phases.issubset(site_phases):
            clamped = "".join(sorted(mask_phases & site_phases)) or load.l1_phase
            _LOGGER.warning(
                "%s %s: phase mask %s includes phases not on site (%s) - clamping to %s",
                "Plug" if load.device_type == DEVICE_TYPE_PLUG else "EVSE",
                load_entity_id,
                load.active_phases_mask,
                "".join(sorted(site_phases)),
                clamped,
            )
            load.active_phases_mask = clamped

        site.loads.append(load)

def _build_circuit_groups(hass, hub_entry_id):
    """Build CircuitGroup objects from config entries for this hub.

    Returns list of CircuitGroup model objects for the calculation engine.
    """
    group_entries = get_groups_for_hub(hass, hub_entry_id)
    # Build set of valid load entry_ids for member validation
    valid_load_ids = {
        e.entry_id
        for e in hass.config_entries.async_entries(DOMAIN)
        if e.data.get(ENTRY_TYPE) == ENTRY_TYPE_LOAD
    }
    groups = []
    for entry in group_entries:
        if entry is None:
            continue
        options = {**entry.data, **entry.options}
        current_limit = options.get(
            CONF_CIRCUIT_GROUP_CURRENT_LIMIT, DEFAULT_CIRCUIT_GROUP_CURRENT_LIMIT
        )
        raw_member_ids = options.get(CONF_CIRCUIT_GROUP_MEMBERS, [])
        # Filter out stale member references (deleted loads)
        member_ids = [mid for mid in raw_member_ids if mid in valid_load_ids]
        stale = set(raw_member_ids) - set(member_ids)
        if stale:
            _LOGGER.warning(
                "Circuit group '%s': removed %d stale member(s) - entries no longer exist",
                options.get(CONF_NAME, "Circuit Group"),
                len(stale),
            )
        group = CircuitGroup(
            group_id=entry.entry_id,
            name=options.get(CONF_NAME, "Circuit Group"),
            current_limit=float(current_limit),
            member_ids=member_ids,
        )
        groups.append(group)
        _LOGGER.debug(
            "  Circuit group '%s': limit=%.0fA, members=%s",
            group.name,
            group.current_limit,
            member_ids,
        )
    return groups
