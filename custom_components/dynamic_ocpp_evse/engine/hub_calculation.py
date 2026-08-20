"""
Load Juggler - Main calculation module.

This file provides a unified interface for EVSE calculations.
All core calculation logic has been refactored into the calculations/ directory.
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

from ..calculations import (
    SiteContext,
    LoadContext,  # noqa: F401 - re-exported via __all__
    PhaseValues,
    calculate_all_charger_targets,
    excess_margin,
)
from ..const import (
    CONF_AUTO_DETECT_PHASE_MAPPING,
    CONF_BATTERY_SOC_HYSTERESIS,
    CONF_ENABLE_MAX_IMPORT_POWER,
    CONF_EXCESS_HYSTERESIS,
    CONF_EXCESS_TRIGGER_MARGIN,
    CONF_GRID_EXPORT_LIMIT,
    CONF_INVERTER_OUTPUT_PHASE_A_ENTITY_ID,
    CONF_INVERTER_OUTPUT_PHASE_B_ENTITY_ID,
    CONF_INVERTER_OUTPUT_PHASE_C_ENTITY_ID,
    CONF_INVERT_PHASES,
    CONF_MAIN_BREAKER_RATING,
    CONF_MAX_IMPORT_POWER_ENTITY_ID,
    CONF_NAME,
    CONF_PHASE_VOLTAGE,
    CONF_SITE_UPDATE_FREQUENCY,
    DEFAULT_BATTERY_SOC_HYSTERESIS,
    DEFAULT_BATTERY_SOC_MIN,
    DEFAULT_BATTERY_SOC_TARGET,
    DEFAULT_DISTRIBUTION_MODE,
    DEFAULT_EXCESS_HYSTERESIS,
    DEFAULT_EXCESS_TRIGGER_MARGIN,
    DEFAULT_GRID_EXPORT_LIMIT,
    DEFAULT_MAIN_BREAKER_RATING,
    DEFAULT_PHASE_VOLTAGE,
    DEFAULT_SITE_UPDATE_FREQUENCY,
    DEVICE_TYPE_EVSE,
    DOMAIN,
    GRID_STALE_TIMEOUT,
    HOUSEHOLD_HOLD_BRIDGE_SECONDS,
    HOUSEHOLD_HOLD_RESIDUAL,
    WIRING_TOPOLOGY_PARALLEL,
    WIRING_TOPOLOGY_SERIES,
)
from ..calculations.utils import (
    compute_household_per_phase,
    grid_without_managed_draws,
    hold_per_phase_floor,
)
from ..helpers import get_entry_value
from .auto_detect import check_inversion, check_phase_mapping
from . import fleet
from .hub_result import _build_hub_result, _compute_forecast_advice
from .load_builders import _add_chargers_to_site, _build_circuit_groups
from .readers import (
    _PHASE_LABELS,
    _check_entity_availability,
    _coerce,
    _fv,
    _fv2,
    _read_entity,
    _read_fleet_members,
    _read_grid_phases,
    _resolve_grid_phases,
    _smooth,
    _track_grid_stale,
)

_LOGGER = logging.getLogger(__name__)


def _apply_feedback_loop(site, solar_is_derived, members):
    """Subtract charger draws from grid readings to prevent double-counting.

    Grid CTs measure total site current INCLUDING charger draws. Without this
    adjustment, the engine double-counts charger power as both 'consumption'
    and 'charger demand'. Modifies site.consumption and site.export_current
    in-place.
    """
    # Off-grid: the grid phase readings are synthetic zeros (no CTs exist) and
    # never contained the charger draws — subtracting them here would fabricate
    # export equal to each charger's own draw. Solar was already derived from
    # the inverter output upstream, so nothing needs re-deriving either.
    if site.is_off_grid:
        return

    # Sum charger draws per site phase
    total_draws = [0.0, 0.0, 0.0]
    for c in site.chargers:
        a_draw, b_draw, c_draw = c.get_site_phase_draw()
        total_draws[0] += a_draw
        total_draws[1] += b_draw
        total_draws[2] += c_draw

    if not any(d > 0 for d in total_draws):
        return

    # Reconstruct raw grid current, remove charger draw, re-split
    orig_consumption = (site.consumption.a, site.consumption.b, site.consumption.c)
    orig_export = (site.export_current.a, site.export_current.b, site.export_current.c)
    new_consumption, new_export = grid_without_managed_draws(
        site.consumption, site.export_current, total_draws
    )
    adj_consumption = (new_consumption.a, new_consumption.b, new_consumption.c)
    adj_export = (new_export.a, new_export.b, new_export.c)

    for i, label in enumerate(_PHASE_LABELS):
        cons = orig_consumption[i]
        draw = total_draws[i]
        if cons is None:
            continue
        raw_grid = cons - (orig_export[i] or 0)
        # Warn when household consumption gets clamped to 0 by feedback
        if draw > 0 and adj_consumption[i] == 0 and cons > 0:
            _LOGGER.warning(
                "Phase %s: household -> 0 after feedback "
                "(raw_grid=%.1fA - charger=%.1fA = %.1fA)",
                label,
                raw_grid,
                draw,
                raw_grid - draw,
            )

    site.consumption = new_consumption
    site.export_current = new_export

    # Update derived solar after feedback. Same per-member derivation as the
    # first pass — only the export term changes, so a fleet where every member
    # measures its own production has nothing to redo (solar_is_derived False).
    solar_note = ""
    if solar_is_derived:
        export_after = site.export_current.total * site.voltage
        total = fleet.solar_total(members, site.voltage)
        if total is None:
            total = max(
                0.0, export_after + fleet.charging_power_total(members)
            )
        site.solar_production_total = total
        solar_note = f" | Solar(derived)={site.solar_production_total:.0f}W"

    _LOGGER.debug(
        "--- Feedback --- Subtracted A=%.1f B=%.1f C=%.1fA -> "
        "cons=(%s/%s/%s) exp=(%s/%s/%s)%s",
        total_draws[0],
        total_draws[1],
        total_draws[2],
        *[_fv(v) for v in adj_consumption],
        *[_fv(v) for v in adj_export],
        solar_note,
    )


def _mixed_household_per_phase(site, members):
    """Per-phase household for a mixed-topology fleet: the parallel formula on
    the parallel members' summed outputs plus the series formula on the series
    members' — grid-bus loads show on the CT + parallel outputs, behind-series
    loads show in the series outputs. Best-effort superposition; uniform
    fleets never come here and keep the exact single-formula path."""
    original = site.inverter_output_per_phase
    try:
        site.inverter_output_per_phase = fleet.sum_outputs(
            members, WIRING_TOPOLOGY_PARALLEL
        )
        parallel_hh = (
            compute_household_per_phase(site, WIRING_TOPOLOGY_PARALLEL)
            if site.inverter_output_per_phase is not None
            else None
        )
        site.inverter_output_per_phase = fleet.sum_outputs(
            members, WIRING_TOPOLOGY_SERIES
        )
        series_hh = (
            compute_household_per_phase(site, WIRING_TOPOLOGY_SERIES)
            if site.inverter_output_per_phase is not None
            else None
        )
    finally:
        site.inverter_output_per_phase = original

    if parallel_hh is None and series_hh is None:
        return None
    values = []
    for phase in ("a", "b", "c"):
        parts = [
            getattr(hh, phase)
            for hh in (parallel_hh, series_hh)
            if hh is not None and getattr(hh, phase) is not None
        ]
        values.append(max(0.0, sum(parts)) if parts else None)
    return PhaseValues(*values)


def _household_hold_decay(hub_entry):
    """Per-cycle retention factor for the household floor hold.

    Derived from wall clock, not a magic per-cycle number: after
    HOUSEHOLD_HOLD_BRIDGE_SECONDS of a zero reading the held value has decayed
    to HOUSEHOLD_HOLD_RESIDUAL, whatever the configured cycle length.
    """
    try:
        cycle_seconds = float(
            get_entry_value(
                hub_entry,
                CONF_SITE_UPDATE_FREQUENCY,
                DEFAULT_SITE_UPDATE_FREQUENCY,
            )
        )
    except (TypeError, ValueError):
        cycle_seconds = float(DEFAULT_SITE_UPDATE_FREQUENCY)
    if not math.isfinite(cycle_seconds) or cycle_seconds <= 0:
        cycle_seconds = float(DEFAULT_SITE_UPDATE_FREQUENCY)
    return HOUSEHOLD_HOLD_RESIDUAL ** (
        cycle_seconds / HOUSEHOLD_HOLD_BRIDGE_SECONDS
    )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def run_hub_calculation(hass, hub_entry, charger_entries=None):
    """
    Run the hub calculation: read HA states, build SiteContext, calculate targets.

    This is the ONE site calculation for a hub, run once per site cycle by the
    hub's DataUpdateCoordinator (see sensor.py). It takes no entity — every
    cycle-counted mechanism inside (settle counters, input EMAs, power-stable
    counts) advances exactly once per call, so a site with N loads no longer
    advances them N times per interval.

    Args:
        hass: Home Assistant instance
        hub_entry: the hub's ConfigEntry
        charger_entries: optional explicit list of load config entries; None
            reads the hub's registered loads

    Returns:
        dict with calculated values including:
            - CONF_TOTAL_ALLOCATED_CURRENT: Total allocated current (A)
            - CONF_PHASES: Number of phases
            - CONF_CHARGING_MODE: Current charging mode
            - charger_targets: per-charger target currents
            - Other site/charger data
    """
    # --- Read hub config values ---
    voltage = (
        get_entry_value(hub_entry, CONF_PHASE_VOLTAGE, DEFAULT_PHASE_VOLTAGE)
        or DEFAULT_PHASE_VOLTAGE
    )
    if voltage <= 0:
        voltage = DEFAULT_PHASE_VOLTAGE
    main_breaker_rating = get_entry_value(
        hub_entry, CONF_MAIN_BREAKER_RATING, DEFAULT_MAIN_BREAKER_RATING
    )
    # Excess trigger, derived from the physical export limit: engage once
    # export is within the trigger margin of the limit (an inverter curtails
    # slightly under the limit, so a trigger exactly AT it would never fire).
    # No limit configured (0) means the grid can absorb everything — the
    # allowance is infinite, grid-side Excess never triggers, and only the
    # battery side of excess_margin() remains.
    grid_export_limit = (
        get_entry_value(hub_entry, CONF_GRID_EXPORT_LIMIT, DEFAULT_GRID_EXPORT_LIMIT)
        or 0
    )
    excess_trigger_margin = get_entry_value(
        hub_entry, CONF_EXCESS_TRIGGER_MARGIN, DEFAULT_EXCESS_TRIGGER_MARGIN
    )
    # Release band once Excess is engaged (the latch below applies it).
    excess_hysteresis = (
        get_entry_value(hub_entry, CONF_EXCESS_HYSTERESIS, DEFAULT_EXCESS_HYSTERESIS)
        or 0
    )
    if grid_export_limit > 0:
        excess_threshold = max(0.0, grid_export_limit - excess_trigger_margin)
    else:
        excess_threshold = float("inf")

    # --- Read per-phase grid current (raw, signed; W/kW converted to A) ---
    # Entries are floats, None (no CT on that phase) or the _UNAVAILABLE
    # sentinel (CT configured but unreadable) — _resolve_grid_phases below is
    # the only thing allowed to substitute a number for the sentinel. Until
    # then, every test here has to be on None, never on truthiness or 0.
    raw_phases = _read_grid_phases(hass, hub_entry, voltage)
    has_grid_cts = any(r is not None for r in raw_phases)

    # A site phase exists if it has EITHER a grid CT or an inverter output
    # sensor configured — the phase count is the combination of both. For a
    # phase with an inverter sensor but no grid CT (an off-grid site, or a
    # partially grid-metered one), grid current is taken as 0 A so the phase
    # still counts. Without this a 1-phase off-grid site would look 3-phase
    # and per-phase figures would be split across phantom phases. A phase whose
    # CT is merely unreadable is NOT 0 A — the sentinel is not None, so it falls
    # through to the holdover instead.
    inv_phase_confs = (
        CONF_INVERTER_OUTPUT_PHASE_A_ENTITY_ID,
        CONF_INVERTER_OUTPUT_PHASE_B_ENTITY_ID,
        CONF_INVERTER_OUTPUT_PHASE_C_ENTITY_ID,
    )
    for i, conf in enumerate(inv_phase_confs):
        if raw_phases[i] is None and get_entry_value(hub_entry, conf, None):
            raw_phases[i] = 0.0
    # Nothing configured at all — fall back to a single phase.
    if all(r is None for r in raw_phases):
        raw_phases = [0.0, None, None]

    # --- Input EMA smoothing (grid CT, solar, battery power) ---
    hub_runtime = hass.data[DOMAIN]["hubs"].get(hub_entry.entry_id, {})
    ema_inputs = hub_runtime.setdefault("_ema_inputs", {})

    # --- Resolve unreadable grid CTs (the only place allowed to substitute) ---
    raw_phases, any_grid_stale = _resolve_grid_phases(
        raw_phases, ema_inputs, main_breaker_rating
    )
    grid_stale_duration = _track_grid_stale(
        hub_runtime, any_grid_stale, time.monotonic()
    )

    smoothed_phases = [
        _smooth(ema_inputs, f"grid_{i}", r) for i, r in enumerate(raw_phases)
    ]
    consumption = [max(0, r) if r is not None else None for r in smoothed_phases]
    export = [max(0, -r) if r is not None else None for r in smoothed_phases]
    consumption_pv = PhaseValues(*consumption)
    export_pv = PhaseValues(*export)

    total_export_current = export_pv.total
    total_export_power = total_export_current * voltage if voltage > 0 else 0

    # --- Inverter fleet (inverter entries + legacy hub-level fields) ---
    # Every battery/inverter scalar below is a fleet aggregate. With a single
    # member (the classic setup) each aggregate reduces to exactly the old
    # singleton value — see engine/fleet.py for the per-member gating rules.
    members = _read_fleet_members(hass, hub_entry, hub_runtime, ema_inputs, voltage)

    # --- Solar production (unified for grid and off-grid) ---
    # Per member: its own production sensor when configured, else derived from
    # its inverter output (parallel output IS production, series output minus
    # its own battery power). Falls back to grid export + the fleet's charging
    # draw when no member knows either. Off-grid, export is naturally 0.
    # Solar counts as derived unless EVERY member measures its own production;
    # a partly-measured fleet still needs the post-feedback re-derivation for
    # the members that only have inverter outputs (or none at all).
    solar_is_derived = not fleet.solar_is_measured(members)
    solar_production_total = fleet.solar_total(members, voltage)
    if solar_production_total is None:
        solar_production_total = max(
            0.0,
            (total_export_power or 0) + fleet.charging_power_total(members),
        )

    # --- Battery data (fleet) ---
    battery_soc = fleet.weighted_soc(members)
    battery_power = fleet.battery_power_total(members)
    battery_soc_hysteresis = get_entry_value(
        hub_entry, CONF_BATTERY_SOC_HYSTERESIS, DEFAULT_BATTERY_SOC_HYSTERESIS
    )
    # Charge capacity sums only members whose own battery is below its own
    # full-SOC; discharge is summed after the SOC-min hysteresis latch below.
    battery_max_charge_power = fleet.charge_power_total(members)

    # --- Max grid import power (entity override → shared hub data → None) ---
    enable_max_import = get_entry_value(hub_entry, CONF_ENABLE_MAX_IMPORT_POWER, True)
    max_import_power_entity = get_entry_value(
        hub_entry, CONF_MAX_IMPORT_POWER_ENTITY_ID, None
    )
    if max_import_power_entity:
        max_grid_import_power = _coerce(
            _read_entity(hass, max_import_power_entity, None, unit="W"), None
        )  # Convert kW→W if needed
    elif enable_max_import:
        hub_rt = hass.data[DOMAIN]["hubs"].get(hub_entry.entry_id, {})
        max_grid_import_power = hub_rt.get("max_import_power", None)
    else:
        max_grid_import_power = None

    # --- Inverter configuration (fleet) ---
    # Member outputs are already stale-guarded + smoothed at read time (per-
    # member EMA keys). The topology scalar is 'series' if ANY member is
    # series: the series solar formula on the summed outputs with the summed
    # battery power is exact for any mix, because parallel members contribute
    # no battery term — so the post-feedback re-derivation stays correct.
    (
        inverter_max_power,
        inverter_max_power_per_phase,
        inverter_supports_asymmetric,
    ) = fleet.inverter_limits(members)
    wiring_topology = fleet.fleet_topology(members)
    inverter_output_per_phase = fleet.sum_outputs(members)

    # Read-time power figures for the calculator's inverter coverage gate
    # (target_calculator._inverter_covers_load): what the fleet is putting out
    # right now, and which way the grid is flowing. Same measured-then-estimated
    # aggregation the display headroom uses, but taken HERE, before the feedback
    # loop: post-feedback the derived solar has the managed draws folded back
    # into it, which inflates the estimate by exactly the draw the gate then has
    # to discount — the two errors would cancel the load-off add-back and the
    # gate would suppress itself (issue #41).
    inverter_output_total = fleet.output_power_total(
        members,
        voltage,
        solar_w=solar_production_total,
        battery_power_w=battery_power,
    )
    # Signed net grid flow (+ import / − export) from the smoothed phases. Off
    # grid every phase reads 0, so this is 0 — correct: nothing to import.
    net_grid_power = sum(r for r in smoothed_phases if r is not None) * voltage

    # --- Runtime state from shared hub data (hub_runtime already fetched above) ---
    distribution_mode = hub_runtime.get("distribution_mode", DEFAULT_DISTRIBUTION_MODE)
    allow_grid_charging = hub_runtime.get("allow_grid_charging", True)
    power_buffer = hub_runtime.get("power_buffer", 0)
    battery_soc_target = hub_runtime.get(
        "battery_soc_target", DEFAULT_BATTERY_SOC_TARGET
    )
    battery_soc_min = hub_runtime.get("battery_soc_min", DEFAULT_BATTERY_SOC_MIN)
    # With exactly one battery this is its real full-SOC (classic behavior,
    # including the calculations-level gates that read it); a multi-battery
    # fleet passes None — its full gating already happened per member in
    # fleet.charge_power_total(), and a fleet-SOC gate would be wrong.
    battery_soc_full = fleet.soc_full_scalar(members)

    # Apply SOC hysteresis — adjust thresholds so engine stays stateless
    now_above_target = False
    now_above_min = False
    if (
        battery_soc is not None
        and battery_soc_hysteresis
        and battery_soc_hysteresis > 0
    ):
        was_above_target = hub_runtime.get("_soc_above_target", False)
        if was_above_target:
            now_above_target = (
                battery_soc >= battery_soc_target - battery_soc_hysteresis
            )
        else:
            now_above_target = battery_soc >= battery_soc_target
        hub_runtime["_soc_above_target"] = now_above_target
        if now_above_target:
            battery_soc_target = battery_soc_target - battery_soc_hysteresis

        # The min floor's band sits ABOVE the setting (mirror of the target's):
        # the floor is protective, so discharge stops AT the configured floor
        # and only resumes once the battery has recovered a full hysteresis
        # above it (floor 20, hysteresis 3 → stop at 20, resume at 23). The
        # target's band sits below its setting for the same reason in reverse —
        # charging never overshoots the configured ceiling.
        was_above_min = hub_runtime.get("_soc_above_min", False)
        if was_above_min:
            now_above_min = battery_soc >= battery_soc_min
        else:
            now_above_min = battery_soc >= battery_soc_min + battery_soc_hysteresis
        hub_runtime["_soc_above_min"] = now_above_min
        if not now_above_min:
            battery_soc_min = battery_soc_min + battery_soc_hysteresis

    # Discharge capacity sums only members whose OWN battery is at/above the
    # (hysteresis-adjusted) hub minimum — a battery below the floor cannot be
    # counted dischargeable because a full sibling lifts the fleet SOC.
    battery_max_discharge_power = fleet.discharge_power_total(members, battery_soc_min)

    # Apply power buffer to reduce effective max grid import power
    if max_grid_import_power is not None and power_buffer > 0:
        max_grid_import_power = max(0, max_grid_import_power - power_buffer)

    # (Excess-export hysteresis is applied after the feedback loop below — it
    # must see the same post-feedback export figure the engine's excess pool
    # compares against the threshold.)

    # --- Debug logging ---
    invert_phases = get_entry_value(hub_entry, CONF_INVERT_PHASES, False)
    _LOGGER.debug(
        "--- Hub Update --- CT: A=%sA B=%sA C=%sA (%dph, invert=%s) | "
        "Solar: %sW (%s) | Export: %sA/%sW",
        _fv2(raw_phases[0], smoothed_phases[0]),
        _fv2(raw_phases[1], smoothed_phases[1]),
        _fv2(raw_phases[2], smoothed_phases[2]),
        consumption_pv.active_count,
        "on" if invert_phases else "off",
        _fv(solar_production_total),
        "measured" if not solar_is_derived else "derived",
        _fv(total_export_current),
        _fv(total_export_power),
    )
    _extra = []
    if any(m.has_battery for m in members):
        _bat_dir = (
            "chg"
            if (battery_power or 0) < 0
            else ("dischg" if (battery_power or 0) > 0 else "idle")
        )
        _hyst_min = "*" if now_above_min else ""
        _hyst_tgt = "*" if now_above_target else ""
        _n_batteries = sum(1 for m in members if m.has_battery)
        _extra.append(
            f"Bat(x{_n_batteries}): {_fv(battery_soc)}%/{_fv(battery_power)}W({_bat_dir}) "
            f"min={_fv(battery_soc_min)}%{_hyst_min} tgt={_fv(battery_soc_target)}%{_hyst_tgt}"
        )
    if inverter_max_power or inverter_max_power_per_phase:
        _extra.append(
            f"Inv: {_fv(inverter_max_power)}W/{_fv(inverter_max_power_per_phase)}W/ph "
            f"{'asym' if inverter_supports_asymmetric else 'sym'} {wiring_topology}"
        )
    _LOGGER.debug(
        "  dist=%s grid_chg=%s buf=%sW max_import=%s%s",
        distribution_mode,
        "on" if allow_grid_charging else "off",
        _fv(power_buffer),
        f"{max_grid_import_power:.0f}W"
        if max_grid_import_power is not None
        else "unlimited",
        (" | " + " | ".join(_extra)) if _extra else "",
    )
    if inverter_output_per_phase:
        _LOGGER.debug(
            "  Inverter output: A=%sA B=%sA C=%sA",
            _fv(inverter_output_per_phase.a),
            _fv(inverter_output_per_phase.b),
            _fv(inverter_output_per_phase.c),
        )

    # --- Build SiteContext ---
    site = SiteContext(
        voltage=voltage,
        main_breaker_rating=main_breaker_rating,
        grid_current=PhaseValues(*raw_phases),
        consumption=consumption_pv,
        export_current=export_pv,
        solar_production_total=solar_production_total,
        solar_is_derived=solar_is_derived,
        battery_soc=float(battery_soc) if battery_soc is not None else None,
        battery_power=float(battery_power) if battery_power is not None else None,
        battery_soc_min=float(battery_soc_min) if battery_soc_min is not None else None,
        battery_soc_target=float(battery_soc_target)
        if battery_soc_target is not None
        else None,
        battery_soc_full=float(battery_soc_full)
        if battery_soc_full is not None
        else None,
        battery_soc_hysteresis=float(battery_soc_hysteresis)
        if battery_soc_hysteresis is not None
        else 5,
        battery_max_charge_power=float(battery_max_charge_power)
        if battery_max_charge_power is not None
        else None,
        battery_max_discharge_power=float(battery_max_discharge_power)
        if battery_max_discharge_power is not None
        else None,
        max_grid_import_power=float(max_grid_import_power)
        if max_grid_import_power is not None
        else None,
        inverter_max_power=float(inverter_max_power)
        if inverter_max_power is not None
        else None,
        inverter_max_power_per_phase=float(inverter_max_power_per_phase)
        if inverter_max_power_per_phase is not None
        else None,
        inverter_supports_asymmetric=inverter_supports_asymmetric,
        wiring_topology=wiring_topology,
        inverter_output_per_phase=inverter_output_per_phase,
        inverter_output_total=float(inverter_output_total)
        if inverter_output_total is not None
        else None,
        net_grid_power=float(net_grid_power),
        excess_export_threshold=excess_threshold,
        allow_grid_charging=allow_grid_charging,
        power_buffer=power_buffer,
        distribution_mode=distribution_mode,
        is_off_grid=not has_grid_cts,
    )

    # --- Add chargers ---
    hub_entry_id = (
        hub_entry.entry_id
        if hasattr(hub_entry, "entry_id")
        else hub_entry.data.get("hub_entry_id")
    )
    _add_chargers_to_site(hass, site, hub_entry_id, charger_entries)

    # --- Build circuit groups ---
    site.circuit_groups = _build_circuit_groups(hass, hub_entry_id)

    # Apply auto-detected phase remaps from previous cycles
    auto_detect_state = hub_runtime.setdefault("_auto_detect", {})
    phase_remaps = auto_detect_state.get("phase_remap", {})
    for charger in site.chargers:
        remap = phase_remaps.get(charger.charger_id)
        if remap:
            old = (charger.l1_phase, charger.l2_phase, charger.l3_phase)
            charger.l1_phase = remap["l1_phase"]
            charger.l2_phase = remap["l2_phase"]
            charger.l3_phase = remap["l3_phase"]
            # Recalculate active_phases_mask from new mapping
            if charger.phases == 3:
                charger.active_phases_mask = "".join(
                    sorted({charger.l1_phase, charger.l2_phase, charger.l3_phase})
                )
            elif charger.phases == 2:
                charger.active_phases_mask = "".join(
                    sorted({charger.l1_phase, charger.l2_phase})
                )
            elif charger.phases == 1:
                charger.active_phases_mask = charger.l1_phase
            _LOGGER.debug(
                "Auto-remap applied for %s: L1:%s→%s L2:%s→%s L3:%s→%s mask=%s",
                charger.entity_id,
                old[0],
                charger.l1_phase,
                old[1],
                charger.l2_phase,
                old[2],
                charger.l3_phase,
                charger.active_phases_mask,
            )

    # --- Feedback loop ---
    _apply_feedback_loop(site, solar_is_derived, members)

    # Excess trigger + hysteresis latch. excess_margin() returns the watts by
    # which everything the site is absorbing (grid export + battery charging)
    # exceeds everything it is allowed to absorb (export allowance + battery
    # charge allowance); >= 0 means on. Once engaged, the band widens by the
    # hub's Excess hysteresis so a load doesn't chatter at the trigger point.
    # The latch lives here so the calculator stays stateless — it just reads
    # site.excess_hysteresis. The per-sink breakdown goes to excess_margin()'s
    # own debug line.
    #
    # Evaluated on POST-feedback figures: the pools compare export with charger
    # draws added back, and pre-feedback export is already eaten by the Excess
    # load's own draw — the band would never engage exactly when a load is running.
    was_excess_on = hub_runtime.get("_excess_on", False)
    margin = excess_margin(site, excess_hysteresis if was_excess_on else 0)
    excess_on = margin >= 0
    hub_runtime["_excess_on"] = excess_on
    site.excess_hysteresis = excess_hysteresis if excess_on else 0
    _LOGGER.debug(
        "Excess %s (margin %+.0fW)", "ON" if excess_on else "off", margin
    )

    # Compute household_consumption_total when solar entity provides ground truth
    if not solar_is_derived and solar_production_total > 0:
        export_power_after_feedback = site.export_current.total * site.voltage
        bp = float(battery_power) if battery_power is not None else 0
        site.household_consumption_total = max(
            0, solar_production_total + bp - export_power_after_feedback
        )
        _LOGGER.debug(
            "Computed household_consumption_total=%.1fW (solar=%.1fW + bat=%.1fW - export=%.1fW)",
            site.household_consumption_total,
            solar_production_total,
            bp,
            export_power_after_feedback,
        )

    # Compute per-phase household from inverter output entities (after feedback)
    if fleet.mixed_topologies(members):
        household = _mixed_household_per_phase(site, members)
    else:
        household = compute_household_per_phase(site, site.wiring_topology)
    if household is not None:
        # Asymmetric hold on the household floor. The managed draw side of the
        # subtraction (OCPP, sub-second) reacts before the polled inverter
        # output does, so a ramping car transiently zeroes household and the
        # engine would hand the real household's power out as headroom. Rises
        # pass straight through; falls are bridged over
        # HOUSEHOLD_HOLD_BRIDGE_SECONDS of wall clock.
        decay = _household_hold_decay(hub_entry)
        raw_household = household
        household = hold_per_phase_floor(
            household, hub_runtime.get("_household_held"), decay
        )
        hub_runtime["_household_held"] = household
        site.household_consumption = household
        _LOGGER.debug(
            "Per-phase household from inverter output (%s): A=%.1fA B=%.1fA "
            "C=%.1fA (raw A=%.1fA B=%.1fA C=%.1fA, hold decay %.3f)",
            site.wiring_topology,
            household.a if household.a is not None else 0,
            household.b if household.b is not None else 0,
            household.c if household.c is not None else 0,
            raw_household.a if raw_household.a is not None else 0,
            raw_household.b if raw_household.b is not None else 0,
            raw_household.c if raw_household.c is not None else 0,
            decay,
        )
    else:
        # No inverter output data at all — nothing to hold, and the held value
        # must be dropped so it cannot resurrect on a later cycle.
        hub_runtime.pop("_household_held", None)

    # --- Calculate targets ---
    calculate_all_charger_targets(site)

    # --- Grid stale fallback: force min_current after timeout ---
    grid_stale = grid_stale_duration > GRID_STALE_TIMEOUT
    if grid_stale:
        _LOGGER.warning(
            "Grid CT unavailable for %.0fs (>%ds) — charging EVSEs falling to "
            "minimum current, binary loads switched off",
            grid_stale_duration,
            GRID_STALE_TIMEOUT,
        )
        for charger in site.chargers:
            # Only an EVSE already charging keeps a minimum-current permit (a
            # hard stop mid-charge is worse than 6 A on a blind site). Binary
            # loads (plugs/tanks) and idle EVSEs get no permit — a permit > 0
            # switches a binary load ON, and energizing a load the engine had
            # deliberately shed while it cannot see the site is unsafe.
            if (
                charger.device_type == DEVICE_TYPE_EVSE
                and charger.connector_status == "Charging"
            ):
                charger.allocated_current = charger.min_current
                charger.available_current = charger.min_current
            else:
                charger.allocated_current = 0
                charger.available_current = 0

    charger_targets = {c.charger_id: c.allocated_current for c in site.chargers}
    charger_available = {c.charger_id: c.available_current for c in site.chargers}
    charger_names = {c.charger_id: c.entity_id for c in site.chargers}

    # Persist this cycle's permit for next-cycle settle detection — an EVSE
    # only counts as "settled and under-drawing" when its measured draw stays
    # below the permit we last offered it.
    chargers_rt = hass.data[DOMAIN].get("chargers", {})
    for c in site.chargers:
        rt = chargers_rt.get(c.charger_id)
        if rt is not None:
            rt["_last_permit"] = c.available_current

    # --- Build per-group allocation data for group sensors ---
    group_data = {}
    charger_by_id = {c.charger_id: c for c in site.chargers}
    for group in site.circuit_groups:
        per_phase_draw = {"A": 0.0, "B": 0.0, "C": 0.0}
        for mid in group.member_ids:
            c = charger_by_id.get(mid)
            if c and c.allocated_current > 0 and c.active_phases_mask:
                for phase in c.active_phases_mask:
                    per_phase_draw[phase] += c.allocated_current
        # Headroom = limit minus max draw on any active phase
        active_draws = [
            per_phase_draw[p]
            for p in ("A", "B", "C")
            if site.consumption and getattr(site.consumption, p.lower()) is not None
        ]
        max_draw = max(active_draws) if active_draws else 0
        headroom = max(0, group.current_limit - max_draw)
        group_data[group.group_id] = {
            "name": group.name,
            "current_limit": group.current_limit,
            "member_ids": group.member_ids,
            "per_phase_draw": per_phase_draw,
            "max_phase_draw": round(max_draw, 1),
            "headroom": round(headroom, 1),
        }

    # --- Auto-detection (inversion + phase mapping) ---
    # auto_detect_state already initialized above (line 926)
    auto_notifications = []
    inv_notif = check_inversion(
        auto_detect_state,
        smoothed_phases,
        site.chargers,
        hub_entry.entry_id,
        get_entry_value(hub_entry, CONF_NAME, "Hub"),
    )
    if inv_notif:
        auto_notifications.append(inv_notif)
    if get_entry_value(hub_entry, CONF_AUTO_DETECT_PHASE_MAPPING, True):
        pm_results = check_phase_mapping(
            auto_detect_state,
            smoothed_phases,
            site.chargers,
            hub_entry.entry_id,
        )
        for notif in pm_results:
            # Store auto-remap for next cycle
            remap = notif.pop("auto_remap", None)
            if remap:
                auto_detect_state.setdefault("phase_remap", {})[remap["charger_id"]] = (
                    remap
                )
                # Reset correlation state so re-detection runs with new mapping
                # (allows 2-phase detection to verify/correct after 1-phase remap)
                pm_state = auto_detect_state.get("phase_map", {})
                pm_state.pop(remap["charger_id"], None)
            auto_notifications.append(notif)

    # --- Hub status (config validation + runtime state) ---
    # The hub Status sensor names exactly which sensor/input is missing or
    # unavailable so the user knows precisely what to fix.
    hub_status = "OK"
    hub_warnings = []

    has_inverter_output = inverter_output_per_phase is not None
    # Any fleet member with its own production sensor counts as a
    # power-measurement input for the setup-completeness check.
    has_solar_entity = any(m.has_solar_entity for m in members)

    if not has_grid_cts and not has_inverter_output and not has_solar_entity:
        hub_status = "Setup incomplete"
        hub_warnings.append(
            "No power-measurement input configured. Add at least one in the "
            "hub options: grid CT current sensors (grid-tied sites), inverter "
            "output power sensors, or a solar production sensor."
        )
    elif not has_grid_cts:
        # Off-grid: no grid CTs, so the battery is the primary state source.
        # Any fleet member's battery satisfies the requirement — the battery
        # may live on the hub's legacy fields or on an inverter entry.
        hub_warnings.append("Off-grid mode (no grid CTs)")
        if not any(m.battery_soc is not None or m.has_battery for m in members):
            hub_status = "Setup incomplete"
            hub_warnings.append(
                "Off-grid hub needs a battery SOC sensor — it drives the "
                "operating-mode logic. Set it on the hub or an inverter entry."
            )
        if not any(m.has_battery_power_entity for m in members):
            hub_status = "Setup incomplete"
            hub_warnings.append(
                "Off-grid hub needs a battery power sensor — it is used to "
                "detect available solar surplus. Set it on the hub or an "
                "inverter entry."
            )
        if not has_inverter_output and not has_solar_entity:
            hub_warnings.append(
                "Off-grid hub has no inverter output or solar production "
                "sensor — available solar can only be inferred from battery "
                "charging. Add one for an accurate measurement."
            )

    if grid_stale:
        hub_status = "Grid sensors unavailable"
        hub_warnings.append(
            f"Grid CT sensors unavailable (stale for {grid_stale_duration:.0f}s)."
        )

    # Configured non-grid sensors that are currently unavailable. Name them in
    # the status line itself (not just the warnings attribute) so the user sees
    # *which* sensor dropped out at a glance, without expanding attributes.
    unavailable = _check_entity_availability(hass, hub_entry)
    if unavailable:
        hub_warnings.extend(
            f"{label} ({entity_id}) is unavailable" for label, entity_id in unavailable
        )
        if hub_status == "OK":
            labels = [label for label, _ in unavailable]
            named = ", ".join(labels[:2])
            if len(labels) > 2:
                named += f" +{len(labels) - 2} more"
            hub_status = f"Sensor unavailable: {named}"

    # --- Per-inverter data for the inverter-entry sensors ---
    # The legacy implicit member (the hub's own fields) has no device of its
    # own — its values already show on the hub's fleet sensors.
    inverters_data = {
        m.entry_id: {
            "name": m.name,
            "solar_w": fleet.member_solar_production(m, voltage),
            "battery_soc": m.battery_soc,
            "battery_power": m.battery_power,
        }
        for m in members
        if m.entry_id != hub_entry_id
    }

    # --- PV clipping forecast (advisory battery headroom) ---
    # Computed post-feedback so solar_production_total excludes managed draws.
    forecast_advice, forecast_per_inverter = _compute_forecast_advice(
        hass,
        hub_entry,
        hub_runtime,
        site,
        battery_soc,
        members,
    )
    for inv_id, advice in forecast_per_inverter.items():
        if inv_id in inverters_data:
            inverters_data[inv_id].update(advice)

    # --- Build result ---
    return _build_hub_result(
        site,
        raw_phases,
        voltage,
        main_breaker_rating,
        battery_soc,
        battery_soc_min,
        battery_max_discharge_power,
        battery_power,
        charger_targets,
        charger_available,
        charger_names,
        auto_notifications,
        group_data,
        grid_stale=grid_stale,
        hub_status=hub_status,
        hub_warnings=hub_warnings,
        excess_available=excess_on,
        excess_margin_power=margin,
        forecast_advice=forecast_advice,
        inverters_data=inverters_data,
    )


__all__ = [
    "SiteContext",
    "LoadContext",
    "PhaseValues",
    "calculate_all_charger_targets",
    "run_hub_calculation",
]
