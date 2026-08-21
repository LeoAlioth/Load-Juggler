"""The dual gate on SOC-gated binary loads — SOC *and* inverter coverage.

Machine-authored tests — not yet human-reviewed.

ISSUES.md #17, reversed from "accepted by design" to a fix. A battery-backed
plug in Solar Priority / Solar Only / Excess is switched on by an SOC verdict
alone: *there is stored energy*. That says nothing about the PATH. While the
inverters already put out everything they are rated for, the plug's power cannot
come from the battery — it comes from the grid (or, off grid, pushes the
inverters past their plate rating). ``_source_limit`` now also requires the
inverter's rating to cover the load's own draw.

The gate is evaluated **with the load off** (ISSUES.md #41's discipline): a gate
the load's own draw can flip is a gate that suppresses itself. What makes that
add-back honest is the grid term — a draw the site is IMPORTING for is not
something the inverters are delivering, so shedding it frees no inverter
capacity. That single term is the difference between a gate that sheds a
grid-fed load and a one-way latch that can never shed anything once it is on.

Pure Python, no Home Assistant dependencies. Runnable two ways:
  python3 dev/tests/test_inverter_gate.py     (standalone, no pytest needed)
  pytest dev/tests/test_inverter_gate.py      (Docker / CI tier)
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
    LoadContext,
    PhaseConstraints,
    PhaseValues,
    SiteContext,
)
from custom_components.dynamic_ocpp_evse.calculations.target_calculator import (  # noqa: E402
    _source_limit,
)
from custom_components.dynamic_ocpp_evse.const.common import (  # noqa: E402
    BEHAVIOR_BINARY_ABOVE_MIN,
    BEHAVIOR_BINARY_ABOVE_TARGET,
    BEHAVIOR_BINARY_EXCESS,
    DEVICE_TYPE_PLUG,
)

V = 230.0
NO_POOL = PhaseConstraints.zeros()


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------
def _plug(
    watts,
    behavior=BEHAVIOR_BINARY_ABOVE_TARGET,
    running=False,
    entity_id="plug",
    priority=1,
    mode_priority=3,
):
    """A binary load: min == max == its rating, on phase A.

    ``running`` sets the measured draw the way the HA layer does for a plug —
    its rating while switched on, 0 while off.
    """
    amps = watts / V
    load = LoadContext(
        load_id=entity_id,
        entity_id=entity_id,
        min_current=amps,
        max_current=amps,
        phases=1,
        priority=priority,
        device_type=DEVICE_TYPE_PLUG,
        operating_mode="Solar Only",
        mode_behavior=behavior,
        mode_priority=mode_priority,
        active_phases_mask="A",
        rated_current=amps,
    )
    load.l1_current = amps if running else 0.0
    return load


def _site(
    loads,
    rating=5000.0,
    output=0.0,
    net_grid=0.0,
    soc=85.0,
    soc_target=80.0,
    soc_min=20.0,
    soc_full=97.0,
):
    """A 1-phase battery site. ``output``/``net_grid`` are the read-time watts
    the engine captures before its feedback loop (fleet AC output, signed net
    meter: + import / − export)."""
    site = SiteContext(
        voltage=V,
        main_breaker_rating=35,
        consumption=PhaseValues(max(0.0, net_grid / V), None, None),
        export_current=PhaseValues(max(0.0, -net_grid / V), None, None),
        battery_soc=soc,
        battery_soc_min=soc_min,
        battery_soc_target=soc_target,
        battery_soc_full=soc_full,
        battery_max_charge_power=5000.0,
        battery_max_discharge_power=5000.0,
        inverter_max_power=rating,
        inverter_max_power_per_phase=rating,
        inverter_output_total=output,
        net_grid_power=net_grid,
    )
    site.loads = list(loads)
    return site


def _limit(site, load, excess=NO_POOL):
    return _source_limit(load, site, NO_POOL, excess, base=0)


def _close(a, b, tol=1e-6):
    return abs(a - b) <= tol


# ---------------------------------------------------------------------------
# The dual gate: SOC and coverage must BOTH pass
# ---------------------------------------------------------------------------
def test_soc_pass_with_inverter_headroom_grants_the_full_rating():
    """5 kW inverter putting out 1 kW, battery above target → the 2 kW plug is
    covered twice over and gets its whole rating."""
    plug = _plug(2000)
    site = _site([plug], rating=5000.0, output=1000.0)
    assert _close(_limit(site, plug), 2000.0 / V)


def test_soc_pass_with_a_saturated_inverter_grants_nothing():
    """Same battery verdict, but the inverter is already delivering its rated
    5 kW to a must-run load: the plug's 2 kW could only come from the grid."""
    plug = _plug(2000)
    site = _site([plug], rating=5000.0, output=5000.0, net_grid=0.0)
    assert _close(_limit(site, plug), 0.0)


def test_coverage_is_measured_against_the_loads_own_rating():
    """The gate asks for THIS load's draw, not for a fixed margin: 1.5 kW of
    headroom carries a 1.4 kW load and refuses a 1.6 kW one."""
    small = _plug(1400)
    big = _plug(1600)
    assert _close(_limit(_site([small], rating=5000.0, output=3500.0), small),
                  1400.0 / V)
    assert _close(_limit(_site([big], rating=5000.0, output=3500.0), big), 0.0)


def test_solar_priority_binary_takes_the_same_gate():
    """Solar Priority (SOC > minimum) is an SOC-derived permit too."""
    plug = _plug(2000, behavior=BEHAVIOR_BINARY_ABOVE_MIN)
    assert _close(_limit(_site([plug], output=1000.0), plug), 2000.0 / V)
    saturated = _plug(2000, behavior=BEHAVIOR_BINARY_ABOVE_MIN)
    assert _close(_limit(_site([saturated], output=5000.0), saturated), 0.0)


def test_a_failing_soc_still_denies_a_wide_open_inverter():
    """The gate is AND, not OR — an idle inverter cannot rescue a flat battery."""
    plug = _plug(2000)
    site = _site([plug], rating=5000.0, output=0.0, soc=50.0, soc_target=80.0)
    assert _close(_limit(site, plug), 0.0)


# ---------------------------------------------------------------------------
# The load-off property (ISSUES.md #41): the gate must not read the load's own
# draw as a reason to shed it.
# ---------------------------------------------------------------------------
def test_an_engaged_load_that_saturates_the_inverter_itself_stays_eligible():
    """The plug is ON and its 2 kW is the very power that took the inverter to
    its 5 kW rating — the site imports nothing, so the battery is carrying it.
    Judged with the load off, the inverter is at 3 kW with 2 kW spare: it keeps
    its permit. Without the add-back this load would shed itself every cycle."""
    plug = _plug(2000, running=True)
    site = _site([plug], rating=5000.0, output=5000.0, net_grid=0.0)
    assert _close(_limit(site, plug), 2000.0 / V)


def test_an_engaged_load_the_site_is_importing_for_is_shed():
    """Identical output and identical draw as the test above — the ONE thing
    that differs is that the site imports 2 kW. Then the inverter is not the
    thing carrying the plug, shedding it frees no inverter capacity, and the
    load-off headroom is still 0. This is the case ISSUES #17 reported, and the
    case a naive add-back can never shed."""
    plug = _plug(2000, running=True)
    site = _site([plug], rating=5000.0, output=5000.0, net_grid=2000.0)
    assert _close(_limit(site, plug), 0.0)


def test_export_the_load_could_displace_counts_as_coverage():
    """A clipping inverter at its rating with 2 kW going to the grid: switching
    the plug on redirects the export instead of raising the output, so nothing
    is imported and the permit stands (the plug here is still OFF, so this is
    coverage the load's own draw cannot have supplied)."""
    plug = _plug(2000)
    site = _site([plug], rating=5000.0, output=5000.0, net_grid=-2000.0)
    assert _close(_limit(site, plug), 2000.0 / V)


def test_a_higher_ranked_load_may_claim_what_it_would_preempt():
    """A running Solar Priority plug (urgency tier 3) holds the whole inverter.
    A cold tank promoted to tier 1 outranks it, so the tank's gate counts that
    draw as capacity it can take — otherwise an incumbent low-priority load
    would lock preemption out of a saturated inverter. The pond's own gate is
    unmoved: it may only ever credit itself."""
    pond = _plug(1400, behavior=BEHAVIOR_BINARY_ABOVE_MIN, running=True,
                 entity_id="pond", priority=1, mode_priority=3)
    tank = _plug(2000, behavior=BEHAVIOR_BINARY_ABOVE_MIN, entity_id="tank",
                 priority=3, mode_priority=1)
    site = _site([pond, tank], rating=2100.0, output=1400.0, net_grid=0.0)
    assert _close(_limit(site, tank), 2000.0 / V)   # 2100 − (1400 − 1400)
    assert _close(_limit(site, pond), 1400.0 / V)   # its own draw, always its own
    # …while a load the pond outranks credits only itself: 2100 − 1400 = 700 W
    # of headroom, which does not carry 800 W.
    strip = _plug(800, behavior=BEHAVIOR_BINARY_ABOVE_MIN, entity_id="strip",
                  priority=4, mode_priority=3)
    outranked = _site([pond, strip], rating=2100.0, output=1400.0, net_grid=0.0)
    assert _close(_limit(outranked, strip), 0.0)


# ---------------------------------------------------------------------------
# Sites with no inverter rating, or no output reading, are untouched
# ---------------------------------------------------------------------------
def test_no_inverter_rating_means_unlimited():
    """A site that never told us its inverter capacity keeps the pre-gate
    behavior — for None and for a 0 that means 'not configured'."""
    for rating in (None, 0.0):
        plug = _plug(2000)
        site = _site([plug], rating=rating, output=99999.0)
        assert _close(_limit(site, plug), 2000.0 / V), rating


def test_an_unknown_output_does_not_gate():
    """No output figure at all (a hand-built context, a site with no inverter
    entities) → the gate abstains rather than shedding blind."""
    plug = _plug(2000)
    site = _site([plug], rating=5000.0)
    site.inverter_output_total = None
    assert _close(_limit(site, plug), 2000.0 / V)


# ---------------------------------------------------------------------------
# Excess: only the SOC-derived half of the verdict is gated
# ---------------------------------------------------------------------------
def test_excess_near_full_shortcut_is_gated():
    """'The battery is full, so it cannot absorb any more' is an SOC verdict,
    and it is no evidence the inverter can pass this load's draw. A full battery
    behind a saturated inverter is precisely the grid-draw case."""
    plug = _plug(2000, behavior=BEHAVIOR_BINARY_EXCESS)
    covered = _site([plug], rating=5000.0, output=1000.0, soc=98.0)
    assert _close(_limit(covered, plug), 2000.0 / V)
    saturated = _site([_plug(2000, behavior=BEHAVIOR_BINARY_EXCESS)],
                      rating=5000.0, output=5000.0, soc=98.0)
    assert _close(_limit(saturated, saturated.loads[0]), 0.0)


def test_excess_falls_through_to_the_export_rule_when_saturated():
    """A denied near-full shortcut does not answer 0 outright: the export pool
    is a FLOW verdict — the power is already on the AC bus — so it stands on its
    own and needs no coverage gate. A clipping inverter that is still exporting
    keeps the load on."""
    plug = _plug(2000, behavior=BEHAVIOR_BINARY_EXCESS)
    site = _site([plug], rating=5000.0, output=5000.0, net_grid=0.0, soc=98.0)
    pool = PhaseConstraints.from_per_phase(20.0, 0.0, 0.0)
    assert _close(_limit(site, plug, excess=pool), 2000.0 / V)


def test_excess_without_a_full_battery_is_pure_flow_and_ungated():
    """Below full SOC, Excess was always export-driven; the gate must not
    change that verdict in either direction."""
    plug = _plug(2000, behavior=BEHAVIOR_BINARY_EXCESS)
    site = _site([plug], rating=5000.0, output=5000.0, soc=85.0)
    pool = PhaseConstraints.from_per_phase(20.0, 0.0, 0.0)
    assert _close(_limit(site, plug, excess=pool), 2000.0 / V)
    assert _close(_limit(site, plug, excess=NO_POOL), 0.0)


def test_a_battery_free_site_keeps_the_live_surplus_rule():
    """With no battery the binary SOC modes fall through to the solar/excess
    rules, which are flow-derived — the coverage gate never runs."""
    plug = _plug(2000)
    site = _site([plug], rating=5000.0, output=5000.0, soc=None)
    solar = PhaseConstraints.from_per_phase(20.0, 0.0, 0.0)
    assert _close(
        _source_limit(plug, site, solar, NO_POOL, base=0), 20.0
    )


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
