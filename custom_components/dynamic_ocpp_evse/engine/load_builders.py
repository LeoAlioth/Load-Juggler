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

from ..calculations import LoadContext, CircuitGroup
from ..const import (
    CONF_CHARGER_ID,
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
    CONF_EVSE_MINIMUM_CHARGE_CURRENT,
    CONF_EVSE_POWER_IMPORT_ENTITY_ID,
    CONF_HEATING_ELEMENT_POWER,
    CONF_NAME,
    CONF_PHASES,
    CONF_PLUG_MAX_CURRENT,
    CONF_PLUG_POWER_MONITOR_ENTITY_ID,
    CONF_PLUG_POWER_RATING,
    CONF_PLUG_SWITCH_ENTITY_ID,
    CONF_STATION_AC_INPUT_ENTITY_ID,
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
    SETTLE_DRAW_CYCLES,
    SETTLE_DRAW_TOLERANCE,
    SETTLE_PERMIT_MARGIN,
    STATION_MODE_STANDARD,
    SUSPENDED_EV_IDLE_TIMEOUT,
    behavior_for,
    resolve_operating_mode,
    resolve_tank_mode_priority,
)
from ..helpers import get_entry_value
from ..registry import get_loads_for_hub, get_groups_for_hub
from .. import units
from .readers import (
    _PHASE_LABELS,
    _clamp_reported_phase_draw,
    _coerce,
    _fv,
    _read_entity,
    _read_phase_attr,
)

_LOGGER = logging.getLogger(__name__)


def _build_evse_load(hass, entry, voltage, load_entity_id, priority):
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
    # makes every permit nonsensical, so collapse it — downwards, so a bad pair
    # can never authorise MORE current than the configured maximum. (The power
    # station builder below resolves its own inverted pair the other way; its
    # min is a trickle floor, not a hardware limit.)
    if min_current > max_current:
        _LOGGER.warning(
            "%s: min_current %.1fA is above max_current %.1fA — using %.1fA for both",
            load_entity_id, min_current, max_current, max_current,
        )
        min_current = max_current

    phases = int(get_entry_value(entry, CONF_PHASES, 3) or 3)

    # Get OCPP device ID for sensor lookups (different from Load Juggler entity_id)
    ocpp_device_id = entry.data.get(CONF_CHARGER_ID, load_entity_id)

    # Read connector status from OCPP entity
    connector_status_entity = f"sensor.{ocpp_device_id}_status_connector"
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
        operating_mode=mode.key,
        mode_behavior=behavior_for(mode),
        mode_priority=mode.priority,
        rated_current=max_current,
        l1_phase=l1_phase,
        l2_phase=l2_phase,
        l3_phase=l3_phase,
    )

    # Get OCPP current draw for this load with fallback chain:
    # 1. Current Import per-phase entities (sensor.{id}_current_import_l1/l2/l3)
    # 2. Current Import entity (per-phase attributes or total)
    # 3. Power Active Import (convert W → A)
    evse_import = entry.data.get(CONF_EVSE_CURRENT_IMPORT_ENTITY_ID)
    evse_import_l1 = entry.data.get(CONF_EVSE_CURRENT_IMPORT_L1_ENTITY_ID)
    evse_import_l2 = entry.data.get(CONF_EVSE_CURRENT_IMPORT_L2_ENTITY_ID)
    evse_import_l3 = entry.data.get(CONF_EVSE_CURRENT_IMPORT_L3_ENTITY_ID)
    evse_power_import = entry.data.get(CONF_EVSE_POWER_IMPORT_ENTITY_ID)
    current_draw = None

    # Try per-phase current import entities first (separate sensors for each phase)
    if evse_import_l1 or evse_import_l2 or evse_import_l3:
        l1_val = (
            _coerce(_read_entity(hass, evse_import_l1, None), None)
            if evse_import_l1
            else None
        )
        l2_val = (
            _coerce(_read_entity(hass, evse_import_l2, None), None)
            if evse_import_l2
            else None
        )
        l3_val = (
            _coerce(_read_entity(hass, evse_import_l3, None), None)
            if evse_import_l3
            else None
        )

        if l1_val is not None or l2_val is not None or l3_val is not None:
            load.l1_current = l1_val if l1_val is not None else 0
            load.l2_current = l2_val if l2_val is not None else 0
            load.l3_current = l3_val if l3_val is not None else 0
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

    if current_draw:
        _LOGGER.debug(
            "EVSE %s: Current draw source: %s", load_entity_id, current_draw
        )

    # No current-import source found — the engine cannot see this EVSE's real
    # draw, so its footprint falls back to its permit (it may reserve more than
    # it uses). Plugs and tanks always carry a correct draw and are never
    # flagged unmetered.
    load.unmetered = current_draw is None

    # Draw-settle detection: the measured draw is trusted as the EVSE's real
    # footprint — freeing the unused gap to lower-priority loads — only when
    # two conditions hold: it has held steady for SETTLE_DRAW_CYCLES cycles
    # *and* it is measurably below the permit we offered last cycle. A car
    # drawing essentially what we offered (util ≈ 1.0) is using all of it, so
    # we keep treating the permit as its footprint. A still-ramping car keeps
    # changing and stays unsettled. Unmetered loads have no draw to settle.
    measured_draw = max(load.l1_current, load.l2_current, load.l3_current)
    if load.unmetered:
        load.draw_settled = False
        load_rt.pop("_settle_last_draw", None)
        load_rt.pop("_settle_count", None)
    else:
        last_draw = load_rt.get("_settle_last_draw")
        if last_draw is not None and abs(measured_draw - last_draw) <= SETTLE_DRAW_TOLERANCE:
            load_rt["_settle_count"] = load_rt.get("_settle_count", 0) + 1
        else:
            load_rt["_settle_count"] = 0
        load_rt["_settle_last_draw"] = measured_draw
        steady = load_rt["_settle_count"] >= SETTLE_DRAW_CYCLES
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
                "EVSE %s: SuspendedEV idle for %.0fs (>%ds) — treating as inactive",
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
    if power_monitor_entity:
        power_draw = _coerce(
            _read_entity(hass, power_monitor_entity, 0, unit="W")
        )  # Convert kW→W if needed

    # On/off: the switch is authoritative when present; without a switch the
    # power monitor decides; with neither, assume the load is on.
    if plug_switch_state is not None:
        on = plug_switch_state.state == "on"
    elif power_monitor_entity:
        on = power_draw is not None and power_draw > 10
    else:
        on = True
    connector_status = "Charging" if on else "Available"

    # Learn the device's real power from the monitor — but only while the plug
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
            # First reading of a run — remember it as the yardstick to compare
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
            # The reading moved off the candidate — the run is broken, restart
            # counting against the new value.
            load_rt["power_candidate"] = power_draw
            load_rt["power_stable_count"] = 1
    else:
        load_rt["power_candidate"] = None
        load_rt["power_stable_count"] = 0

    # Clamp to 0.1 A so the value survives the calculator's round(x, 1) and the
    # plug is not permanently locked off due to a very low power rating.
    equivalent_current = max(0.1, power_rating / (voltage * phases)) if voltage > 0 else 0

    # Actual draw — the measured draw while the plug is on (else the set power
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
        device_type=DEVICE_TYPE_PLUG,
        operating_mode=mode.key,
        mode_behavior=behavior_for(mode),
        mode_priority=mode.priority,
        rated_current=plug_max_current,
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


def _build_power_station_load(hass, entry, voltage, load_entity_id, priority):
    """Build a LoadContext for a portable power station (modulating load).

    The station charges at a commandable rate, so to the engine it is an EVSE
    without the OCPP: min/max current from the *configured* watt bounds (not the
    device's own, so it can be held below what the hardware allows), and the
    allocation is written back as an AC charging speed.

    Its managed draw is the charging component only — ``ac_input - ac_output``.
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

    # Status. The station is inactive — and its power goes back to other loads —
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
    # station was last told to charge — an idle station draws nothing.
    ac_in = _coerce(
        _read_entity(
            hass,
            get_entry_value(entry, CONF_STATION_AC_INPUT_ENTITY_ID, None),
            None,
            unit="W",
        ),
        None,
    )
    ac_out = _coerce(
        _read_entity(
            hass,
            get_entry_value(entry, CONF_STATION_AC_OUTPUT_ENTITY_ID, None),
            None,
            unit="W",
        ),
        None,
    )
    if ac_in is not None and ac_out is not None:
        actual_draw_w = max(0.0, ac_in - abs(ac_out))
    elif load_rt.get("station_charging"):
        # _read_entity parses and unit-converts; _coerce only maps the
        # unavailable sentinel, so the raw state string must not go through it.
        actual_draw_w = _coerce(_read_entity(hass, speed_entity, 0, unit="W"), 0) or 0
    else:
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
        device_type=DEVICE_TYPE_POWER_STATION,
        operating_mode=mode.key,
        mode_behavior=behavior_for(mode),
        mode_priority=mode.priority,
        rated_current=max_current,
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
    """Build a LoadContext for a hot water tank (climate-driven binary load).

    To the engine the tank is a smart load (plug): a fixed-power binary draw.
    The climate entity owns temperature regulation; the HA layer reads its
    hvac_action and writes the setpoint. Tank operating modes (Freeze
    Protection / Normal / Solar Priority / Solar Excess) map to engine modes
    here.
    """
    load_rt = hass.data[DOMAIN]["loads"].get(entry.entry_id, {})

    # Connector status from the climate entity's hvac_action: a thermostat
    # reporting "idle" means the tank is satisfied — mark it inactive so the
    # engine reallocates that power. Anything else is treated as an active load.
    climate_entity = entry.data.get(CONF_CLIMATE_ENTITY_ID)
    connector_status = "Charging"
    climate_state = hass.states.get(climate_entity) if climate_entity else None
    hvac_action = (
        climate_state.attributes.get("hvac_action") if climate_state else None
    )
    if hvac_action == "idle":
        connector_status = "Available"

    # Set power: the runtime slider if set, else the configured element
    # power. A configured tank power sensor overrides it with the live draw
    # while the element is heating, and is written back so the slider learns.
    #
    # The heating gate is essential: standby electronics or a circulation pump
    # keep the sensor at a few watts with the element off, and learning from
    # that would shrink the tank's equivalent_current to the 0.1 A floor —
    # a 2 kW load booked as free. With no hvac_action to confirm heating we
    # keep the configured rating rather than learn from an unknown state.
    element_power = get_entry_value(
        entry, CONF_HEATING_ELEMENT_POWER, DEFAULT_HEATING_ELEMENT_POWER
    )
    slider_power = load_rt.get("device_power")
    power_rating = slider_power if slider_power else element_power
    power_entity = get_entry_value(entry, CONF_TANK_POWER_ENTITY_ID, None)
    live = None
    if power_entity:
        live = _coerce(_read_entity(hass, power_entity, 0, unit="W"))
        if live and live > 10 and hvac_action == "heating":
            power_rating = live
            load_rt["device_power"] = round(live, 0)

    connected_to_phase = get_entry_value(entry, CONF_CONNECTED_TO_PHASE, "A") or "A"
    phases = len(connected_to_phase)

    equivalent_current = power_rating / (voltage * phases) if voltage > 0 else 0

    # Actual draw — the element only consumes while the thermostat is calling
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
    # *which* setpoint (away/normal/boost) to aim at — the mode behavior only
    # decides how the tank competes for power, not whether it runs.
    mode = resolve_operating_mode(
        DEVICE_TYPE_HOT_WATER_TANK,
        load_rt.get("operating_mode", DEFAULT_OPERATING_MODE_HOT_WATER_TANK.key),
    )

    # Cold-tank promotion: a Solar Priority tank below its normal temperature is
    # bumped to the Normal urgency tier so it beats other solar-priority loads
    # in contention. Only the tier is raised — the behavior stays Solar Priority,
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

    # Surplus demotion: a tank aiming at boost is heating on energy the site
    # would otherwise dump, so it competes at the Excess tier instead of its
    # mode's own. The label is whatever the command layer last wrote — one cycle
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

    load = LoadContext(
        load_id=entry.entry_id,
        entity_id=load_entity_id,
        min_current=equivalent_current,
        max_current=equivalent_current,
        phases=phases,
        priority=priority,
        active_phases_mask=connected_to_phase,
        connector_status=connector_status,
        device_type=DEVICE_TYPE_HOT_WATER_TANK,
        operating_mode=mode.key,
        mode_behavior=behavior_for(mode),
        mode_priority=mode_priority,
        rated_current=equivalent_current,
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


def _add_loads_to_site(hass, site, hub_entry_id, load_entries=None):
    """Build LoadContext objects for all loads and add them to the site.

    ``load_entries`` overrides the hub's registered loads (used by tests and
    by any caller that already knows the entries); None reads the registry.
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
                hass, entry, site.voltage, load_entity_id, priority
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
                "%s %s: phase mask %s includes phases not on site (%s) — clamping to %s",
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
                "Circuit group '%s': removed %d stale member(s) — entries no longer exist",
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
