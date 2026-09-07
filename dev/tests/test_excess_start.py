"""The Excess start edge — what a modulating Excess load is granted.

Machine-authored tests — not yet human-reviewed.

The rule: *the verdict starts the FIRST load, the pool only sizes it.* A
modulating load (an EVSE) cannot run below its minimum current, so while Excess
is engaged its minimum IS the floor — held there while the momentary pool is
smaller than it, followed upward once the pool exceeds it. That is the start
edge the binary Excess loads (plug, tank boost) have always had: they engage on
threshold-hit even though their whole rating overshoots the pool.

Behind the first consumer, Excess loads start IN RANK ORDER: each one only
while the surplus left after the higher-ranked loads' claims (their permits,
not their not-yet-measured draws) is still positive. It need not cover the
load's own minimum — 500 W left after a 2.1 kW tank still starts a 1.4 kW EVSE
at its floor — but nothing left means it waits, and a running one yields. Two
2 kW steps no longer engage together on a 300 W surplus and flap (the EcoFlow +
boiler site, 2026-09-03).

Gating the start on the pool instead leaves a modulating load at 0 forever on
the site the pool is smallest at: with our charge control tracking the export
overshoot the standing margin sits AT the trigger — a pool of 0 amps, which is
Excess by definition (nothing more can be absorbed) — peaking only between
register writes.

The floor is a floor on the *Excess allocation*, not a licence to overrun
physical limits: the wire, the phase and a circuit-group breaker still stop a
load that cannot be given its minimum. The last three tests are that boundary.

Release is not this file's subject — the latch's hysteresis on the reconstructed
margin owns it (see test_excess_stayon.py).

Pure Python, no Home Assistant dependencies. Runnable two ways:
  python3 dev/tests/test_excess_start.py     (standalone, no pytest needed)
  pytest dev/tests/test_excess_start.py      (Docker / CI tier)
"""

import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Module loading — shared stub loader (avoids the HA-importing package root)
# ---------------------------------------------------------------------------
sys.path.insert(0, str(Path(__file__).resolve().parent))
from standalone_loader import load_pure_modules

load_pure_modules()

from custom_components.dynamic_ocpp_evse.calculations.models import (  # noqa: E402
    CircuitGroup,
    LoadContext,
    PhaseConstraints,
    PhaseValues,
    SiteContext,
)
from custom_components.dynamic_ocpp_evse.calculations.target_calculator import (  # noqa: E402
    calculate_all_load_targets,
    excess_margin,
)
from custom_components.dynamic_ocpp_evse.calculations.utils import (  # noqa: E402
    grid_without_managed_draws,
)

V = 230.0
# Export allowance — the trigger for these sites. A whole number of amps
# (15 A × 230 V) on purpose: the sites below are built to sit EXACTLY on it,
# and a threshold that is not representable would land the margin a rounding
# error either side of the verdict.
THRESHOLD = 3450.0
BREAKER = 25.0       # main breaker rating (A), plenty for one 16 A EVSE


def _evse(eid="evse", min_current=6.0, max_current=16.0, priority=1,
          phase="A", draw=0.0):
    """A modulating Excess-mode EVSE on one site phase."""
    return LoadContext(
        load_id=eid,
        entity_id=eid,
        min_current=min_current,
        max_current=max_current,
        phases=1,
        priority=priority,
        device_type="evse",
        operating_mode="Excess",
        mode_behavior="excess",
        mode_priority=4,
        active_phases_mask=phase,
        l1_phase=phase,
        l1_current=draw,
        connector_status="Charging",
    )


def _site(export_w, loads=(), breaker=BREAKER, threshold=THRESHOLD,
          groups=()):
    """A single-phase, batteryless site exporting ``export_w`` watts.

    No battery means the whole verdict rides on the export term:
    ``margin = export − threshold``. The readings are the PHYSICAL ones — what
    the CT shows with ``loads`` running — so a running load's draw is already
    missing from the export; ``_prepare`` puts it back the way the engine does.
    """
    export_a = export_w / V
    return SiteContext(
        voltage=V,
        main_breaker_rating=breaker,
        consumption=PhaseValues(0.0, None, None),
        export_current=PhaseValues(export_a, None, None),
        grid_current=PhaseValues(-export_a, None, None),
        excess_export_threshold=threshold,
        battery_soc=None,
        battery_power=None,
        battery_max_charge_power=None,
        battery_max_discharge_power=None,
        loads=list(loads),
        circuit_groups=list(groups),
    )


def _prepare(site):
    """Run the engine's pre-calculation steps, then the calculator.

    Mirrors run_hub_calculation's order: the feedback loop takes the managed
    draws off the grid readings (``grid_without_managed_draws``, its pure core),
    the latch settles ``site.excess_hysteresis`` — 0 here, since none of these
    sites needs the release band — and the calculator runs last. Returns the
    margin the calculator saw.
    """
    draws = [0.0, 0.0, 0.0]
    for load in site.loads:
        a, b, c = load.get_site_phase_draw()
        draws[0] += a
        draws[1] += b
        draws[2] += c
    if any(d > 0 for d in draws):
        site.consumption, site.export_current = grid_without_managed_draws(
            site.consumption, site.export_current, tuple(draws)
        )
    margin = excess_margin(site, site.excess_hysteresis)
    calculate_all_load_targets(site)
    return margin


def _close(a, b, tol=0.05):
    return abs(a - b) < tol


# ---------------------------------------------------------------------------
# The start edge
# ---------------------------------------------------------------------------

def test_the_verdict_with_no_pool_at_all_starts_the_load_at_its_minimum():
    """The field case. Export sits exactly ON the allowance: the site cannot
    place another watt — Excess by definition — and the pool that describes it
    is 0 A. The load starts at its minimum anyway, exactly as a plug would."""
    load = _evse()
    margin = _prepare(_site(THRESHOLD, loads=[load]))
    assert _close(margin, 0.0)
    assert _close(load.allocated_current, 6.0)
    assert _close(load.available_current, 6.0)


def test_a_pool_wider_than_the_minimum_is_followed_upward():
    """Above the minimum nothing changed: the allocation is the pool's, not the
    floor's. 2.8 kW over the allowance on one phase is 12.2 A."""
    load = _evse()
    margin = _prepare(_site(THRESHOLD + 2800, loads=[load]))
    assert _close(margin, 2800.0)
    assert _close(load.allocated_current, 12.2)


def test_a_pool_wider_than_the_load_is_capped_by_its_maximum():
    """The load's own maximum is still the ceiling."""
    load = _evse()
    _prepare(_site(THRESHOLD + 8000, loads=[load]))
    assert _close(load.allocated_current, 16.0)


def test_the_verdict_off_allocates_nothing():
    """One watt short of the allowance is not Excess, and the floor is not a
    licence to start without the verdict."""
    load = _evse()
    margin = _prepare(_site(THRESHOLD - 1000, loads=[load]))
    assert margin < 0
    assert _close(load.allocated_current, 0.0)
    assert _close(load.available_current, 0.0)


def test_a_running_load_rides_a_pool_dip_at_its_minimum():
    """The load is already drawing its 6 A and the CT shows 1380 W less export
    for it (9 A instead of 15 A). The reconstruction puts the draw back — export
    reads the allowance, the verdict holds, the pool is 0 — and the load rides
    the dip at its minimum rather than being dropped and restarted."""
    load = _evse(draw=6.0)
    margin = _prepare(_site(THRESHOLD - 6.0 * V, loads=[load]))
    assert _close(margin, 0.0)
    assert _close(load.allocated_current, 6.0)


def test_the_floor_is_each_load_s_own_minimum():
    """Not a constant: a 10 A load floors at 10 A, a 6 A one at 6 A. 7 A of
    pool: the first claims its 6 A floor, 1 A is left, so the second starts —
    at ITS floor of 10 A, which the leftover need not cover."""
    small = _evse("small", min_current=6.0, priority=1)
    large = _evse("large", min_current=10.0, priority=2)
    _prepare(_site(THRESHOLD + 7.0 * V, loads=[small, large]))
    assert _close(small.allocated_current, 6.0)
    assert _close(large.allocated_current, 10.0)


def test_two_excess_loads_on_an_empty_pool_only_the_first_starts():
    """The verdict starts the first consumer on a pool of 0 (the saturated
    site). The second sees nothing left after the first one's 6 A claim and
    waits — it no longer rides the same verdict onto a surplus that cannot
    feed it."""
    first = _evse("first", priority=1)
    second = _evse("second", priority=2)
    _prepare(_site(THRESHOLD, loads=[first, second]))
    assert _close(first.allocated_current, 6.0)
    assert _close(second.allocated_current, 0.0)


def _tank(eid="tank", watts=2100.0, priority=2, heating=True, phase="A"):
    """A 2.1 kW binary tank (Freeze Protection: full-power behavior, tier 1)
    that is calling for heat — its draw is its rating while heating and 0 the
    cycle it has only just been permitted."""
    amps = watts / V
    return LoadContext(
        load_id=eid,
        entity_id=eid,
        min_current=amps,
        max_current=amps,
        phases=1,
        priority=priority,
        device_type="hot_water_tank",
        operating_mode="Freeze Protection",
        mode_behavior="full_power",
        mode_priority=1,
        active_phases_mask=phase,
        l1_phase=phase,
        l1_current=amps if heating else 0.0,
        rated_current=amps,
    )


def test_a_lower_ranked_evse_starts_on_what_the_tank_leaves():
    """Anže's example: a 2.5 kW surplus, a 2.1 kW tank ahead of a 1.4 kW-minimum
    EVSE. The tank takes its 2.1 kW, 400 W are left, and the EVSE still starts at
    its 6 A floor — the leftover is positive, that is all the rule asks."""
    tank = _tank(heating=True)
    evse = _evse("evse", min_current=6.0, priority=3)
    # Physical CT: the reconstructed surplus of 2500 W minus the tank's draw.
    margin = _prepare(_site(THRESHOLD + 2500.0 - 2100.0, loads=[tank, evse]))
    assert _close(margin, 2500.0, tol=1.0)
    assert _close(tank.allocated_current, 2100.0 / V)
    assert _close(evse.allocated_current, 6.0)


def test_a_lower_ranked_excess_load_waits_behind_a_tank_that_takes_it_all():
    """The live pattern: a 300 W surplus, the tank's 2.1 kW step ahead of the
    station. Nothing is left after the tank's claim, so the station does not
    start — with or without the tank already drawing (the claim is the permit,
    so the cycle the tank has only just been permitted counts the same)."""
    for heating in (True, False):
        tank = _tank(heating=heating)
        station = _evse("station", min_current=0.9, max_current=10.4, priority=3)
        draw = 2100.0 if heating else 0.0
        _prepare(_site(THRESHOLD + 300.0 - draw, loads=[tank, station]))
        # The tank's permit is its available current; the published
        # allocation is its measured footprint (0 until the element responds).
        assert _close(tank.available_current, 2100.0 / V), f"heating={heating}"
        assert _close(station.allocated_current, 0.0), f"heating={heating}"


# ---------------------------------------------------------------------------
# The pool's size takes no hysteresis
# ---------------------------------------------------------------------------
#
# The latch's release band belongs to the VERDICT — a running load rides a
# momentary dip at its minimum instead of being cut. It must not size the pool:
# a deadband on a decision is not surplus. Live 2026-09-07 the published margin
# read 989 W where the reconstruction was 489 W over its threshold, the whole
# difference being the 500 W band, and two loads sized themselves on it.


def test_the_pool_is_zero_at_the_threshold_and_tracks_above_it():
    """Anze's arithmetic: at the threshold the pool is exactly 0, and 100 W
    above it the pool is 100 W."""
    at = _evse("at", min_current=0.9, max_current=10.4)
    _prepare(_site(THRESHOLD, loads=[at]))
    # A pool of 0 is still Excess (saturated), so the load starts at its floor.
    assert _close(at.allocated_current, 0.9)

    above = _evse("above", min_current=0.9, max_current=10.4)
    margin = _prepare(_site(THRESHOLD + 100.0, loads=[above]))
    assert _close(margin, 100.0, tol=1.0)


def test_the_hysteresis_never_widens_the_pool():
    """Same site, same engaged verdict, hysteresis 0 vs 500: the allocation is
    identical. Before the fix the band was handed out as surplus."""
    allocations = []
    for hysteresis in (0.0, 500.0):
        load = _evse("evse", min_current=0.9, max_current=10.4)
        site = _site(THRESHOLD + 300.0, loads=[load])
        site.excess_hysteresis = hysteresis
        _prepare(site)
        allocations.append(load.allocated_current)
    assert _close(*allocations), allocations
    # And the 300 W surplus is what sized it, not 300 + 500.
    assert _close(allocations[0], 300.0 / V, tol=0.05)


def test_the_release_band_still_keeps_a_running_load_alive():
    """The verdict keeps its hysteresis, so a load already running rides a dip
    below the threshold at its minimum rather than being cut — the pool's size
    losing the band must not cost the latch its job."""
    load = _evse("evse", min_current=0.9, max_current=10.4, draw=0.9)
    site = _site(THRESHOLD - 200.0 - 0.9 * V, loads=[load])
    site.excess_hysteresis = 500.0
    _prepare(site)
    assert _close(load.allocated_current, 0.9)


def _site_3ph(export_w, loads=(), breaker=BREAKER, threshold=THRESHOLD):
    """The same batteryless site on three phases, exporting ``export_w`` TOTAL.

    Spread evenly, as a symmetric inverter does. The export limit the Excess
    threshold stands for is a site total, so what the pool rations is the total
    — which is the whole point of the test below.
    """
    per_phase = export_w / V / 3.0
    return SiteContext(
        voltage=V,
        main_breaker_rating=breaker,
        consumption=PhaseValues(0.0, 0.0, 0.0),
        export_current=PhaseValues(per_phase, per_phase, per_phase),
        grid_current=PhaseValues(-per_phase, -per_phase, -per_phase),
        excess_export_threshold=threshold,
        battery_soc=None,
        battery_power=None,
        battery_max_charge_power=None,
        battery_max_discharge_power=None,
        loads=list(loads),
        circuit_groups=[],
    )


# ---------------------------------------------------------------------------
# The pool's own physics: net, signed, summed
# ---------------------------------------------------------------------------
#
# The excess pool carries ``netting=True`` because what it rations is the
# surplus that cannot be EXPORTED, and the export position is a site total. The
# gross pools (grid, inverter, group, solar, physical) keep the default: a
# breaker, an inverter leg and a contractual export limit are all per-phase
# quantities. Anze's rule, 2026-09-07: per-phase -1, -1, 2 is a site total of 0.


def test_the_pool_totals_its_phases():
    """-1, -1, 2 sums to 0, so nothing is available anywhere — a load on C must
    not see its own +2 while A and B are importing, because taking it would
    simply make the site import."""
    pool = PhaseConstraints.from_per_phase(-1.0, -1.0, 2.0, netting=True)
    assert _close(pool.ABC, 0.0)
    assert _close(pool.get_available("C"), 0.0)
    # A three-phase load is bound by its WEAKEST leg (-1), not by the total's
    # share (0) — it cannot avoid drawing on the importing phases. Either way
    # it is refused; the value says which constraint answered.
    assert _close(pool.get_available("ABC"), -1.0)


def test_an_importing_phase_does_not_bind_a_load_elsewhere():
    """-2, 1, 2 nets to 1, and a load on C may take that 1 A: its own phase
    holds 2 A and the site has 1 A spare.

    It used to get 0, because the two-phase field AC = -2 + 2 = 0 was applied
    as a bound on a single-phase load that is not even on phase A. That was
    never a gross-vs-net matter — a pair field bounds a load SPANNING those
    phases and nothing else — so it was fixed in ``get_available`` itself and
    both readings now agree. Asserted for both to keep it that way.
    """
    net = PhaseConstraints.from_per_phase(-2.0, 1.0, 2.0, netting=True)
    gross = PhaseConstraints.from_per_phase(-2.0, 1.0, 2.0)
    assert _close(net.get_available("C"), 1.0)
    assert _close(gross.get_available("C"), 1.0)


def test_a_claim_leaves_the_rest_of_the_site_total():
    """A claim bigger than its own phase's share must not consume the other
    phases. 4.348 A/phase (3 kW) less a 9.13 A claim on B leaves 3.91 A —
    900 W of real surplus. The gross cascade zeroed every field, including
    that."""
    pool = PhaseConstraints.from_per_phase(4.348, 4.348, 4.348, netting=True)
    after = pool.deduct(9.130, "B")
    assert _close(after.ABC, 3.914, tol=0.01)
    assert _close(after.get_available("C"), 3.914, tol=0.01)

    gross_after = PhaseConstraints.from_per_phase(4.348, 4.348, 4.348).deduct(9.130, "B")
    assert _close(gross_after.ABC, 0.0)


def test_the_netting_flag_survives_every_pool_operation():
    """A method that drops the flag silently reverts the pool to gross — a
    wrong number with no error, so every operation is pinned."""
    base = PhaseConstraints.from_per_phase(1.0, 1.0, 1.0, netting=True)
    other = PhaseConstraints.from_per_phase(1.0, 1.0, 1.0, netting=True)
    for name, obj in (
        ("copy", base.copy()),
        ("add", base + other),
        ("element_min", base.element_min(other)),
        ("element_max", base.element_max(other)),
        ("deduct", base.deduct(0.5, "A")),
        ("normalize", base.normalize()),
        ("zeros", PhaseConstraints.zeros(netting=True)),
    ):
        assert obj.netting is True, name
    # And the default is still gross, on every constructor.
    assert PhaseConstraints.zeros().netting is False
    assert PhaseConstraints.from_per_phase(1.0, 1.0, 1.0).netting is False
    assert PhaseConstraints.from_pool(1.0, 1.0, 1.0, 3.0).netting is False


def test_a_net_pool_is_never_reshaped_by_normalize():
    """normalize()'s clamp-and-cascade is the symmetric-inverter rule — 5 A on
    one leg consumes 5 A on all three. True of capacity, false of surplus, so a
    net pool passes through untouched even with a phase deep in the negative."""
    pool = PhaseConstraints.from_per_phase(-5.0, 4.0, 4.0, netting=True)
    assert pool.normalize() == pool


def test_a_claim_on_one_phase_counts_against_a_load_on_another():
    """A claim anywhere is a claim against everyone, whatever the inverter's
    phase symmetry, because the export limit it rations is a SITE TOTAL.

    Live on 2026-09-07: the tank claimed 9.4 A on phase B and the station on
    phase C still read its own pool as untouched, so both ran on the same site
    headroom. ``_excess_ahead`` used to branch on
    ``inverter_supports_asymmetric`` and, for a symmetric inverter, count only
    the claims landing on this load's own phases — the per-phase view that is
    correct for the INVERTER CAPACITY pool and wrong for this one.
    """
    tank = _tank(phase="B", heating=True)
    station = _evse(
        "station", min_current=0.9, max_current=10.4, priority=3, phase="C"
    )
    _prepare(_site_3ph(THRESHOLD + 300.0 - 2100.0, loads=[tank, station]))
    assert _close(tank.available_current, 2100.0 / V)
    # 300 W of site surplus, the tank's claim is 2.1 kW: nothing is left for
    # the station even though no claim landed on phase C.
    assert _close(station.allocated_current, 0.0)


def test_a_claim_on_another_phase_still_leaves_room_when_there_is_room():
    """The mirror: the scope change must not starve a load that genuinely fits.

    3 kW of site surplus against the tank's 2.1 kW claim leaves 900 W, and the
    station starts — on 1.3 A, which is the leftover spread over the site's
    three phases (4.35 A of phase-C surplus less the claim's 3.04 A share),
    not the 3.9 A that 900 W on one phase would be.

    THE CAUSE IS NOT THE CLAIM ACCOUNTING, which was the first guess and is
    now gone: the pool is per-phase and net, and ``_excess_ahead`` re-deducts
    the claims through the pool itself with no divisor. The figure did not
    move. It is the LOADS-OFF RECONSTRUCTION that decides this: the tank's
    2.1 kW is credited back onto phase B, the phase it draws on, so B is where
    the surplus appears and phase C is left with only its own share. A load on
    C is then bound by C, not by the site total.

    Whether that is a defect depends on a question this test does not settle:
    an Excess load may not drive its OWN phase into import (Anze, 2026-09-07 —
    a Standard load may, and does, because it reads the physical pool), and
    letting C reach the whole site total would do exactly that. So the 1.3 A is
    the conservative reading of a real constraint, not an accounting artifact.
    """
    tank = _tank(phase="B", heating=True)
    station = _evse(
        "station", min_current=0.9, max_current=10.4, priority=3, phase="C"
    )
    _prepare(_site_3ph(THRESHOLD + 3000.0 - 2100.0, loads=[tank, station]))
    assert _close(tank.available_current, 2100.0 / V)
    assert _close(station.allocated_current, 1.30)


def test_a_running_lower_ranked_load_yields_when_the_tank_claims_the_surplus():
    """The station was charging at its 0.9 A floor when the tank engaged. With
    the tank's claim exceeding the 300 W surplus the station is cut, not held
    at its floor — the rank above it owns the surplus."""
    tank = _tank(heating=True)
    station = _evse("station", min_current=0.9, max_current=10.4, priority=3, draw=0.9)
    _prepare(_site(THRESHOLD + 300.0 - 2100.0 - 0.9 * V, loads=[tank, station]))
    assert _close(tank.allocated_current, 2100.0 / V)
    assert _close(station.allocated_current, 0.0)


def test_an_idle_tank_about_to_boost_claims_before_it_heats():
    """The verdict-on cycle: the tank's thermostat has not responded yet, so
    the tank is INACTIVE — but it will boost, and it says so
    (excess_claim_current). The station behind it must not start on the 300 W
    the tank is about to take, on that very cycle."""
    tank = _tank(heating=False)
    tank.connector_status = "Available"  # thermostat idle → inactive
    tank.excess_claim_current = 2100.0 / V
    station = _evse("station", min_current=0.9, max_current=10.4, priority=3)
    _prepare(_site(THRESHOLD + 300.0, loads=[tank, station]))
    assert tank.allocated_current == 0  # inactive: nothing allocated
    assert _close(station.allocated_current, 0.0)

    # With room to spare after the tank's claim, the station still starts.
    tank2 = _tank(heating=False)
    tank2.connector_status = "Available"
    tank2.excess_claim_current = 2100.0 / V
    station2 = _evse("station2", min_current=0.9, max_current=10.4, priority=3)
    _prepare(_site(THRESHOLD + 2500.0, loads=[tank2, station2]))
    # Sized on what the tank's claim leaves (400 W), not on the whole pool the
    # not-yet-drawing tank has left untouched.
    assert _close(station2.allocated_current, 400.0 / V, tol=0.06)

    # An idle tank that will NOT boost (already above its boost setpoint —
    # claim 0) leaves the surplus to the station.
    tank3 = _tank(heating=False)
    tank3.connector_status = "Available"
    station3 = _evse("station3", min_current=0.9, max_current=10.4, priority=3)
    _prepare(_site(THRESHOLD + 300.0, loads=[tank3, station3]))
    assert _close(station3.allocated_current, 300.0 / V, tol=0.06)


def test_a_settled_grid_backed_evse_claims_only_its_draw():
    """A Standard-mode car permitted 16 A but settled at 10 A does not block
    the surplus it is not using: an Excess load behind it sees the pool minus
    10 A, not minus 16 A."""
    car = LoadContext(
        load_id="car", entity_id="car", min_current=6.0, max_current=16.0,
        phases=1, priority=1, device_type="evse", operating_mode="Standard",
        mode_behavior="full_power", mode_priority=1, active_phases_mask="A",
        l1_phase="A", l1_current=10.0, draw_settled=True,
    )
    evse = _evse("evse", min_current=6.0, priority=2)
    # 12 A of surplus with the car's 10 A already off the CT.
    _prepare(_site(THRESHOLD + 12.0 * V - 10.0 * V, loads=[car, evse]))
    assert _close(evse.allocated_current, 6.0)


def test_the_pool_beyond_the_floors_follows_the_rank():
    """20 A of pool, two 6 A floors: the rest is the higher-ranked load's, and
    the lower-ranked one stays at its floor. Ordering is unchanged — the same
    _rank the distributor has always served."""
    first = _evse("first", priority=1)
    second = _evse("second", priority=2)
    margin = _prepare(_site(THRESHOLD + 20.0 * V, loads=[first, second]))
    assert _close(margin, 4600.0)
    assert first.allocated_current > second.allocated_current
    assert _close(first.allocated_current, 14.0)
    assert _close(second.allocated_current, 6.0)


# ---------------------------------------------------------------------------
# The floor does not overrule physical limits
# ---------------------------------------------------------------------------

def test_a_circuit_group_that_cannot_fit_the_minimum_still_stops_the_load():
    """A group breaker with 4 A behind it cannot carry a 6 A minimum, verdict or
    no verdict: the group cap is enforced after distribution and zeroes a member
    it cannot bring to its minimum."""
    load = _evse()
    group = CircuitGroup(
        group_id="g", name="garage", current_limit=4.0, member_ids=["evse"]
    )
    _prepare(_site(THRESHOLD, loads=[load], groups=[group]))
    assert _close(load.allocated_current, 0.0)


def test_a_wire_that_cannot_fit_the_minimum_still_stops_the_load():
    """The physical pool is checked before the floor is ever reserved: a 4 A
    main breaker leaves no room for a 6 A minimum."""
    load = _evse()
    _prepare(_site(THRESHOLD, loads=[load], breaker=4.0))
    assert _close(load.allocated_current, 0.0)


def test_a_phase_the_site_does_not_have_still_stops_the_load():
    """The floor is per-phase current on the phases the load occupies. This site
    has only phase A, so a load wired to B has no pool to floor — the
    phase-mask arithmetic zeroes it while the verdict is on."""
    load = _evse(phase="B")
    margin = _prepare(_site(THRESHOLD, loads=[load]))
    assert _close(margin, 0.0)
    assert _close(load.allocated_current, 0.0)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    # Deliberately pytest-free: the pure tier has to run on the developer's
    # machine, which has no pytest (dev/tests/conftest.py imports HA anyway).
    failed = []
    for _name, _fn in sorted(list(globals().items())):
        if not _name.startswith("test_") or not callable(_fn):
            continue
        try:
            _fn()
        except Exception as exc:  # noqa: BLE001 - report and continue
            failed.append((_name, exc))
            print(f"FAIL {_name}: {type(exc).__name__}: {exc}")
        else:
            print(f"PASS {_name}")
    print(f"\n{'FAILED' if failed else 'OK'} — {len(failed)} failure(s)")
    sys.exit(1 if failed else 0)
