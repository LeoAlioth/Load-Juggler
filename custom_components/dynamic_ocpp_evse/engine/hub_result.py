"""Load Juggler - the hub's published result: forecast advice and hub_data.

The far side of the cycle from engine/readers.py. ``_build_hub_result()``
assembles the single dict the hub coordinator publishes — the site figures, the
per-load allocations, the per-inverter data and the hub status — and
``_compute_forecast_advice()`` derives the advisory battery-headroom keys from
the PV clipping forecast that ride along with it. Nothing here reads HA states
or decides allocations; it shapes what has already been calculated.

Split out of hub_calculation.py, which now consumes these rather than defining
them.
"""

# PEP 604 unions (``float | None``) appear in this module's signatures. Nothing
# here evaluates annotations at runtime (no dataclasses, NamedTuple/TypedDict or
# get_type_hints calls), so deferring them keeps the module importable on the
# Python 3.9 interpreters the standalone test runners use (same arrangement as
# engine/auto_detect.py).
from __future__ import annotations

import logging

from ..calculations import (
    clipping_forecast,
    battery_max_soc,
    headroom_deficit_kwh,
    recommended_charge_limit,
)
from ..const import (
    CONF_BASE_CONSUMPTION,
    CONF_EXCESS_TRIGGER_MARGIN,
    CONF_FORECAST_SOC_FLOOR,
    CONF_GRID_EXPORT_LIMIT,
    CONF_PHASES,
    CONF_SOLAR_FORECAST_ENTITY_IDS,
    CONF_TOTAL_ALLOCATED_CURRENT,
    DEFAULT_BASE_CONSUMPTION,
    DEFAULT_EXCESS_TRIGGER_MARGIN,
    DEFAULT_FORECAST_SOC_FLOOR,
    DEFAULT_GRID_EXPORT_LIMIT,
    FORECAST_SOC_HYSTERESIS,
)
from ..helpers import get_entry_value
from . import fleet
from .forecast_reader import (
    read_forecast_series,
    forecast_window,
    configured_forecast_sensors,
)

_LOGGER = logging.getLogger(__name__)


def _compute_forecast_advice(
    hass,
    hub_entry,
    hub_runtime,
    site,
    battery_soc,
    members,
):
    """Advisory battery headroom from the PV clipping forecast.

    Returns ``(hub_advice_or_None, per_inverter_advice)``. Enabled only when
    an export limit, fleet battery capacity and at least one forecast source
    are configured — the hub publishes advice sensors, it never commands the
    house battery (a future write-control on the inverter entries will
    optionally push these values to a device).

    Fleet semantics: capacity and charge rate are FLEET sums, and the charge
    sum is UNGATED — the forecast reserves room for the rest of the day, not
    for the current instant, so a battery that is momentarily full still
    counts (its ceiling advice is exactly what empties it). Reserving
    ``absorbable_kwh`` across the fleet at one uniform ceiling ``s`` gives
    ``Σ cap_i × (100 − s)/100`` — precisely what battery_max_soc computes
    from the summed capacity — so every battery is advised the same percent,
    splitting the headroom proportionally to capacity by construction. The
    recommended charge limit splits proportionally to each member's charge
    cap, clamped to its own cap.

    The energy question and the power question are asked at DIFFERENT
    thresholds — the integral at the true export limit, the instantaneous
    charge-limit advice one Excess trigger margin below it, so a
    hard-limiting inverter cannot mask the signal. The derivation below
    spells out why.

    Two fleet-level pieces of carried state, both in ``hub_runtime`` and
    neither ever per-member (the advice is uniform by construction, so
    per-member state would diverge):

    * ``_forecast_max_soc`` — the published ceiling's ratchet, mirroring the
      Excess latch: it rises freely and falls only past
      FORECAST_SOC_HYSTERESIS. Whole percent — inverter SOC registers are
      integers.
    * ``_forecast_charge_limiting`` — whether the charge-rate cap was engaged
      last cycle, which is what makes its SOC gate a two-threshold latch
      instead of one boundary the integer SOC can sit on and flap across (see
      ``recommended_charge_limit``). The engine owns the persistence; the
      calculation stays a pure function of state in, state out.
    """
    export_limit = (
        get_entry_value(hub_entry, CONF_GRID_EXPORT_LIMIT, DEFAULT_GRID_EXPORT_LIMIT)
        or 0
    )
    # Fleet capacity: the hub's legacy capacity field arrives via the implicit
    # legacy member, entry capacities via theirs.
    capacity_kwh = fleet.capacity_total(members)
    # Forecast sources are per inverter (each PV array belongs to one), but
    # clipping is a site question — every array competes for the same export
    # headroom — so the fleet's devices merge into one site forecast. The
    # hub's legacy fields arrive via the implicit legacy member.
    device_ids = fleet.forecast_device_ids(members)
    legacy_entity_ids = (
        get_entry_value(hub_entry, CONF_SOLAR_FORECAST_ENTITY_IDS, None) or []
    )
    if export_limit <= 0 or capacity_kwh <= 0 or not (device_ids or legacy_entity_ids):
        hub_runtime.pop("_forecast_max_soc", None)
        hub_runtime.pop("_forecast_charge_limiting", None)
        hub_runtime.pop("_forecast_parse_memo", None)
        return None, {}

    base_consumption = (
        get_entry_value(hub_entry, CONF_BASE_CONSUMPTION, DEFAULT_BASE_CONSUMPTION) or 0
    )
    soc_floor = get_entry_value(
        hub_entry, CONF_FORECAST_SOC_FLOOR, DEFAULT_FORECAST_SOC_FLOOR
    )
    # TWO thresholds, and they are deliberately different numbers.
    #
    # ``clip_threshold`` is the TRUE one — power the site can place without
    # curtailment — and it is what the forecast INTEGRAL must use: the energy
    # question ("how many kWh will this day produce above what we can place?")
    # is answered at the real export limit, or the reserved headroom would be
    # systematically too large.
    #
    # ``advice_threshold`` is the anchor for the INSTANTANEOUS charge-limit
    # advice, and it sits one Excess trigger margin lower. The reason is the
    # same one that already puts the Excess trigger below the limit: a signal
    # anchored exactly AT a hard limit can never be observed. An inverter that
    # hard-enforces the export limit curtails its own PV to hold it, so
    # measured production can never exceed
    #     export_limit + house + battery_allowance
    # and an advice anchored at the limit degenerates into
    #     battery_allowance + (house − base)
    # — a formula that reproduces its own previous output. The allowance then
    # freezes near its floor while real kilowatts are curtailed: the masking
    # hides the very overshoot signal the advice is computed from.
    #
    # Anchoring a margin below the limit breaks that fixed point without any
    # probe state, in two regimes:
    #  1. Unpinned: the battery absorbs production − house − (limit − margin),
    #     so export settles at (limit − margin) — comfortably under the hard
    #     limit, nothing is curtailed, measured production is honest and the
    #     plain formula tracks the sun.
    #  2. Pinned (export clamped at the limit, production masked): measured
    #     production is limit + house + allowance, so the next advice is
    #     allowance + margin + (house − base) — the allowance SELF-CREEPS by
    #     about one margin per cycle until export falls off the limit and
    #     regime 1 takes over. The escape from masking falls out of the
    #     arithmetic.
    excess_trigger_margin = (
        get_entry_value(
            hub_entry, CONF_EXCESS_TRIGGER_MARGIN, DEFAULT_EXCESS_TRIGGER_MARGIN
        )
        or 0
    )
    clip_threshold = export_limit + base_consumption
    advice_threshold = max(0.0, export_limit - excess_trigger_margin) + base_consumption

    # UNGATED fleet charge rate (see docstring) and total inverter capacity.
    fleet_charge_cap = sum(m.charge_cap or 0 for m in members) or None
    fleet_max_power, _, _ = fleet.inverter_limits(members)

    # Cap the summed series at what the site can physically produce — an
    # AC-coupled string inverter cannot deliver what Open-Meteo models from
    # kWp, and without the cap an oversized array over-reserves badly.
    power_cap = None
    if fleet_max_power:
        power_cap = fleet_max_power + (fleet_charge_cap or 0)

    entity_ids = configured_forecast_sensors(hass, device_ids, legacy_entity_ids)
    series = read_forecast_series(hass, entity_ids, hub_runtime)
    now, until = forecast_window()
    fc = clipping_forecast(
        series,
        clip_threshold,
        now,
        until,
        charge_cap_w=fleet_charge_cap,
        power_cap_w=power_cap,
    )

    max_soc = battery_max_soc(fc.absorbable_kwh, capacity_kwh, soc_floor)
    proposed = round(max_soc)
    prev = hub_runtime.get("_forecast_max_soc")
    if prev is not None and prev - FORECAST_SOC_HYSTERESIS <= proposed < prev:
        proposed = prev
    hub_runtime["_forecast_max_soc"] = proposed

    deficit = headroom_deficit_kwh(fc.absorbable_kwh, capacity_kwh, battery_soc)
    charge_limit = None
    if fleet_charge_cap:
        charge_limit, limiting = recommended_charge_limit(
            fc.absorbable_kwh,
            battery_soc,
            proposed,
            fleet_charge_cap,
            site.solar_production_total or 0,
            # The shifted anchor, never clip_threshold — see above.
            advice_threshold,
            FORECAST_SOC_HYSTERESIS,
            hub_runtime.get("_forecast_charge_limiting", False),
        )
        hub_runtime["_forecast_charge_limiting"] = limiting
    else:
        hub_runtime.pop("_forecast_charge_limiting", None)

    # Per-inverter advice: the uniform ceiling for every battery member, and
    # the fleet charge limit split proportionally to each member's charge cap,
    # clamped to its own cap.
    per_inverter = {}
    hub_id = getattr(hub_entry, "entry_id", None)
    for m in members:
        if m.entry_id == hub_id or not m.has_battery or not (m.capacity_kwh or 0) > 0:
            continue
        member_limit = None
        if charge_limit is not None and fleet_charge_cap and m.charge_cap:
            member_limit = round(
                min(m.charge_cap, charge_limit * m.charge_cap / fleet_charge_cap), 0
            )
        per_inverter[m.entry_id] = {
            "forecast_battery_max_soc": proposed,
            "forecast_charge_limit_w": member_limit,
        }

    _LOGGER.debug(
        "Forecast advice: clip %.2f kWh / storable %.2f kWh above %dW"
        " (advice anchored at %dW) to %s"
        " | max SOC %d%% (raw %.1f) deficit %.2f kWh charge cap %s",
        fc.clipped_kwh,
        fc.absorbable_kwh,
        clip_threshold,
        advice_threshold,
        until,
        proposed,
        max_soc,
        deficit,
        f"{charge_limit:.0f}W" if charge_limit is not None else "n/a",
    )

    return {
        "forecast_clipped_kwh": round(fc.clipped_kwh, 2),
        "forecast_absorbable_kwh": round(fc.absorbable_kwh, 2),
        "forecast_battery_max_soc": proposed,
        "forecast_headroom_deficit_kwh": round(deficit, 2),
        "forecast_charge_limit_w": (
            round(charge_limit, 0) if charge_limit is not None else None
        ),
    }, per_inverter


def _build_hub_result(
    site,
    raw_phases,
    voltage,
    main_breaker_rating,
    battery_soc,
    battery_soc_min,
    battery_max_discharge_power,
    battery_power,
    load_targets,
    load_available,
    load_names,
    auto_detect_notifications=None,
    group_data=None,
    grid_stale=False,
    grid_assumed=False,
    solar_assumed=False,
    hub_status="OK",
    hub_warnings=None,
    excess_available=False,
    excess_margin_power=0,
    forecast_advice=None,
    inverters_data=None,
):
    """Build the result dict returned by run_hub_calculation.

    ``grid_assumed`` says that at least one grid phase this cycle is the
    main-breaker worst case invented by ``_resolve_grid_phases`` (a CT
    unreadable with no EMA history — cold start, or the first cycles after an
    entry reload), not a reading and not a held EMA value. It splits the two
    kinds of published figure apart:

    * the grid MEASUREMENTS — ``grid_power``, ``total_export_power`` and the
      ``household_power`` derived from them — publish None, so their sensors
      read unknown and the recorder stores nothing. Publishing the assumption
      instead painted a fabricated grid spike (3 x breaker x voltage) onto
      Current Grid Power and into long-term statistics on every reload;
    * the computed ALLOCATIONS — every ``available_*`` / remaining figure and
      the per-load permits — keep publishing. The engine really did allocate
      on the worst case, so "no headroom" is the truthful consequence of the
      assumption, not a fabrication.

    None for the TOTALS even when only one phase is assumed: a total that
    contains one fabricated phase is itself fabricated, and there is no
    per-phase grid measurement published to partial it out into. A HELD EMA
    value is not covered — that is a legitimate estimate of what the phase was
    doing moments ago, and suppressing it would blank the grid sensors during
    every brief CT dropout.

    ``solar_assumed`` is the same split for solar (``fleet.solar_is_assumed``):
    a CONFIGURED production sensor that is unreadable with nothing to hold
    substitutes 0 W, which the calculation keeps — it is the conservative
    figure, and the household maths cannot take None — while ``solar_power``
    publishes None. A confident 0 W is right at night and a lie in daylight,
    and either way it lands in long-term statistics. ``household_power`` joins
    it ONLY when the household figure was itself computed from solar (the
    supply identity); the inverter-output form does not consume solar and
    stays. A site with NO production sensor configured is not affected at all:
    its solar is derived from the inverter output or grid export, and nothing
    there is invented. Per-inverter figures are handled one member at a time in
    hub_calculation.py, where each member has a published sensor of its own.
    """
    # Grid available power (based on consumption after feedback loop).
    # Off-grid there is no grid feed at all — headroom is 0 by definition.
    if site.is_off_grid:
        grid_headroom = 0.0
    else:
        grid_headroom = sum(
            max(0, main_breaker_rating - c) * voltage
            for c in (site.consumption.a, site.consumption.b, site.consumption.c)
            if c is not None
        )

    # Battery rated discharge power (gated by SOC >= minimum). This is the
    # battery's capability, not what is spare right now — see battery_remaining.
    #
    # Mirror the distribution engine's gate (_calculate_inverter_limit): in
    # derived-solar mode the engine can only add battery discharge to the pool
    # when a battery-power sensor is present (without it the battery's effect on
    # the grid CT can't be untangled, so the engine treats it as 0). The display
    # must use the same gate or these sensors would advertise battery headroom
    # the engine never actually grants — masking exactly the case where a large
    # load stays off despite a healthy SOC.
    battery_discharge_unusable = site.solar_is_derived and battery_power is None
    if (
        battery_soc is not None
        and battery_soc_min is not None
        and battery_soc >= battery_soc_min
        and battery_max_discharge_power
        and not battery_discharge_unusable
    ):
        battery_rated_discharge = round(float(battery_max_discharge_power), 0)
    else:
        battery_rated_discharge = 0

    # Total EVSE power = sum of actual load draws
    total_evse_power = round(
        sum(
            (c.l1_current + c.l2_current + c.l3_current) * voltage
            for c in site.loads
        ),
        0,
    )

    # Net site consumption
    net_consumption = sum(r for r in raw_phases if r is not None) * voltage

    # Unmanaged (household) draw, W. NOT household_consumption_total — that is
    # only the inverter-served share (solar + battery − export), which omits
    # everything the grid is serving and understated household by the full
    # grid import. The site-bus identity counts both supply paths:
    #   net grid + solar + battery discharge − managed draw
    # (battery power is positive when discharging, so the signed value also
    # handles charging; export shows up as negative net grid).
    #  1. Measured solar: the identity is exact.
    #  2. Derived solar with inverter output entities: use the engine's
    #     per-phase household (grid + inverter output − export per phase),
    #     since derived solar is itself built from these terms.
    #  3. Last resort: the identity with derived solar — best effort.
    hh_phases = getattr(site, "household_consumption", None)
    _identity_household = max(
        0,
        net_consumption
        + (site.solar_production_total or 0)
        + (battery_power or 0)
        - total_evse_power,
    )
    # ``household_from_solar`` records which of the three it was, because only
    # the two identity forms carry a fabricated solar figure into the household
    # result — form 2 is built from grid and inverter output alone.
    if not site.solar_is_derived and site.solar_production_total:
        household_power = round(_identity_household, 0)
        household_from_solar = True
    elif hh_phases is not None:
        household_power = round(
            sum(v for v in (hh_phases.a, hh_phases.b, hh_phases.c) if v is not None)
            * voltage,
            0,
        )
        household_from_solar = False
    else:
        household_power = round(_identity_household, 0)
        household_from_solar = True

    # Cap grid headroom by max grid import power limit (if configured)
    if site.max_grid_import_power is not None:
        post_feedback_import = sum(
            c * voltage
            for c in (site.consumption.a, site.consumption.b, site.consumption.c)
            if c is not None
        )
        grid_headroom = min(
            grid_headroom,
            max(0, site.max_grid_import_power - max(0, post_feedback_import)),
        )

    # Solar power available to loads = solar production - household loads
    # (household_consumption_total is set after feedback loop, so it excludes load draws)
    solar_available = 0
    if site.solar_production_total and site.solar_production_total > 0:
        household = getattr(site, "household_consumption_total", None)
        if household is not None:
            solar_available = max(0, site.solar_production_total - household)
        else:
            # Derived solar mode: export IS the solar available (best approximation)
            solar_available = max(0, site.solar_production_total)

    # Battery power still spare for managed loads = rated discharge minus the
    # discharge already serving the household.
    current_battery_discharge = max(0, battery_power or 0)
    battery_remaining = max(0, battery_rated_discharge - current_battery_discharge)

    # Site remaining power = grid import headroom + power the inverter can
    # still source from solar and battery for managed loads. On an off-grid
    # system grid_headroom is 0, so this is purely inverter-sourced; on a
    # grid-tied system it is the sum of both paths.
    #
    # Two ceilings apply, and we take the lower:
    #  - Source: solar surplus + spare battery discharge.
    #  - Inverter: rated capacity minus what the inverters are *already*
    #    outputting. That output is MEASURED when output entities exist and
    #    otherwise estimated topology-aware per fleet member — the old
    #    solar + battery_power form was the series (DC-coupled) model only, and
    #    on a parallel (AC-coupled) site it understated the output by the whole
    #    battery charge power, advertising headroom the site does not have.
    #
    #    The figure is site.inverter_output_total — captured at READ time,
    #    before the feedback loop, the same one the calculator's coverage gate
    #    consumes (#17). Recomputing it here from the post-feedback scalars
    #    inflated the estimate on a derived-solar site by the managed draws the
    #    feedback loop folds back into solar, understating Site Remaining Power
    #    by exactly the running loads' draw (issue #48).
    inverter_sourced = solar_available + battery_remaining
    if site.inverter_max_power:
        current_inverter_output = (
            site.inverter_output_total
            if site.inverter_output_total is not None
            else 0.0
        )
        # Headroom is clamped to the inverter's own rating: a negative measured
        # output (a cascaded inverter feeding power IN through the load port)
        # means the site is absorbing, but it does NOT raise this inverter's AC
        # output capability above its nameplate — so it cannot buy extra
        # headroom. Above the rating the headroom is 0, as before.
        inverter_headroom = max(
            0.0,
            min(
                float(site.inverter_max_power),
                site.inverter_max_power - current_inverter_output,
            ),
        )
        inverter_sourced = min(inverter_sourced, inverter_headroom)
        # Battery Remaining Power is likewise bounded by the inverter: the
        # battery cannot deliver more to loads than the inverter can pass.
        battery_remaining = min(battery_remaining, inverter_headroom)
    total_site_available = grid_headroom + inverter_sourced

    # Per-phase remaining current (A) = total remaining current on that phase,
    # i.e. grid + inverter. Each phase gets its share of grid headroom
    # (proportional to its raw breaker headroom, preserving asymmetric
    # loading) plus an equal share of inverter-sourced power. Summed across
    # the active phases this matches Site Remaining Power / voltage.
    #
    # A phase is gated on whether IT exists (consumption is not None), never on
    # its index versus the phase count: the site's phases need not be a prefix
    # of A/B/C — a B+C-only installation is explicitly supported. Indexing by
    # count would zero phase C and hand phase A (which does not exist) the
    # inverter share.
    phase_cons = (site.consumption.a, site.consumption.b, site.consumption.c)
    num_phases = site.num_phases or 1
    raw_phase_headroom = [
        max(0, main_breaker_rating - c) if c is not None else 0.0
        for c in phase_cons
    ]
    total_raw_headroom = sum(raw_phase_headroom)
    grid_current = grid_headroom / voltage if voltage else 0
    inverter_current_share = (
        inverter_sourced / voltage / num_phases if voltage else 0
    )
    available_per_phase = []
    for i, raw_hr in enumerate(raw_phase_headroom):
        if phase_cons[i] is None:
            available_per_phase.append(0)
            continue
        if total_raw_headroom > 0:
            grid_part = grid_current * (raw_hr / total_raw_headroom)
        else:
            grid_part = 0
        available_per_phase.append(round(grid_part + inverter_current_share, 1))

    # Per-pool remaining current (A) — the headroom each source still offers to
    # managed loads, broken out for diagnostics. grid + inverter is the total
    # remaining current available to loads. solar and battery are the two parts
    # that feed the inverter pool: the inverter figure is their sum capped by
    # the inverter's own rated headroom, so it can be smaller than solar +
    # battery when the inverter is the binding constraint. A managed load only
    # turns on if its minimum current fits within the inverter (off-grid) or
    # grid + inverter (grid-tied) figure — so a battery reading of ~0 here is
    # the usual reason a large load stays off despite a healthy SOC.
    grid_remaining_current = grid_headroom / voltage if voltage else 0
    solar_remaining_current = solar_available / voltage if voltage else 0
    battery_remaining_current = battery_remaining / voltage if voltage else 0
    inverter_remaining_current = inverter_sourced / voltage if voltage else 0

    # The grid measurements, or None while any phase is the breaker assumption
    # (see the docstring). Computed either way — the household identity above
    # needs the same terms — and dropped only at the point of publication.
    published_grid_power = None if grid_assumed else round(net_consumption, 0)
    published_export_power = (
        None if grid_assumed else round(site.total_export_power, 0)
    )
    # Same for solar, and for the household figure whenever it was derived FROM
    # solar (see the docstring and household_from_solar above).
    published_solar_power = (
        None if solar_assumed else round(site.solar_production_total or 0, 0)
    )
    published_household_power = (
        None
        if grid_assumed or (solar_assumed and household_from_solar)
        else household_power
    )

    # Build per-load operating modes dict
    load_modes = {c.load_id: c.operating_mode for c in site.loads}

    # Per-load effective priority rank — the order the engine serves loads
    # when power is contended: mode urgency first, then the configured priority
    # number (the same sort key _sort_loads uses to distribute power). Rank
    # 1 is served first. Exposed so each device can show where it really
    # stands, since mode urgency can override the configured priority number.
    _ranked = sorted(
        site.loads,
        key=lambda c: (c.mode_priority, c.priority),
    )
    load_rank = {c.load_id: idx + 1 for idx, c in enumerate(_ranked)}

    # Per-load actual draw — the measured current the load is really
    # pulling (sum of phase currents). For a binary load this is what the
    # device draws right now, which can be far below its reserved allocation
    # (e.g. a metered plug switched on but its appliance idle).
    load_draw = {
        c.load_id: round(c.l1_current + c.l2_current + c.l3_current, 1)
        for c in site.loads
    }

    # Per-load active phase count (for W-based OCPP profiles)
    # Uses actual draw to detect 1-phase car on 3-phase EVSE; falls back to configured phases.
    load_active_phases = {}
    load_phase_masks = {}
    for c in site.loads:
        active = sum(
            1 for cur in (c.l1_current, c.l2_current, c.l3_current) if cur > 1.0
        )
        load_active_phases[c.load_id] = active if active > 0 else c.phases
        # Live site-phase mask: which site phases A/B/C are actively drawing
        site_draw = c.get_site_phase_draw()
        load_phase_masks[c.load_id] = "".join(
            phase for phase, draw in zip(("A", "B", "C"), site_draw) if draw > 1.0
        )

    return {
        CONF_TOTAL_ALLOCATED_CURRENT: round(sum(load_targets.values()), 1),
        CONF_PHASES: site.num_phases,
        "calc_used": "calculate_all_load_targets",
        # Site-level data for hub sensor
        "battery_soc": site.battery_soc,
        "battery_soc_min": site.battery_soc_min,
        "battery_soc_target": site.battery_soc_target,
        "battery_power": battery_power,
        "available_current_a": available_per_phase[0],
        "available_current_b": available_per_phase[1],
        "available_current_c": available_per_phase[2],
        "available_grid_current": round(grid_remaining_current, 1),
        "available_solar_current": round(solar_remaining_current, 1),
        "available_battery_current": round(battery_remaining_current, 1),
        "available_inverter_current": round(inverter_remaining_current, 1),
        "total_site_available_power": round(total_site_available, 0),
        "grid_power": published_grid_power,
        "available_grid_power": round(grid_headroom, 0),
        "available_battery_power": battery_remaining,
        "total_evse_power": total_evse_power,
        "household_power": published_household_power,
        "solar_power": published_solar_power,
        "available_solar_power": round(solar_available, 0),
        "total_export_power": published_export_power,
        # The one Excess decision, computed by excess_margin() with the hysteresis
        # latch applied. Every Excess-mode load reads this rather than re-deriving
        # the rule — including the hot water tank, whose boost setpoint is
        # resolved in the HA layer. The margin is how many watts past (or short
        # of) the trigger the site is; the per-sink split is in the debug log.
        "excess_available": excess_available,
        "excess_margin_power": round(excess_margin_power, 0),
        # Per-load targets
        "load_targets": load_targets,
        "load_available": load_available,
        "load_names": load_names,
        "load_modes": load_modes,
        "load_rank": load_rank,
        "load_draw": load_draw,
        "load_active_phases": load_active_phases,
        "load_phase_masks": load_phase_masks,
        "distribution_mode": site.distribution_mode,
        # Auto-detection notifications (inversion, phase mapping)
        "auto_detect_notifications": auto_detect_notifications or [],
        # Circuit group data (for group sensors)
        "group_data": group_data or {},
        # Grid sensor health
        "grid_stale": grid_stale,
        # Hub status
        "hub_status": hub_status,
        "hub_warnings": hub_warnings or [],
        # Per-inverter-entry data (for the inverter devices' own sensors)
        "inverters": inverters_data or {},
        # Advisory battery headroom from the PV clipping forecast. Keys are
        # present only while the feature is configured — the matching sensors
        # are gated the same way.
        **(forecast_advice or {}),
    }
