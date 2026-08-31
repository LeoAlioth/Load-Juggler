"""The Excess start edge — what a modulating Excess load is granted.

Machine-authored tests — not yet human-reviewed.

The rule: *the verdict starts the load, the pool only sizes it.* A modulating
load (an EVSE) cannot run below its minimum current, so while Excess is engaged
its minimum IS the floor — held there while the momentary pool is smaller than
it, followed upward once the pool exceeds it. That is the start edge the binary
Excess loads (plug, tank boost) have always had: they engage on threshold-hit
even though their whole rating overshoots the pool.

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
    """Not a constant: a 10 A load floors at 10 A, a 6 A one at 6 A."""
    small = _evse("small", min_current=6.0, priority=1)
    large = _evse("large", min_current=10.0, priority=2)
    _prepare(_site(THRESHOLD, loads=[small, large]))
    assert _close(small.allocated_current, 6.0)
    assert _close(large.allocated_current, 10.0)


def test_two_excess_loads_on_an_empty_pool_each_get_their_floor():
    """The shared verdict engages both, and the first one's draw draining the
    pool to zero does not stop the second — the same way two Excess plugs both
    switch on. (The multi-load attribution question this raises is the
    watch-only 'Excess margin over-credits' item in dev/TODO.md; parity with
    the binary loads is all that is claimed here.)"""
    first = _evse("first", priority=1)
    second = _evse("second", priority=2)
    _prepare(_site(THRESHOLD, loads=[first, second]))
    assert _close(first.allocated_current, 6.0)
    assert _close(second.allocated_current, 6.0)


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
