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
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from ..calculations import (
    SiteContext,
    LoadContext,  # noqa: F401 - re-exported via __all__
    PhaseValues,
    calculate_all_load_targets,
    excess_margin,
    grid_overdraw,
)
from ..calculations.models import INACTIVE_STATUSES
from ..const import (
    CONF_AUTO_DETECT_PHASE_MAPPING,
    CONF_BATTERY_SOC_HYSTERESIS,
    CONF_BINARY_MIN_OFF_TIME,
    CONF_CHARGE_PAUSE_DURATION,
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
    CONF_FILTER_CTRL_FAST_TAU_S,
    CONF_FILTER_INPUT_TAU_S,
    CONF_FILTER_SETTLE_SECONDS,
    CONF_SITE_UPDATE_FREQUENCY,
    CONF_UPDATE_FREQUENCY,
    DEAD_BAND,
    DEFAULT_BATTERY_SOC_HYSTERESIS,
    DEFAULT_BINARY_MIN_OFF_TIME,
    DEFAULT_CHARGE_PAUSE_DURATION,
    DEFAULT_BATTERY_SOC_MIN,
    DEFAULT_BATTERY_SOC_TARGET,
    DEFAULT_DISTRIBUTION_MODE,
    DEFAULT_EXCESS_HYSTERESIS,
    DEFAULT_EXCESS_TRIGGER_MARGIN,
    DEFAULT_GRID_EXPORT_LIMIT,
    DEFAULT_MAIN_BREAKER_RATING,
    DEFAULT_PHASE_VOLTAGE,
    CTRL_FAST_TAU_S,
    DEAD_BAND,
    DEFAULT_SITE_UPDATE_FREQUENCY,
    DEFAULT_UPDATE_FREQUENCY,
    EMA_TAU_S,
    SETTLE_DRAW_SECONDS,
    DEVICE_TYPE_EVSE,
    DEVICE_TYPE_HOT_WATER_TANK,
    DEVICE_TYPE_PLUG,
    DOMAIN,
    GRID_STALE_TIMEOUT,
    HOUSEHOLD_HOLD_BRIDGE_SECONDS,
    HOUSEHOLD_HOLD_RESIDUAL,
    INPUT_STALE_TIMEOUT,
    LOAD_RT_SUN_PROBE,
    SUN_PROBE_MAX_PAUSE_S,
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
from .load_builders import (
    _add_loads_to_site,
    _build_circuit_groups,
    _watch_readouts_against_household,
)
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
    set_ema_interval,
    _smooth_directional,
    _track_grid_stale,
)

_LOGGER = logging.getLogger(__name__)


def _managed_phase_draws(site, ema_inputs=None):
    """Σ managed-load draw per site phase (A), the feedback loop's subtrahend.

    SMOOTHED on the same EMA as the grid phases it is subtracted from, when
    ``ema_inputs`` is supplied. The two terms are subtracted from each other,
    so they have to be on one time basis: the reconstruction answers "what
    would the site export with our loads off", and that answer is only stable
    while both halves move together. With a smoothed grid reading and a raw
    draw, every change in a load's draw moved the reconstruction before the
    grid term caught up - the margin overshot, the permit chased it, and a
    modulating load rang around its target instead of settling (measured on
    the rig, 2026-09-08: a station hunting 299-828 W against a 600 W target,
    seven register writes a minute).

    With ``ema_inputs`` every call ADVANCES that EMA, so it is taken exactly
    once per site cycle (``run_hub_calculation``) and the one result is handed
    to every view that subtracts it. Two calls a cycle ran the draw's filter
    at twice the grid's speed: during a ramp the draw term led the grid term,
    the household read low by the difference and the permit overshot the
    allowance (dev/tests/test_managed_draw_smoothing.py). The same holds for
    the series household, the SMOOTHED inverter output minus this draw - the
    only household an off-grid site has - so it takes this one result too
    (_apply_household_figures; dev/tests/test_offgrid_household_smoothing.py).

    Callers that work on the RAW grid basis must leave ``ema_inputs`` unset and
    get raw draws, so their pairing stays consistent too -
    ``engine/hub_result.py`` adds draws back to ``raw_phases`` and wants raw
    on both sides.

    A load whose Dynamic Control is OFF is skipped. Subtracting its draw would
    add that draw back into the reconstruction - telling the engine the site
    could export it if only our loads stood down - when the whole point of the
    switch is that this load does not stand down for us. Left in, it is
    household consumption, which is what an unmanaged load is.
    """
    total_draws = [0.0, 0.0, 0.0]
    for c in site.loads:
        if not c.dynamic_control:
            continue
        a_draw, b_draw, c_draw = c.get_site_phase_draw()
        total_draws[0] += a_draw
        total_draws[1] += b_draw
        total_draws[2] += c_draw
    if ema_inputs is None:
        return total_draws
    # Same alpha, same shape as readers._smooth on grid_0..2.
    return [
        _smooth(ema_inputs, f"managed_draw_{i}", d) or 0.0
        for i, d in enumerate(total_draws)
    ]


def _charge_control_view(site, consumption, export, battery_power, draws):
    """The site as the battery CHARGE CONTROLLER reads it.

    Same loads, same allowance, same feedback subtraction as ``site`` - only
    the grid phases and the battery power come from the DIRECTIONAL smoothers
    (``readers._smooth_directional``): export moving toward the limit and
    charging falling both reach the controller in two cycles, while the moves
    back toward zero keep the ordinary EMA. Nothing else reads this view; the
    Excess verdict and the allocation stay on the symmetric site, which is what
    keeps a responsive register from flapping the verdict through the
    allowance term (measured: 21 flips when the shared readings were made
    fast, 1 with the view scoped here - dev/tests/test_charge_control_loop.py).

    Built AFTER ``_apply_feedback_loop`` ran on the site, so the same
    managed-draw subtraction is applied to these phases here - ``draws`` is the
    very list the site view subtracted, not a second smoothing of it; off-grid
    the phases are synthetic zeros on both views and there is nothing to
    subtract.
    """
    if not site.is_off_grid and any(d > 0 for d in draws):
        consumption, export = grid_without_managed_draws(consumption, export, draws)
    return replace(
        site, consumption=consumption, export_current=export, battery_power=battery_power
    )


def _supply_per_phase(raw_phases, grid_assumed, has_grid_cts, members,
                      output_total_w, voltage):
    """Per phase, what the site is drawing WITH our managed loads in it (A) -
    the quantity the household is reconstructed from by taking every managed
    draw off. Returns ``(phases, unusable, source)``, one entry per site
    phase: None where there is no usable reading, and ``unusable`` flagging a
    value that stands on an assumption rather than a reading; ``source`` names
    where it was measured, for the watch's log line.

    * Grid-tied: the signed grid reading, unsmoothed - the feedback loop's own
      basis (_apply_feedback_loop). ``unusable`` is the breaker worst case an
      unreadable CT with no history stands on.
    * Off-grid: the inverter fleet's AC output. With no grid, everything the
      site consumes - managed loads included - comes out of the inverters, so
      the fleet's summed output IS the site's consumption, per phase where the
      output sensors are per phase (unsmoothed, like the grid path). A site
      with no output sensors at all is modelled single-phase
      (_read_site_phases), and the fleet's total output - measured, else the
      topology-aware estimate - stands on that one phase.

    Read only by the stuck-readout watch (engine/load_builders.
    _watch_readouts_against_household); the allocation's own household is
    untouched by it.
    """
    if has_grid_cts:
        return list(raw_phases), tuple(grid_assumed), "the grid"
    exists = [r is not None for r in raw_phases]
    outputs = fleet.sum_outputs(members, raw=True)
    if outputs is not None:
        values = [
            getattr(outputs, p) if exists[i] else None
            for i, p in enumerate(("a", "b", "c"))
        ]
    elif sum(exists) == 1 and output_total_w is not None and voltage > 0:
        values = [output_total_w / voltage if e else None for e in exists]
    else:
        values = [None, None, None]
    return values, (False, False, False), "the inverter output"


def _apply_feedback_loop(site, solar_is_derived, members, total_draws):
    """Subtract load draws from grid readings to prevent double-counting.

    Grid CTs measure total site current INCLUDING load draws. Without this
    adjustment, the engine double-counts load power as both 'consumption'
    and 'load demand'. Modifies site.consumption and site.export_current
    in-place. ``total_draws`` is this cycle's smoothed per-phase managed draw
    from ``_managed_phase_draws``.
    """
    # Off-grid: the grid phase readings are synthetic zeros (no CTs exist) and
    # never contained the load draws - subtracting them here would fabricate
    # export equal to each load's own draw. Solar was already derived from
    # the inverter output upstream, so nothing needs re-deriving either.
    if site.is_off_grid:
        return

    if not any(d > 0 for d in total_draws):
        return

    # Reconstruct raw grid current, remove load draw, re-split
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
                "(raw_grid=%.1fA - load=%.1fA = %.1fA)",
                label,
                raw_grid,
                draw,
                raw_grid - draw,
            )

    site.consumption = new_consumption
    site.export_current = new_export

    # Update derived solar after feedback. Same per-member derivation as the
    # first pass - only the export term changes, so a fleet where every member
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


def _mixed_household_per_phase(site, members, draws):
    """Per-phase household for a mixed-topology fleet: the parallel formula on
    the parallel members' summed outputs plus the series formula on the series
    members' - grid-bus loads show on the CT + parallel outputs, behind-series
    loads show in the series outputs. Best-effort superposition; uniform
    fleets never come here and keep the exact single-formula path. ``draws``
    is what the series half subtracts (see _apply_household_figures), and only
    the series half: off-grid the parallel formula takes the draws off too, so
    the parallel half is handed none, or they would come off twice."""
    original = site.inverter_output_per_phase
    try:
        site.inverter_output_per_phase = fleet.sum_outputs(
            members, WIRING_TOPOLOGY_PARALLEL
        )
        parallel_hh = (
            compute_household_per_phase(
                site, WIRING_TOPOLOGY_PARALLEL, (0.0, 0.0, 0.0)
            )
            if site.inverter_output_per_phase is not None
            else None
        )
        site.inverter_output_per_phase = fleet.sum_outputs(
            members, WIRING_TOPOLOGY_SERIES
        )
        series_hh = (
            compute_household_per_phase(site, WIRING_TOPOLOGY_SERIES, draws)
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


def _read_hub_config(hub_entry):
    """The hub's own scalar settings, read once per cycle.

    Returns ``(voltage, main_breaker_rating, excess_hysteresis,
    excess_threshold)`` - the site electricals plus the Excess trigger
    point and release band derived from the configured export limit.
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
    # No limit configured (0) means the grid can absorb everything - the
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
    return voltage, main_breaker_rating, excess_hysteresis, excess_threshold


def _read_site_phases(hass, hub_entry, voltage):
    """The site's per-phase grid readings and whether it has grid CTs at all.

    Returns ``(raw_phases, has_grid_cts)``. Which phases EXIST is decided
    here, from the CTs and the inverter output sensors together, because
    everything downstream (the phase count, the per-phase split, off-grid
    handling) reads it off this one list.
    """
    # --- Read per-phase grid current (raw, signed; W/kW converted to A) ---
    # Entries are floats, None (no CT on that phase) or the _UNAVAILABLE
    # sentinel (CT configured but unreadable) - _resolve_grid_phases below is
    # the only thing allowed to substitute a number for the sentinel. Until
    # then, every test here has to be on None, never on truthiness or 0.
    raw_phases = _read_grid_phases(hass, hub_entry, voltage)
    has_grid_cts = any(r is not None for r in raw_phases)

    # A site phase exists if it has EITHER a grid CT or an inverter output
    # sensor configured - the phase count is the combination of both. For a
    # phase with an inverter sensor but no grid CT (an off-grid site, or a
    # partially grid-metered one), grid current is taken as 0 A so the phase
    # still counts. Without this a 1-phase off-grid site would look 3-phase
    # and per-phase figures would be split across phantom phases. A phase whose
    # CT is merely unreadable is NOT 0 A - the sentinel is not None, so it falls
    # through to the holdover instead.
    inv_phase_confs = (
        CONF_INVERTER_OUTPUT_PHASE_A_ENTITY_ID,
        CONF_INVERTER_OUTPUT_PHASE_B_ENTITY_ID,
        CONF_INVERTER_OUTPUT_PHASE_C_ENTITY_ID,
    )
    for i, conf in enumerate(inv_phase_confs):
        if raw_phases[i] is None and get_entry_value(hub_entry, conf, None):
            raw_phases[i] = 0.0
    # Nothing configured at all - fall back to a single phase.
    if all(r is None for r in raw_phases):
        raw_phases = [0.0, None, None]
    return raw_phases, has_grid_cts


def _read_max_import_power(hass, hub_entry):
    """The site's max grid import power, or None for unlimited.

    Precedence, as the hub form's own help text states it: a configured
    OVERRIDE SENSOR applies whenever it is set - "it takes precedence over both
    the slider and this checkbox" - then the hub's slider while the checkbox
    that creates it is ticked, else unlimited.

    The checkbox is ``CONF_ENABLE_MAX_IMPORT_POWER``, labelled "Create max
    import power limit slider". It is NOT a switch for the feature, and it
    must never gate the sensor: a user who drives the limit from their own
    sensor has no use for the slider and leaves the box unticked. Commit
    565a0bf (2026-09-07) read the flag as a feature switch and tested it
    first, on the theory that an unticked box beside a configured sensor was
    a leftover - it was the intended configuration, and the live SE17K's
    15-minute block limit (sensor.current_block_power_limit, 10,600 W in
    tariff block 2) went unapplied for a week until the export of 2026-09-14
    showed the published grid headroom sitting up to 9 kW above it.
    """
    # --- Max grid import power (override sensor -> slider -> unlimited) ---
    max_import_power_entity = get_entry_value(
        hub_entry, CONF_MAX_IMPORT_POWER_ENTITY_ID, None
    )
    if max_import_power_entity:
        return _coerce(
            _read_entity(hass, max_import_power_entity, None, unit="W"), None
        )  # Convert kW->W if needed
    if not get_entry_value(hub_entry, CONF_ENABLE_MAX_IMPORT_POWER, True):
        return None
    hub_rt = hass.data[DOMAIN]["hubs"].get(hub_entry.entry_id, {})
    return hub_rt.get("max_import_power", None)


def _apply_soc_hysteresis(
    hub_runtime,
    battery_soc,
    battery_soc_hysteresis,
    battery_soc_target,
    battery_soc_min,
):
    """Latch the battery SOC thresholds so the calculator stays stateless.

    The two latch bits live in ``hub_runtime``; the adjusted thresholds are
    returned rather than mutated in place, so the caller can see exactly
    which values the rest of the cycle runs on. Returns
    ``(battery_soc_target, battery_soc_min, now_above_target,
    now_above_min)`` - the last two feed the debug line's hysteresis marks.
    """
    # Apply SOC hysteresis - adjust thresholds so engine stays stateless
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
        # target's band sits below its setting for the same reason in reverse -
        # charging never overshoots the configured ceiling.
        #
        # STRICTLY above, because "stop AT the floor" is what the consumers
        # mean by it: the SOC-gated binary modes run while ``soc > soc_min``
        # (calculations.target_calculator._source_limit). Latching on ``>=``
        # here disagreed with them at exactly the floor - the latch read "still
        # above", so it never widened the threshold, while the gate read "not
        # above" and shed the load. The band was then unreachable: the load
        # switched off AT the floor, the battery recovered one percent, the load
        # switched back on and pulled it down again, cycling in a 1% window for
        # as long as production held. Nothing on the target side needs this:
        # there the latch DOES adjust its threshold while above, so the gate
        # cannot chatter against it.
        was_above_min = hub_runtime.get("_soc_above_min", False)
        if was_above_min:
            now_above_min = battery_soc > battery_soc_min
        else:
            now_above_min = battery_soc >= battery_soc_min + battery_soc_hysteresis
        hub_runtime["_soc_above_min"] = now_above_min
        if not now_above_min:
            battery_soc_min = battery_soc_min + battery_soc_hysteresis
    return (
        battery_soc_target,
        battery_soc_min,
        now_above_target,
        now_above_min,
    )


def _apply_phase_remaps(site, auto_detect_state):
    """Re-point loads onto the phases auto-detection worked out earlier.

    Runs before the calculation so this cycle already allocates on the
    corrected mapping; the detection that produced it ran on a previous
    cycle (see _run_auto_detection). Mutates the loads in place.
    """
    phase_remaps = auto_detect_state.get("phase_remap", {})
    for load in site.loads:
        remap = phase_remaps.get(load.load_id)
        if remap:
            old = (load.l1_phase, load.l2_phase, load.l3_phase)
            load.l1_phase = remap["l1_phase"]
            load.l2_phase = remap["l2_phase"]
            load.l3_phase = remap["l3_phase"]
            # Recalculate active_phases_mask from new mapping
            if load.phases == 3:
                load.active_phases_mask = "".join(
                    sorted({load.l1_phase, load.l2_phase, load.l3_phase})
                )
            elif load.phases == 2:
                load.active_phases_mask = "".join(
                    sorted({load.l1_phase, load.l2_phase})
                )
            elif load.phases == 1:
                load.active_phases_mask = load.l1_phase
            _LOGGER.debug(
                "Auto-remap applied for %s: L1:%s→%s L2:%s→%s L3:%s→%s mask=%s",
                load.entity_id,
                old[0],
                load.l1_phase,
                old[1],
                load.l2_phase,
                old[2],
                load.l3_phase,
                load.active_phases_mask,
            )


def _apply_excess_latch(hub_runtime, site, excess_hysteresis):
    """Decide whether Excess mode is engaged this cycle, with hysteresis.

    Sets ``site.excess_hysteresis`` to the band the calculator should use
    and returns ``(excess_on, margin)`` for the status/result side. The
    latch bit lives in ``hub_runtime``.
    """
    # Excess trigger + hysteresis latch. excess_margin() returns the watts by
    # which everything the site is absorbing (grid export + battery charging)
    # exceeds everything it is allowed to absorb (export allowance + battery
    # charge allowance); >= 0 means on. Once engaged, the band widens by the
    # hub's Excess hysteresis so a load doesn't chatter at the trigger point.
    # The latch lives here so the calculator stays stateless - it just reads
    # site.excess_hysteresis. The per-sink breakdown goes to excess_margin()'s
    # own debug line.
    #
    # Evaluated on POST-feedback figures: the pools compare export with load
    # draws added back, and pre-feedback export is already eaten by the Excess
    # load's own draw - the band would never engage exactly when a load is running.
    was_excess_on = hub_runtime.get("_excess_on", False)
    margin = excess_margin(site, excess_hysteresis if was_excess_on else 0)
    excess_on = margin >= 0
    hub_runtime["_excess_on"] = excess_on
    site.excess_hysteresis = excess_hysteresis if excess_on else 0
    _LOGGER.debug(
        "Excess %s (margin %+.0fW)", "ON" if excess_on else "off", margin
    )
    return excess_on, margin


# How long the saturation sign must hold before it is believed: the engine
# drives the pools on a battery reading up to INPUT_STALE_TIMEOUT old
# (readers._stale_guard), so a battery that answers our loads inside that
# window is never taken for one that cannot.
SATURATION_CONFIRM_S = INPUT_STALE_TIMEOUT
# How long a verdict holds before the rating is offered again - judgements,
# not measurements: from the meter nothing tells a limit that has gone from one
# that has not, so the only test is to offer the rating and watch, and a limit
# still there costs one detection's overrun (and, where what fits is below a
# load's minimum, one start). A quarter hour is the block a capacity tariff
# averages import over, so each block pays for one at most; every offer the
# limit answers doubles the wait, up to an hour - the longest a verdict that
# has gone stale may keep the battery from the loads.
SATURATION_HOLD_S = 15 * 60.0
SATURATION_HOLD_MAX_S = 60 * 60.0


def _apply_saturation_latch(hub_runtime, site, now):
    """A meter-only hybrid at its limit: hold its battery at what it gave then.

    With no solar and no output sensor the sun the house uses itself reaches
    no meter, so by day the rating cap is short by it and the inverter pool
    offers the battery's rating less its flow where the inverter cannot pass
    it: 3 kW of sun and a 1 kW house on a 7 kW inverter permitted a car 30.4 A
    where 26.1 A fits, and the grid carried the 1 kW past the allowance
    (dev/tests/test_gridtied_inverter_pool.py). A hybrid at its limit cannot
    answer more load from sun + battery, so the grid does. The SIGN, all of
    it for SATURATION_CONFIRM_S on end:

        the grid carries our loads more than the grid half grants them
        (``grid_overdraw``) by over DEAD_BAND - the least the permit acts on;
        the site's import rose by over DEAD_BAND since the grid last stood
        inside the grant (or our loads last drew less), and our loads grew by
        at least that rise; the battery, more than DEAD_BAND below its
        rating, did not rise by DEAD_BAND (a rise restarts the count).

    Seen, the battery is held at the flow it gave (``site.
    battery_discharge_ceiling``): the inverter pool offers what the inverters
    already give beyond the house and no more, the grid half its grant as
    usual. The ceiling follows the flow up (the battery showed it can) and
    down to it at each new verdict; it goes when the flow reaches the rating,
    or SATURATION_HOLD_S after the verdict, doubled for every offer the limit
    answered, to SATURATION_HOLD_MAX_S - from the meter a sun that rises looks
    like a house that falls, so only an offer can show the limit has gone.

    What else shows the sign, and why it is excluded or harmless:
      * a battery slow to answer our loads moves inside the window; one whose
        answer takes longer than INPUT_STALE_TIMEOUT is not told apart;
      * an import our loads did not raise - the battery's own grid setpoint,
        the house - is not the site's import rising with them, so a steady
        one never latches, nor ratchets our loads down;
      * a battery its own logic holds (a BMS limit, its own reserve, a charge
        priority, a forced charge) gives no more than its flow either, so the
        ceiling is the truth there too; below the hub's SOC minimum its rating
        is 0 already and nothing latches;
      * at night the rating cap is exact, and a healthy battery never shows it;
      * an import inside the allowance is inside the grant.

    Grid-tied, solar from the meter alone (``solar_is_metered``), the
    battery's power read, a usable rating, an Inverter Max Power - everywhere
    else the state is dropped. It lives in ``hub_runtime`` so the calculator
    stays stateless.
    """
    state = hub_runtime.setdefault("_saturation", {})
    rating = site.battery_max_discharge_power or 0.0
    if (
        site.is_off_grid
        or not site.solar_is_metered
        or not site.inverter_max_power
        or site.battery_power is None
        or rating <= 0
    ):
        state.clear()
        return
    band = DEAD_BAND * site.voltage
    flow = site.battery_power
    draw = sum(site.managed_phase_draws or ()) * site.voltage
    over = grid_overdraw(site) * site.voltage
    ceiling = state.get("ceiling")
    hold = state.get("hold", SATURATION_HOLD_S)
    if ceiling is not None and flow >= rating - band:
        ceiling, hold = None, SATURATION_HOLD_S
    elif ceiling is not None and now - state["seen"] > hold:
        ceiling = None
        state["offered"] = now
    # Where growth is measured from, (our draw, the site's import): the last
    # cycle inside the grant, or the least our loads have drawn since. None
    # until the grid has stood inside it once.
    net = site.net_grid_power or 0.0
    ref = state.get("ref")
    if over <= 0 or (ref is not None and draw < ref[0]):
        ref = (draw, net)
    if (
        ref is not None
        and over > band
        and net - ref[1] > band
        and draw - ref[0] >= net - ref[1] - band
        and flow < rating - band
    ):
        since, flow0 = state.get("episode", (now, flow))
        if flow > flow0 + band:
            since, flow0 = now, flow
        state["episode"] = (since, flow0)
        if now - since >= SATURATION_CONFIRM_S:
            if ceiling is None:
                answered = now - state.get("offered", -math.inf) <= hold
                hold = (
                    min(2 * hold, SATURATION_HOLD_MAX_S)
                    if answered
                    else SATURATION_HOLD_S
                )
            ceiling = flow if ceiling is None else min(ceiling, flow)
            state["seen"] = now
            ref = (draw, net)  # another verdict needs more growth
    else:
        state.pop("episode", None)
    if ceiling is not None:
        ceiling = max(ceiling, flow)
    state.update(ref=ref, ceiling=ceiling, hold=hold)
    site.battery_discharge_ceiling = ceiling
    if ceiling is not None:
        _LOGGER.debug(
            "Inverter at its limit: battery held at %.0fW (flow %.0fW, "
            "rating %.0fW, grid %+.0fW past its grant, next offer in %.0fs)",
            ceiling, flow, rating, over, hold - (now - state["seen"]),
        )


def _apply_household_figures(
    site,
    members,
    hub_entry,
    hub_runtime,
    solar_is_derived,
    solar_production_total,
    battery_power,
    managed_draws,
):
    """Fill in what the household (everything unmanaged) is drawing.

    Post-feedback on purpose: both the total and the per-phase figures are
    derived from readings the managed draws have already been taken out of
    (off-grid the total takes them out itself - see below).
    Sets ``site.household_consumption_total`` / ``site.household_consumption``
    and owns the asymmetric hold state in ``hub_runtime``. ``members`` also
    says whether every battery's power is read - off-grid with no output
    sensors, the condition for knowing the site's supply at all.

    ``managed_draws`` is this cycle's smoothed draw from _managed_phase_draws -
    the one list every other view subtracts too. The series household - and
    off-grid the parallel one - is the SMOOTHED inverter output minus the
    managed draw, so the draw has to be on the same EMA step: with the raw
    draw, a charger's start came off at once while the output it is part of
    was still catching up, the household read low by the filter's lag and the
    permit went over the inverter's allowance by as much (off-grid, where the
    inverter rating is the whole allowance: up to 773 W,
    dev/tests/test_offgrid_household_smoothing.py).
    Off-grid the feedback loop never ran, so the off-grid household total
    takes the same draw off itself.
    """
    if site.is_off_grid and site.inverter_output_per_phase is None:
        # Off-grid with no inverter output sensors, solar + battery is the
        # whole supply on the AC bus - the site's one measure of what it draws
        # - and it is known whenever every battery's power is read. A solar
        # sensor reading 0 W at night is a reading, not an absent one; with no
        # solar sensor at all the solar figure is the engine's own (inferred
        # from battery charging, so 0 while the battery discharges) and the
        # battery's discharge is the supply. Built only from a solar reading
        # above 0, the household fell back to the synthetic 0 A grid phases at
        # night and on a battery-only site, and the inverter's whole rating
        # went out as headroom: on a 6 kW inverter with a 1 kW house the car
        # got 26.1 A and the inverter ran 1003 W - the house - over its rating
        # (dev/tests/test_offgrid_battery_household.py). A battery whose power
        # is not read leaves the supply unknowable, and no total is built:
        # _calculate_inverter_limit then hands out nothing rather than the
        # whole rating.
        build_total = all(
            m.battery_power is not None for m in members if m.has_battery
        )
    else:
        # Grid-tied (and off-grid with output sensors) only a measured
        # production figure provides the ground truth.
        build_total = not solar_is_derived and solar_production_total > 0
    if build_total:
        export_power_after_feedback = site.export_current.total * site.voltage
        bp = float(battery_power) if battery_power is not None else 0
        # Grid-tied the feedback loop has put the managed draws back onto the
        # export, so they come off here through it. Off-grid it leaves the
        # synthetic zero phases alone, and solar + battery is the inverters'
        # whole supply, our own loads included - left in, a charger's draw was
        # house load and the allowance it is sized on (rating - household)
        # shrank by that draw: a car meant to get 21.7 A settled at 10.9 A
        # (dev/tests/test_offgrid_solar_household.py), and an
        # inverter-limited car hunted (dev/tests/test_offgrid_battery_headroom.py). ``managed_draws`` is the
        # one smoothed list every view subtracts, on the same EMA step as the
        # solar and battery readings it comes off.
        managed_power = (
            sum(managed_draws) * site.voltage if site.is_off_grid else 0.0
        )
        site.household_consumption_total = max(
            0,
            solar_production_total + bp - export_power_after_feedback
            - managed_power,
        )
        _LOGGER.debug(
            "Computed household_consumption_total=%.1fW (solar=%.1fW + bat=%.1fW "
            "- export=%.1fW - managed=%.1fW)",
            site.household_consumption_total,
            solar_production_total,
            bp,
            export_power_after_feedback,
            managed_power,
        )

    # Compute per-phase household from inverter output entities (after feedback)
    if fleet.mixed_topologies(members):
        household = _mixed_household_per_phase(site, members, managed_draws)
    else:
        household = compute_household_per_phase(
            site, site.wiring_topology, managed_draws
        )
    if household is not None:
        # Asymmetric hold on the household floor. The managed draw side of the
        # subtraction (OCPP, sub-second) can report before the polled inverter
        # output does - the two share one input EMA, but not one sensor
        # cadence - so a ramping car can still transiently zero household and
        # the engine would hand the real household's power out as headroom.
        # Rises pass straight through; falls are bridged over
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
        # No inverter output data at all - nothing to hold, and the held value
        # must be dropped so it cannot resurrect on a later cycle.
        hub_runtime.pop("_household_held", None)


def _apply_sun_probe(hass, hub_entry, hub_runtime, site):
    """Off-grid with no battery, where nothing measures the spare sun: probe it.

    A PV-only array follows demand, so read through its output sensors alone,
    or a solar sensor alone, the sun it is not asked for shows nowhere: the
    unused sun is 0 by identity (target_calculator._off_grid_unused_sun), a
    running load keeps what it draws and one at 0 A never starts. (A solar
    sensor beside output sensors measures the spare and needs none of this.)

    So offer one STEP on top of what our loads hold (``site.sun_probe``, which
    every pool carries and caps at the rating less the house and each leg,
    like the rest of the unused sun) and watch the production:

    - it rose by the step, to within DEAD_BAND: the sun had room. The loads
      keep it - a running load keeps what it draws - and the next step may
      follow on the next cycle: growth, one step at a time, only while
      production does. The offer itself is the step PLUS DEAD_BAND, so what a
      started load is left drawing clears its minimum by the band: offered
      exactly its minimum, a reading a hair low cuts it (on the closed loop
      below, behind a solar sensor alone, the pool came out a float's width
      under 6 A and the car was stopped 16 s after it started).
    - it had not by the end of the window: the loads are held to what the
      production DID follow - a car left under its minimum stops - and no
      step is offered for the load's own restart dwell (its charge pause; a
      plug's or tank's minimum off time) plus one command interval: the stop
      may wait that long to be sent, and the load's dwell only counts from it.
      Each failed try in a row - a start or a growth step, one count per load
      - doubles that pause, up to SUN_PROBE_MAX_PAUSE_S: on a marginal site
      every try asks the array for a step past the sun, and an inverter that
      trips on overload drops out on each. A try production follows resets it.
      Nothing else does: every other signal is either blind here (a solar
      sensor alone reads the demand, not the sun) or a guess (the forecast; a
      new day, whose dawn is the weakest sun) - and the house "dropping", read
      here as production less our loads, is also how an inverter that has
      tripped reads: a house gone to nothing. The count and the next try are shown on the load's status
      (``LOAD_RT_SUN_PROBE`` in its runtime bucket, entities/sun_probe.py).

    The step is the minimum of the first load in rank order that could take
    more: what a load at 0 A needs to start, and for a running one the same
    size, so a failed growth step never asks more of the sun than a failed
    start. The window is the chain a step travels before production can show
    it, each link a figure the integration already uses for it: the load's
    command interval (the send gate), the draw settle time (a car's ramp) and
    the household hold bridge (how far an inverter output lags a draw) -
    15 + 15 + 15 = 45 s at the defaults (dev/tests/test_offgrid_sun_probe.py).

    State in ``hub_runtime["_sun_probe"]`` and each load's backoff, dropped
    wherever the gate fails.
    """
    loads_rt = hass.data[DOMAIN].get("loads", {})
    if (
        not site.is_off_grid
        or site.battery_power is not None
        or site.battery_soc is not None
        or site.solar_is_derived == (site.inverter_output_per_phase is None)
    ):
        hub_runtime.pop("_sun_probe", None)
        for load in site.loads:
            loads_rt.get(load.load_id, {}).pop(LOAD_RT_SUN_PROBE, None)
        return
    now = time.monotonic()
    supply = (site.solar_production_total or 0.0) / site.voltage
    held = sum(site.managed_phase_draws or ())
    probe = hub_runtime.get("_sun_probe")
    if probe and probe["step"]:
        load_rt = loads_rt.get(probe["load"], {})
        followed = max(0.0, supply - probe["supply"])
        if followed >= probe["step"] - probe["band"]:
            _LOGGER.debug("Sun probe: production followed %.1f A", followed)
            load_rt.pop(LOAD_RT_SUN_PROBE, None)
            probe = None
        elif now < probe["until"]:
            site.sun_probe = probe["held"] + probe["step"] + probe["band"] - held
            return
        else:
            last = load_rt.get(LOAD_RT_SUN_PROBE) or {}
            pause = max(
                probe["pause"],
                min(2 * last.get("pause_s", 0.0), SUN_PROBE_MAX_PAUSE_S),
            )
            failed = last.get("failed_tries", 0) + 1
            load_rt[LOAD_RT_SUN_PROBE] = {
                "failed_tries": failed,
                "pause_s": pause,
                "next_try_at": datetime.now(timezone.utc) + timedelta(seconds=pause),
            }
            _LOGGER.debug(
                "Sun probe: production followed %.1f A of %.1f A - holding "
                "our loads to it for %.0f s (failed try %d in a row)",
                followed, probe["step"], pause, failed,
            )
            probe = hub_runtime["_sun_probe"] = {
                "step": 0.0,
                "held": probe["held"] + followed,
                "until": now + pause,
            }
    if probe and now < probe["until"]:
        site.sun_probe = min(0.0, probe["held"] - held)
        return
    hub_runtime.pop("_sun_probe", None)

    wanting = sorted(
        (
            load for load in site.loads
            if load.dynamic_control
            and load.active_phases_mask
            and not load.unmetered
            and (
                load.device_type == DEVICE_TYPE_PLUG
                or load.connector_status not in INACTIVE_STATUSES
            )
            and max(load.get_site_phase_draw()) < load.max_current - DEAD_BAND
        ),
        key=lambda load: (load.mode_priority, load.priority),
    )
    if not wanting:
        return
    load = wanting[0]
    entry = loads_rt.get(load.load_id, {}).get("entry")

    def setting(key, default):
        return float((get_entry_value(entry, key, default) if entry else default) or 0)

    if load.device_type in (DEVICE_TYPE_PLUG, DEVICE_TYPE_HOT_WATER_TANK):
        dwell = setting(CONF_BINARY_MIN_OFF_TIME, DEFAULT_BINARY_MIN_OFF_TIME)
    else:
        dwell = setting(CONF_CHARGE_PAUSE_DURATION, DEFAULT_CHARGE_PAUSE_DURATION)
    interval = setting(CONF_UPDATE_FREQUENCY, DEFAULT_UPDATE_FREQUENCY)
    settle = float(
        get_entry_value(hub_entry, CONF_FILTER_SETTLE_SECONDS, SETTLE_DRAW_SECONDS)
    )
    step = load.min_current * len(load.active_phases_mask)
    band = DEAD_BAND * len(load.active_phases_mask)
    hub_runtime["_sun_probe"] = {
        "load": load.load_id,
        "step": step,
        "band": band,
        "held": held,
        "supply": supply,
        "until": now + interval + settle + HOUSEHOLD_HOLD_BRIDGE_SECONDS,
        "pause": dwell * 60 + interval,
    }
    site.sun_probe = step + band
    _LOGGER.debug(
        "Sun probe: offering %.1f A more (for %s) on %.1f A held",
        step + band, load.entity_id, held,
    )


def _apply_grid_stale_fallback(site, grid_stale_duration):
    """Override the calculated permits once the grid CTs have been blind too long.

    Returns whether the timeout has been exceeded - the hub status and the
    published result both report it.
    """
    # --- Grid stale fallback: force min_current after timeout ---
    grid_stale = grid_stale_duration > GRID_STALE_TIMEOUT
    if grid_stale:
        _LOGGER.warning(
            "Grid CT unavailable for %.0fs (>%ds) - charging EVSEs falling to "
            "minimum current, binary loads switched off",
            grid_stale_duration,
            GRID_STALE_TIMEOUT,
        )
        for load in site.loads:
            # Only an EVSE already charging keeps a minimum-current permit (a
            # hard stop mid-charge is worse than 6 A on a blind site). Binary
            # loads (plugs/tanks) and idle EVSEs get no permit - a permit > 0
            # switches a binary load ON, and energizing a load the engine had
            # deliberately shed while it cannot see the site is unsafe.
            if (
                load.device_type == DEVICE_TYPE_EVSE
                and load.connector_status == "Charging"
            ):
                load.allocated_current = load.min_current
                load.available_current = load.min_current
            else:
                load.allocated_current = 0
                load.available_current = 0
    return grid_stale


def _build_group_data(site):
    """Per-circuit-group allocation data for the group sensors.

    Read-only over the calculated site: what each group's members were
    allocated per phase, and how much of the group's breaker limit that
    leaves. Keyed by group_id.
    """
    # --- Build per-group allocation data for group sensors ---
    group_data = {}
    load_by_id = {c.load_id: c for c in site.loads}
    for group in site.circuit_groups:
        per_phase_draw = {"A": 0.0, "B": 0.0, "C": 0.0}
        for mid in group.member_ids:
            c = load_by_id.get(mid)
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
    return group_data


def _run_auto_detection(hub_entry, auto_detect_state, smoothed_phases, site):
    """Run the CT-inversion and phase-mapping detectors for this cycle.

    Returns the notifications to publish. Any phase remap it works out is
    stored in ``auto_detect_state`` for the NEXT cycle to apply (see
    _apply_phase_remaps) - never mid-calculation.
    """
    # --- Auto-detection (inversion + phase mapping) ---
    # auto_detect_state is initialized by the caller before the first use.
    auto_notifications = []
    inv_notif = check_inversion(
        auto_detect_state,
        smoothed_phases,
        site.loads,
        hub_entry.entry_id,
        get_entry_value(hub_entry, CONF_NAME, "Hub"),
    )
    if inv_notif:
        auto_notifications.append(inv_notif)
    if get_entry_value(hub_entry, CONF_AUTO_DETECT_PHASE_MAPPING, True):
        pm_results = check_phase_mapping(
            auto_detect_state,
            smoothed_phases,
            site.loads,
            hub_entry.entry_id,
        )
        for notif in pm_results:
            # Store auto-remap for next cycle
            remap = notif.pop("auto_remap", None)
            if remap:
                auto_detect_state.setdefault("phase_remap", {})[remap["load_id"]] = (
                    remap
                )
                # Reset correlation state so re-detection runs with new mapping
                # (allows 2-phase detection to verify/correct after 1-phase remap)
                pm_state = auto_detect_state.get("phase_map", {})
                pm_state.pop(remap["load_id"], None)
            auto_notifications.append(notif)
    return auto_notifications


def _build_hub_status(
    hass,
    hub_entry,
    members,
    has_grid_cts,
    inverter_output_per_phase,
    grid_stale,
    grid_stale_duration,
):
    """The hub Status sensor's state and its warnings attribute.

    Returns ``(hub_status, hub_warnings)``. Names the specific sensor or
    setting at fault rather than a generic error, so the user knows what to
    fix without reading the log.
    """
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
        # Any fleet member's battery satisfies the requirement - the battery
        # may live on the hub's legacy fields or on an inverter entry.
        hub_warnings.append("Off-grid mode (no grid CTs)")
        if not any(m.battery_soc is not None or m.has_battery for m in members):
            hub_status = "Setup incomplete"
            hub_warnings.append(
                "Off-grid hub needs a battery SOC sensor - it drives the "
                "operating-mode logic. Set it on the hub or an inverter entry."
            )
        if not any(m.has_battery_power_entity for m in members):
            hub_status = "Setup incomplete"
            hub_warnings.append(
                "Off-grid hub needs a battery power sensor - it is used to "
                "detect available solar surplus. Set it on the hub or an "
                "inverter entry."
            )
        if not has_inverter_output and not has_solar_entity:
            hub_warnings.append(
                "Off-grid hub has no inverter output or solar production "
                "sensor - available solar can only be inferred from battery "
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
    return hub_status, hub_warnings


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def run_hub_calculation(hass, hub_entry, load_entries=None):
    """
    Run the hub calculation: read HA states, build SiteContext, calculate targets.

    This is the ONE site calculation for a hub, run once per site cycle by the
    hub's DataUpdateCoordinator (see sensor.py). It takes no entity - every
    cycle-counted mechanism inside (settle counters, input EMAs, power-stable
    counts) advances exactly once per call, so a site with N loads no longer
    advances them N times per interval.

    Args:
        hass: Home Assistant instance
        hub_entry: the hub's ConfigEntry
        load_entries: optional explicit list of load config entries; None
            reads the hub's registered loads

    Returns:
        dict with calculated values including:
            - CONF_TOTAL_ALLOCATED_CURRENT: Total allocated current (A)
            - CONF_PHASES: Number of phases
            - CONF_CHARGING_MODE: Current charging mode
            - load_targets: per-load target currents
            - Other site/load data
    """
    (
        voltage,
        main_breaker_rating,
        excess_hysteresis,
        excess_threshold,
    ) = _read_hub_config(hub_entry)

    raw_phases, has_grid_cts = _read_site_phases(hass, hub_entry, voltage)

    # --- Input EMA smoothing (grid CT, solar, battery power) ---
    hub_runtime = hass.data[DOMAIN]["hubs"].get(hub_entry.entry_id, {})
    ema_inputs = hub_runtime.setdefault("_ema_inputs", {})
    # Every _smooth call this cycle uses a weight derived from how often this
    # site actually refreshes, so the filter's behaviour is fixed in SECONDS
    # rather than in cycles. Without this a site polled slowly was silently
    # filtered far harder than one polled quickly - see EMA_TAU_S.
    set_ema_interval(
        ema_inputs,
        get_entry_value(
            hub_entry, CONF_SITE_UPDATE_FREQUENCY, DEFAULT_SITE_UPDATE_FREQUENCY
        ),
        # The Filters page; each default is the constant it overrides.
        tau=get_entry_value(hub_entry, CONF_FILTER_INPUT_TAU_S, EMA_TAU_S),
        fast_tau=get_entry_value(
            hub_entry, CONF_FILTER_CTRL_FAST_TAU_S, CTRL_FAST_TAU_S
        ),
    )

    # --- Resolve unreadable grid CTs (the only place allowed to substitute) ---
    # ``grid_assumed_phases`` marks the phases standing on the main-breaker
    # worst case rather than on a reading or a held EMA value. The allocation
    # below runs on the assumption exactly as before - that is what keeps a
    # blind site from handing out headroom - but the PUBLISHED grid
    # measurements must not carry it (see _build_hub_result).
    raw_phases, any_grid_stale, grid_assumed_phases = _resolve_grid_phases(
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
    # The charge controller's view of the same phases: fast toward either
    # limit, slow back toward zero, under its own EMA keys (see
    # _charge_control_view). The symmetric values above feed everything else.
    ctrl_phases = [
        _smooth_directional(ema_inputs, f"grid_ctrl_{i}", r, fast_away=True)
        for i, r in enumerate(raw_phases)
    ]
    ctrl_consumption_pv = PhaseValues(
        *[max(0, r) if r is not None else None for r in ctrl_phases]
    )
    ctrl_export_pv = PhaseValues(
        *[max(0, -r) if r is not None else None for r in ctrl_phases]
    )

    total_export_current = export_pv.total
    total_export_power = total_export_current * voltage if voltage > 0 else 0

    # --- Inverter fleet (inverter entries + legacy hub-level fields) ---
    # Every battery/inverter scalar below is a fleet aggregate. With a single
    # member (the classic setup) each aggregate reduces to exactly the old
    # singleton value - see engine/fleet.py for the per-member gating rules.
    members = _read_fleet_members(hass, hub_entry, hub_runtime, ema_inputs, voltage)
    # Off-grid every output carries its own battery's flow, parallel included
    # (fleet.member_solar) - the one place the solar derivation learns it.
    for m in members:
        m.off_grid = not has_grid_cts

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
    battery_power_ctrl = fleet.battery_power_total(members, "battery_power_ctrl")
    battery_soc_hysteresis = get_entry_value(
        hub_entry, CONF_BATTERY_SOC_HYSTERESIS, DEFAULT_BATTERY_SOC_HYSTERESIS
    )
    # Charge capacity sums only members whose own battery is below its own
    # full-SOC, and sums what each one is PERMITTED to take rather than its
    # nameplate rate: while our own Battery Charge Control holds a member's
    # register below that rate, the difference is not somewhere this site can
    # place production (see fleet.charge_power_total). This scalar has exactly
    # one consumer, the Excess verdict - calculations.excess_margin - so the
    # narrowing reaches nothing else. Discharge is summed after the SOC-min
    # hysteresis latch below.
    battery_max_charge_power = fleet.charge_power_total(members)

    max_grid_import_power = _read_max_import_power(hass, hub_entry)

    # --- Inverter configuration (fleet) ---
    # Member outputs are already stale-guarded + smoothed at read time (per-
    # member EMA keys). The topology scalar is 'series' if ANY member is
    # series: the series solar formula on the summed outputs with the summed
    # battery power is exact for any mix, because parallel members contribute
    # no battery term - so the post-feedback re-derivation stays correct.
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
    # to discount - the two errors would cancel the load-off add-back and the
    # gate would suppress itself (issue #41).
    inverter_output_total = fleet.output_power_total(
        members,
        voltage,
        solar_w=solar_production_total,
        battery_power_w=battery_power,
    )
    # Signed net grid flow (+ import / − export) from the smoothed phases. Off
    # grid every phase reads 0, so this is 0 - correct: nothing to import.
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
    # fleet passes None - its full gating already happened per member in
    # fleet.charge_power_total(), and a fleet-SOC gate would be wrong.
    battery_soc_full = fleet.soc_full_scalar(members)

    (
        battery_soc_target,
        battery_soc_min,
        now_above_target,
        now_above_min,
    ) = _apply_soc_hysteresis(
        hub_runtime,
        battery_soc,
        battery_soc_hysteresis,
        battery_soc_target,
        battery_soc_min,
    )

    # Discharge capacity sums only members whose OWN battery is at/above the
    # (hysteresis-adjusted) hub minimum - a battery below the floor cannot be
    # counted dischargeable because a full sibling lifts the fleet SOC.
    battery_max_discharge_power = fleet.discharge_power_total(members, battery_soc_min)

    # Apply power buffer to reduce effective max grid import power
    if max_grid_import_power is not None and power_buffer > 0:
        max_grid_import_power = max(0, max_grid_import_power - power_buffer)

    # (Excess-export hysteresis is applied after the feedback loop below - it
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
        solar_is_metered=fleet.solar_is_metered(members),
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

    # --- Add loads ---
    hub_entry_id = (
        hub_entry.entry_id
        if hasattr(hub_entry, "entry_id")
        else hub_entry.data.get("hub_entry_id")
    )
    _add_loads_to_site(
        hass, site, hub_entry_id, load_entries,
        settle_seconds=get_entry_value(
            hub_entry, CONF_FILTER_SETTLE_SECONDS, SETTLE_DRAW_SECONDS
        ),
    )

    # --- Build circuit groups ---
    site.circuit_groups = _build_circuit_groups(hass, hub_entry_id)

    # Apply auto-detected phase remaps from previous cycles
    auto_detect_state = hub_runtime.setdefault("_auto_detect", {})
    _apply_phase_remaps(site, auto_detect_state)

    # --- Stuck charger readouts, judged against the household ---
    # On the phases as remapped, and on the household as the engine is about
    # to reconstruct it - the grid reading minus our draws, or off-grid the
    # inverter output minus our draws - which is where a readout stuck LOW
    # does its damage (see engine/readout_watch.observe_household).
    _watch_readouts_against_household(
        hass,
        site,
        *_supply_per_phase(
            raw_phases, grid_assumed_phases, has_grid_cts, members,
            inverter_output_total, voltage,
        ),
    )

    # --- Feedback loop ---
    # The managed-draw EMA advances HERE, once per cycle, like every other
    # input filter - and both views subtract this one result.
    managed_draws = _managed_phase_draws(site, ema_inputs)
    site.managed_phase_draws = tuple(managed_draws)
    _apply_feedback_loop(site, solar_is_derived, members, managed_draws)
    ctrl_site = _charge_control_view(
        site,
        ctrl_consumption_pv,
        ctrl_export_pv,
        float(battery_power_ctrl) if battery_power_ctrl is not None else None,
        # Its phases come from the DIRECTIONAL smoothers, but the draw it
        # subtracts is the same smoothed term the site view used - one value,
        # so the two views cannot disagree about what our loads draw.
        managed_draws,
    )

    excess_on, margin = _apply_excess_latch(hub_runtime, site, excess_hysteresis)

    _apply_household_figures(
        site,
        members,
        hub_entry,
        hub_runtime,
        solar_is_derived,
        solar_production_total,
        battery_power,
        # This cycle's one smoothed draw, not a raw re-sum: the series
        # household (off-grid, either wiring's) subtracts it from the SMOOTHED
        # inverter output, and an
        # off-grid site with no output sensors takes it off the solar-sensor
        # total the same way.
        managed_draws,
    )
    _apply_sun_probe(hass, hub_entry, hub_runtime, site)

    _apply_saturation_latch(hub_runtime, site, time.monotonic())

    # --- Calculate targets ---
    calculate_all_load_targets(site)

    grid_stale = _apply_grid_stale_fallback(site, grid_stale_duration)

    load_targets = {c.load_id: c.allocated_current for c in site.loads}
    load_available = {c.load_id: c.available_current for c in site.loads}
    load_names = {c.load_id: c.entity_id for c in site.loads}

    # Persist this cycle's permit for next-cycle settle detection - an EVSE
    # only counts as "settled and under-drawing" when its measured draw stays
    # below the permit we last offered it.
    loads_rt = hass.data[DOMAIN].get("loads", {})
    for c in site.loads:
        rt = loads_rt.get(c.load_id)
        if rt is not None:
            rt["_last_permit"] = c.available_current

    group_data = _build_group_data(site)

    auto_notifications = _run_auto_detection(
        hub_entry, auto_detect_state, smoothed_phases, site
    )

    hub_status, hub_warnings = _build_hub_status(
        hass,
        hub_entry,
        members,
        has_grid_cts,
        inverter_output_per_phase,
        grid_stale,
        grid_stale_duration,
    )

    # --- Per-inverter data for the inverter-entry sensors ---
    # The legacy implicit member (the hub's own fields) has no device of its
    # own - its values already show on the hub's fleet sensors.
    inverters_data = {
        m.entry_id: {
            "name": m.name,
            # None while this member's own production sensor is unreadable with
            # nothing to hold - its device sensor reads unknown rather than a
            # confident 0 W. A healthy sibling keeps publishing its own figure.
            "solar_w": fleet.member_solar_published(m, voltage),
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
        excess_on,
        ctrl_site=ctrl_site,
    )
    for inv_id, advice in forecast_per_inverter.items():
        if inv_id in inverters_data:
            inverters_data[inv_id].update(advice)

    # --- Build result ---
    return _build_hub_result(
        site,
        raw_phases,
        voltage,
        battery_soc,
        battery_soc_min,
        battery_max_discharge_power,
        battery_power,
        load_targets,
        load_available,
        load_names,
        auto_notifications,
        group_data,
        grid_stale=grid_stale,
        grid_assumed=any(grid_assumed_phases),
        # Same split for solar: the calculation keeps the conservative 0 W of a
        # dead production sensor, the published measurement does not.
        solar_assumed=fleet.solar_is_assumed(members),
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
    "calculate_all_load_targets",
    "run_hub_calculation",
]
