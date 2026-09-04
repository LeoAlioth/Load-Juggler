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
import time

from .forecast_observers import (
    observe_clipped,
    observe_gain,
    observe_peakiness,
)
from ..calculations.calibration import block_power_at
from ..calculations import (
    merge_forecast_series,
    select_clipping_window,
    first_production_at,
    reservation_is_due,
    battery_max_soc,
    excess_load_draw_power,
    headroom_deficit_kwh,
    reconstructed_export_power,
    recommended_charge_limit,
    yields_to_excess,
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
    DEFAULT_SOC_LIMIT_NORMAL,
    FORECAST_SOC_HYSTERESIS,
)
from ..helpers import get_entry_value
from . import fleet
from .forecast_reader import (
    read_forecast_series_pair,
    forecast_windows,
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
    excess_on=False,
    ctrl_site=None,
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
    ``Σ cap_i × (target_i − s)/100`` — precisely what battery_max_soc computes
    from the summed capacity and the capacity-weighted destination
    (``fleet.soc_target_weighted``) — so every battery is advised the same
    percent, splitting the headroom proportionally to capacity by construction.
    The recommended charge limit is divided by the room each pack still has
    under that ceiling, water-filled against each member's own charge cap
    (``fleet.split_charge_limit``), so the packs arrive at the ceiling together.

    The reserve is carved below where the batteries are HEADING rather than
    below 100 %: each member's "normal SOC ceiling source" entity is its
    destination, and a member without one is heading for 100 (so a site that
    configures none behaves exactly as it did before). Nothing downstream needs
    to change for it — the write-control fan-out already writes
    ``min(normal, recommendation)``, and the recommendation is now at or below
    the destination by construction. The two ways it can sit ABOVE a member's
    own normal are both safe there: a floor higher than the destination
    (``CONF_FORECAST_SOC_FLOOR``), and the ratchet holding a recommendation
    from before the user lowered their ceiling by less than
    FORECAST_SOC_HYSTERESIS. The min() writes the user's number in both cases.
    A larger mid-day change to the destination is not resisted at all: the
    ratchet only absorbs falls smaller than its band, so lowering the ceiling by
    more than that lowers the recommendation on the very next cycle, and raising
    it lets the recommendation rise freely.

    The window the integral runs over is the NEXT clipping window, not the rest
    of the calendar day (``select_clipping_window``). While today still has clip
    left that is the remainder of today and every published figure is exactly
    what it always was. Once today's clip has integrated away, the window
    becomes tomorrow's — and one day is as far as the search ever looks
    (``FORECAST_LOOKAHEAD_DAYS``), so with no clip today and none tomorrow the
    recommendation rests at the destination. The clippable / storable / deficit
    figures follow that same window, so from the evening onward they describe
    TOMORROW's peak: "how much the site will fail to place at the next peak, and
    how much of that the battery can still make room for".

    A reservation for a window that is still a night away is not APPLIED the
    moment today's clip runs out. The published ceiling becomes a discharge
    floor once the SOC write-control fans it out, so dropping it at dusk parks
    the pack at the reserve by early evening and the house runs on the grid
    until dawn; the battery only has to be down there by the time production
    starts. So the advice holds at the destination and drops just in time,
    scheduled against ``first_production_at`` with base consumption as the
    discharge-rate estimator (``reservation_is_due`` for the rule, the latch and
    the degraded modes). The ratchet does not fight the handover: the hold is a
    RISE to the destination, which is never resisted, and the drop is normally
    many points, so it clears the FORECAST_SOC_HYSTERESIS band in one cycle.

    The RESERVATION half of the charge-rate advice deliberately does NOT follow
    the window — it is handed ``absorbable_today``, which is zero the moment the
    reservation moves to tomorrow. That half exists to stop the battery eating
    headroom out from under a clip that is happening; a clip a night away is
    answered by the SOC ceiling instead, and leaving it released overnight is
    what keeps a clip in the dark from writing to a charge register.

    The DESTINATION half does not ask about the window at all: a battery at or
    above where its owner sends it is held there whatever the forecast says, and
    the room above the destination stays the buffer for a day the forecast
    under-read (see ``recommended_charge_limit``). It costs no register traffic
    to speak of — with no overshoot the advice is a single flat value, so the
    control writes the floor once and the deadband swallows every cycle after
    it — and a pack that spends the evening serving the house falls a
    hysteresis band below the destination and releases on its own.

    The energy question and the power question are asked in DIFFERENT terms.
    The integral is an energy question about a whole day, asked at the true
    clipping threshold (``export limit + base consumption``). The instantaneous
    charge-limit advice is a power question about this cycle, and it is
    MEMORYLESS DIRECT FEEDBACK on two live figures — what the fleet battery is
    absorbing and what the meter is exporting — against an export SETPOINT one
    Excess trigger margin under the limit. It never consults base consumption at
    all: base is a guess about the house, and a guess in the instantaneous path
    was what the deleted integral trim existed to correct. See
    ``recommended_charge_limit`` for the derivation, including why a
    hard-limiting inverter cannot mask this form either.

    The fleet-level carried state, all of it in ``hub_runtime`` and none of it
    ever per-member (the advice is uniform by construction, so per-member state
    would diverge):

    * ``_forecast_max_soc`` — the published ceiling's ratchet, mirroring the
      Excess latch: it rises freely and falls only past
      FORECAST_SOC_HYSTERESIS. Whole percent — inverter SOC registers are
      integers.
    * ``_forecast_reservation_due`` — whether the next window's reservation has
      already been applied for this night, so a pack discharging faster than
      base consumption cannot make the advice climb back to the destination in
      the dark (``reservation_is_due``). Self-clearing: nothing is latched while
      production is under way, so every night starts from a clean state.
    * ``_forecast_charge_limiting`` — whether the charge-rate cap was engaged
      last cycle, which is what makes its RESERVATION gate a two-threshold latch
      instead of one boundary the integer SOC can sit on and flap across (see
      ``recommended_charge_limit``). One state for both engagement sources, the
      destination hold included: at the destination the reserved ceiling is at or
      below the SOC anyway, so the two always agree, and carrying the hold in it
      is what lets a clip appearing while the pack is parked take over without a
      step. The engine owns the persistence; the calculation stays a pure
      function of state in, state out.
    * ``_forecast_soc_yielding`` — whether the battery was already at or above
      its destination, the latch of the same shape at that crossing
      (``yields_to_excess``). It decides two things at once: that the Excess
      loads are served first (``excess_draw_w``) and that the destination is held
      as a standing ceiling (``at_destination``).
    Every one of them is dropped the moment the feature is not configured, so a
    site that turns it off leaves nothing behind. There is deliberately no state
    for the engaged VALUE any more: it is recomputed from this cycle's
    measurements, so a reload, a cloud or an hour of release leaves nothing
    stale to carry back in.
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
        hub_runtime.pop("_forecast_reservation_due", None)
        hub_runtime.pop("_forecast_charge_limiting", None)
        hub_runtime.pop("_forecast_soc_yielding", None)
        hub_runtime.pop("_forecast_parse_memo", None)
        return None, {}

    base_consumption = (
        get_entry_value(hub_entry, CONF_BASE_CONSUMPTION, DEFAULT_BASE_CONSUMPTION) or 0
    )
    soc_floor = get_entry_value(
        hub_entry, CONF_FORECAST_SOC_FLOOR, DEFAULT_FORECAST_SOC_FLOOR
    )
    # TWO numbers, in two different currencies, and deliberately so.
    #
    # ``clip_threshold`` is POWER THE SITE CAN PLACE — export limit plus the
    # house — and it is what the forecast INTEGRAL must use: the energy question
    # ("how many kWh will this day produce above what we can place?") is
    # answered at the real export limit, or the reserved headroom would be
    # systematically too large. base_consumption belongs here, where it is a
    # day-scale average of a real quantity.
    #
    # ``export_setpoint`` is WATTS AT THE METER — where the instantaneous
    # charge-limit advice steers export to — and it carries no house term at
    # all: the advice measures what the house is doing this cycle instead of
    # assuming it (see ``recommended_charge_limit``). It sits one Excess trigger
    # margin under the limit for the same reason the Excess trigger does: a
    # signal anchored exactly AT a hard limit can never be observed. An inverter
    # that hard-enforces the export limit curtails its own PV to hold it, so
    # with export pinned at the wall the feedback value is
    #     battery + (limit − setpoint) = battery + margin
    # and the permit SELF-CREEPS by one margin per cycle until export falls off
    # the limit; from there the plain feedback tracks the sun and export settles
    # a margin under the limit, where nothing is curtailed and every reading is
    # honest.
    excess_trigger_margin = (
        get_entry_value(
            hub_entry, CONF_EXCESS_TRIGGER_MARGIN, DEFAULT_EXCESS_TRIGGER_MARGIN
        )
        or 0
    )
    clip_threshold = export_limit + base_consumption
    export_setpoint = max(0.0, export_limit - excess_trigger_margin)

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
    # Per-array optimism, resolved device → entity so the factor follows the
    # array rather than the site (fleet.forecast_inflation_by_device). Only the
    # CLIP series is inflated; ``series`` below stays raw for the overnight
    # drop's deadline, which carries its own early bias already.
    inflation_by_device = fleet.forecast_inflation_by_device(members)
    inflation_by_entity = {}
    for device_id, pct in inflation_by_device.items():
        for entity_id in configured_forecast_sensors(hass, [device_id], None):
            inflation_by_entity.setdefault(entity_id, pct)
    series, clip_series, by_entity = read_forecast_series_pair(
        hass, entity_ids, hub_runtime, inflation_by_entity
    )
    # The NEXT clipping window, not the rest of the calendar day: the remainder
    # of today while today still has clip left, tomorrow once it does not (see
    # ``select_clipping_window``). ``window`` is 0 exactly in the first case.
    windows = forecast_windows()
    window, fc = select_clipping_window(
        clip_series,
        clip_threshold,
        windows,
        charge_cap_w=fleet_charge_cap,
        power_cap_w=power_cap,
    )
    until = windows[window][1]
    # The charge-rate advice protects headroom that is at risk NOW, so it asks
    # only about today: a clip that is a night away is the SOC ceiling's problem,
    # and the ceiling is what makes room for it. Identical to ``fc`` whenever the
    # chosen window IS today; zero once the reservation has moved to tomorrow,
    # which keeps the cap released and the register untouched all night —
    # exactly as it was before the window could ever move.
    absorbable_today = fc.absorbable_kwh if window == 0 else 0.0

    # Where the fleet's batteries are HEADING (their own normal-ceiling sources,
    # capacity-weighted; 100 % for every member that configures none). The
    # reserve is carved below this, not below 100 — see
    # ``fleet.soc_target_weighted`` for the weighting's derivation and
    # ``battery_max_soc`` for why the destination is the right anchor.
    soc_target = fleet.soc_target_weighted(members, DEFAULT_SOC_LIMIT_NORMAL)
    reserved_soc = battery_max_soc(
        fc.absorbable_kwh, capacity_kwh, soc_floor, soc_target=soc_target
    )
    # Just-in-time: the reserve has to be in place by the time production
    # starts, not the moment the clip is computed. Until then the advice rests
    # at the destination, so the evening's house draw comes out of the battery
    # instead of the grid. Every term is recomputed from live state each cycle —
    # the only thing carried is the latch that keeps a drop dropped. See
    # ``reservation_is_due``.
    production_at = first_production_at(
        series, base_consumption, windows[0][0], windows[-1][1], power_cap_w=power_cap
    )
    due, due_latched = reservation_is_due(
        windows[0][0],
        production_at,
        battery_soc,
        reserved_soc,
        capacity_kwh,
        base_consumption,
        hub_runtime.get("_forecast_reservation_due", False),
    )
    hub_runtime["_forecast_reservation_due"] = due_latched
    # Holding means the destination itself — battery_max_soc with nothing to
    # absorb — so the floor and its clamps stay exactly where they always were.
    max_soc = (
        reserved_soc
        if due
        else battery_max_soc(0.0, capacity_kwh, soc_floor, soc_target=soc_target)
    )
    proposed = round(max_soc)
    prev = hub_runtime.get("_forecast_max_soc")
    if prev is not None and prev - FORECAST_SOC_HYSTERESIS <= proposed < prev:
        proposed = prev
    hub_runtime["_forecast_max_soc"] = proposed

    deficit = headroom_deficit_kwh(fc.absorbable_kwh, capacity_kwh, battery_soc)
    # The two live plant figures the engaged advice is computed from, read once
    # — from the CHARGE-CONTROL VIEW of the site when the engine supplies one:
    # the same loads, allowance and feedback subtraction, but grid phases and
    # battery power through the directional smoothers (engine/readers), so a
    # lensing peak reaches the register in two cycles instead of being averaged
    # away while the Excess verdict keeps reading the symmetric site. Falling
    # back to the site itself keeps every pure rig and scenario byte-identical.
    view = ctrl_site or site
    battery_charge_w = -(view.battery_power or 0.0)
    export_now_w = reconstructed_export_power(view)
    charge_limit = None
    limiting = False
    if fleet_charge_cap:
        # Above the destination the battery is the absorber of LAST RESORT: the
        # Excess loads that exist to soak up surplus get it first, and the
        # battery takes what they cannot. Below it the battery comes first, as
        # ever. Latched at the crossing (see ``yields_to_excess``) because this
        # moves the advice by whole kilowatts and an integer SOC register would
        # otherwise sit on the boundary and flip it.
        #
        # The same crossing is also the STANDING CEILING: at or above the
        # destination the charge cap engages whether or not anything is forecast
        # to clip, because the room above the destination is the buffer for a day
        # the forecast under-read and not the forecast's to spend (see
        # ``recommended_charge_limit`` — that gate is tested before the clip).
        yielding = yields_to_excess(
            battery_soc,
            soc_target,
            FORECAST_SOC_HYSTERESIS,
            hub_runtime.get("_forecast_soc_yielding", False),
        )
        hub_runtime["_forecast_soc_yielding"] = yielding
        # The engaged value's two live inputs, both from THIS cycle:
        #
        # * what the fleet battery is absorbing, positive charging —
        #   ``site.battery_power`` is positive DISCHARGING (see
        #   ``fleet.battery_power_total``), so it is negated here and a pack that
        #   is giving power back arrives as a negative term, which the pure
        #   function's clamp at 0 turns into "a charge cap cannot force a
        #   discharge". No sensor at all reads 0, the conservative degradation.
        # * RECONSTRUCTED export — the draws-credited-back figure the Excess
        #   verdict decides on, and the reason this loop is safe to close: an
        #   engaged Excess load's kilowatts are not read as an export shortfall,
        #   so our own loads cannot steer the battery's limit (they are
        #   subtracted deliberately instead, as ``excess_draw_w``).
        charge_limit, limiting = recommended_charge_limit(
            absorbable_today,
            battery_soc,
            proposed,
            fleet_charge_cap,
            battery_charge_w,
            export_now_w,
            export_setpoint,
            FORECAST_SOC_HYSTERESIS,
            hub_runtime.get("_forecast_charge_limiting", False),
            excess_draw_w=excess_load_draw_power(site) if yielding else 0.0,
            at_destination=yielding,
        )
        hub_runtime["_forecast_charge_limiting"] = limiting
    else:
        hub_runtime.pop("_forecast_charge_limiting", None)
        hub_runtime.pop("_forecast_soc_yielding", None)

    # Per-inverter advice: the uniform ceiling for every battery member, and
    # the fleet charge limit divided by the room each pack still has under
    # that ceiling, clamped to its own cap (fleet.split_charge_limit).
    per_inverter = {}
    hub_id = getattr(hub_entry, "entry_id", None)
    charge_shares = fleet.split_charge_limit(members, charge_limit, proposed)
    for m in members:
        if m.entry_id == hub_id or not m.has_battery or not (m.capacity_kwh or 0) > 0:
            continue
        member_limit = None
        if m.entry_id in charge_shares:
            member_limit = round(charge_shares[m.entry_id], 0)
        per_inverter[m.entry_id] = {
            "forecast_battery_max_soc": proposed,
            "forecast_charge_limit_w": member_limit,
            # The GATE, not the value: the charge control needs to know a
            # protective regime transition (the cap engaging) from a
            # steady-state correction, because only the latter is paced by the
            # persistence window (see ``control/inverter.py``). Fleet-wide by
            # construction — one latch decides for every member.
            "forecast_charge_limiting": bool(limiting),
        }

    # --- Observers (measure only; the advice above is untouched) -----------
    #
    # Two forecast errors, watched separately because neither correction fixes
    # the other: this inverter's LEVEL bias (actual ÷ forecast energy) and the
    # site's PEAKINESS (how much a 15-minute average understates the clip).
    # Both publish what they would have corrected and correct nothing — a
    # season of evidence decides whether either is worth applying.
    #
    # dt comes off the monotonic clock and is capped: after a stall or a
    # suspend, one cycle must not book an hour of made-up energy.
    now_mono = time.monotonic()
    last_mono = hub_runtime.get("_forecast_obs_mono")
    hub_runtime["_forecast_obs_mono"] = now_mono
    dt_hours = 0.0
    if last_mono is not None:
        dt_hours = max(0.0, min(now_mono - last_mono, 60.0)) / 3600.0

    now_local = windows[0][0]
    local_day = now_local.date()
    # Curtailing is the one regime the gain must not learn from: while the
    # inverter throttles its own array, measured production is suppressed by the
    # very thing being forecast. Excluded per INTERVAL, so a clipping day still
    # contributes its honest morning and evening (calibration.note_gain_sample).
    #
    # The verdict is the EXCESS one, the same test the clipped-energy observer
    # below uses — not "export is above the setpoint". The setpoint sits one
    # trigger margin BELOW the real limit and driving export onto it is exactly
    # what the charge control exists to do, so testing against it marked the
    # controller's own operating point as curtailment: on a live site the gain
    # observer skipped nearly every productive interval, never reached its
    # minimum informative energy, and published Unknown all day (2026-08-31).
    constrained = bool(excess_on)

    for m in members:
        if not m.forecast_device_ids:
            continue
        member_entities = configured_forecast_sensors(
            hass, list(m.forecast_device_ids), None
        )
        member_series = merge_forecast_series(
            [by_entity[e] for e in member_entities if e in by_entity]
        )
        observed = observe_gain(
            hub_runtime,
            m.entry_id,
            local_day,
            block_power_at(member_series, now_local),
            fleet.member_solar_production(m, site.voltage),
            dt_hours,
            constrained,
            now_local=now_local,
        )
        per_inverter.setdefault(m.entry_id, {}).update(observed)

    peakiness = observe_peakiness(
        hub_runtime,
        now_local,
        local_day,
        clip_threshold,
        fleet.solar_total(members, site.voltage),
        dt_hours,
    )
    # SATURATION is the Excess verdict, not a second test of the same thing:
    # "the export allowance is used up AND the battery is taking all it can" is
    # precisely what that margin already decides, hysteresis and all. Reusing it
    # means the clipped figure can never disagree with the verdict the rest of
    # the integration acts on.
    clipped = observe_clipped(
        hub_runtime,
        local_day,
        block_power_at(series, now_local),
        fleet.solar_total(members, site.voltage),
        excess_on,
        dt_hours,
    )

    _LOGGER.debug(
        "Forecast advice: clip %.2f kWh / storable %.2f kWh above %dW"
        " (export setpoint %dW) in window +%dd to %s"
        " | max SOC %d%% (raw %.1f of destination %.1f%%) deficit %.2f kWh"
        " | reserve %s (reserved %.1f%%, production from %s)"
        " charge cap %s (battery %+.0fW, export %.0fW)",
        fc.clipped_kwh,
        fc.absorbable_kwh,
        clip_threshold,
        export_setpoint,
        window,
        until,
        proposed,
        max_soc,
        soc_target,
        deficit,
        "due" if due else "held at the destination",
        reserved_soc,
        production_at,
        f"{charge_limit:.0f}W" if charge_limit is not None else "n/a",
        battery_charge_w,
        export_now_w,
    )

    return {
        **peakiness,
        **clipped,
        # Which window the figures below describe: today's remaining peak, or
        # tomorrow's once today's clip has integrated away (display only).
        "forecast_window_tomorrow": bool(window),
        "forecast_clipped_kwh": round(fc.clipped_kwh, 2),
        "forecast_absorbable_kwh": round(fc.absorbable_kwh, 2),
        "forecast_battery_max_soc": proposed,
        "forecast_headroom_deficit_kwh": round(deficit, 2),
        "forecast_charge_limit_w": (
            round(charge_limit, 0) if charge_limit is not None else None
        ),
    }, per_inverter


def _draw_is_unknown(load, booked):
    """Whether this load's published draw would be a fabrication.

    Two conditions, and both are needed:

    * its monitor produced no reading (``draw_assumed`` — configured, but
      unreadable with nothing held), and
    * we have reason to believe it could be drawing: the engine booked
      footprint for it this cycle (``load_targets``, which for an unmetered
      EVSE is its whole permit), or it reports itself active — a car connected,
      a thermostat calling for heat, a switch that is on.

    The second half is what keeps this from blanking Current Managed Power
    across a whole site because one idle charger is offline — by far the common
    case, since an offline OCPP charger takes every one of its sensors with it.
    For a load the engine booked nothing for and that says it is not running,
    0 W is not a guess: our own allocation and its own status are facts we hold
    without any meter. Once either says otherwise, the 0 is an invention about a
    load that may be pulling kilowatts, and no honest total can contain it.

    The booked figure is deliberately ``load_targets`` and not
    ``load_available``: an inactive load still gets an available current (so the
    HA layer can switch it back on), so the permit alone would call every
    offline charger engaged — the exact false positive this rule exists to
    avoid.
    """
    return bool(load.draw_assumed) and ((booked or 0) > 0 or not load.reports_idle)


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

    The third case is the managed draws (``LoadContext.draw_assumed``, resolved
    per load by ``_draw_is_unknown`` below): an unreadable current or power
    monitor leaves its load carrying 0 A, so a charging car could publish 0 W
    of ``total_evse_power``. The internal 0 stays — it is the conservative
    figure for the feedback loop, which subtracts managed draws from the grid
    CTs — while ``total_evse_power``, that load's ``load_draw`` entry and
    ``household_power`` publish None. Household joins them because EVERY form
    of it nets the managed draw out (the identity subtracts it; the per-phase
    form is built on post-feedback consumption), so a fabricated 0 leaves the
    car's kilowatts sitting inside the household figure. ``total_export_power``
    does NOT: with the draw at 0 the feedback loop subtracts nothing, so the
    published export degrades to the CT's own reading rather than to an
    invented number.
    """
    # Which loads carry an invented 0 draw this cycle (see _draw_is_unknown).
    # Resolved once, here, because both the per-load figure and the total need
    # the same answer, and it needs this cycle's permits.
    draw_unknown = {
        c.load_id: _draw_is_unknown(c, load_targets.get(c.load_id))
        for c in site.loads
    }
    managed_draw_assumed = any(draw_unknown.values())

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
    # Raw export with the managed draws added back, per phase (an importing
    # phase adds no export — the same clamp the engine's reconstruction uses).
    _draws = [0.0, 0.0, 0.0]
    for c in site.loads:
        for i, d in enumerate(c.get_site_phase_draw()):
            _draws[i] += d
    raw_export_with_loads_off = (
        sum(
            max(0.0, -(r or 0.0) + d)
            for r, d in zip(raw_phases, _draws)
            if r is not None
        )
        * voltage
    )

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
        if grid_assumed
        or (solar_assumed and household_from_solar)
        or managed_draw_assumed
        else household_power
    )
    # The managed draw: the total while every engaged load's contribution is a
    # reading, and each load's own figure the same way. Per load because each
    # has a published figure of its own, exactly like the per-inverter solar.
    published_evse_power = None if managed_draw_assumed else total_evse_power

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
        c.load_id: (
            None
            if draw_unknown[c.load_id]
            else round(c.l1_current + c.l2_current + c.l3_current, 1)
        )
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
        # The reconstructed export on the RAW meter basis: this cycle's meter
        # export plus the managed draws — what the site would export with our
        # loads off, on the same reading ``grid_power`` shows. (The engine's
        # own figure, ``total_export_power``, is the smoothed one.)
        "total_export_power_raw": (
            None if grid_assumed else round(raw_export_with_loads_off, 0)
        ),
        "available_grid_power": round(grid_headroom, 0),
        "available_battery_power": battery_remaining,
        "total_evse_power": published_evse_power,
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
