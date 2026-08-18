"""Unit tests for inverter AC output: signed readings and display headroom.

Machine-authored tests — not yet human-reviewed.

Two fixes are pinned here (ISSUES.md #13 and #15):

**#13 — headroom must not assume a wiring topology.** The hub's display
headroom is ``inverter_rating − current_output``. The old current-output form
was ``solar + battery_power``, which is the SERIES (DC-coupled) model: charging
takes DC power that never reaches the AC side. On a PARALLEL (AC-coupled) site
the battery charges FROM the AC bus, so the PV inverter still puts out its full
production and the old form understated the output by the whole charge power —
Site Remaining Power then advertised headroom the site does not have.
``fleet.output_power_total`` fixes this in two tiers: measured output when
output entities exist (no topology assumption at all), else a topology-aware
per-member estimate.

**#15 — inverter output readings are signed.** A hybrid with an AC-coupled
inverter on its load port legitimately reads NEGATIVE output, up to the child's
production. ``abs()`` fabricated output; a 0-clamp would erase the term the
fleet sum needs to net the child's back-feed against its parent. The clamps
that remain are the aggregates where physics demands them: a member's derived
production, the fleet solar total, per-phase household.

Pure Python, no Home Assistant dependencies. Runnable two ways:
  python3 dev/tests/test_inverter_output.py     (standalone, no pytest needed)
  pytest dev/tests/test_inverter_output.py      (Docker / CI tier)
"""

import sys
import types
import importlib.util
from pathlib import Path

# ---------------------------------------------------------------------------
# Module loading (same pattern as run_tests.py / test_household_hold.py: the
# package root imports homeassistant, so load the pure modules directly).
# engine/fleet.py itself is HA-free — it needs only calculations.PhaseValues
# and the const package.
# ---------------------------------------------------------------------------
repo_root = Path(__file__).parents[2]
_comp_dir = repo_root / "custom_components" / "dynamic_ocpp_evse"
_calc_dir = _comp_dir / "calculations"
_const_dir = _comp_dir / "const"
_engine_dir = _comp_dir / "engine"

_PKG_ROOT = "custom_components"
_PKG_COMP = "custom_components.dynamic_ocpp_evse"
_PKG_CALC = "custom_components.dynamic_ocpp_evse.calculations"
_PKG_ENGINE = "custom_components.dynamic_ocpp_evse.engine"

for _pkg_name in (_PKG_ROOT, _PKG_COMP, _PKG_CALC, _PKG_ENGINE):
    if _pkg_name not in sys.modules:
        _pkg = types.ModuleType(_pkg_name)
        _pkg.__path__ = []
        _pkg.__package__ = _pkg_name
        sys.modules[_pkg_name] = _pkg


def _load_module_as(fqn, path):
    spec = importlib.util.spec_from_file_location(fqn, str(path))
    module = importlib.util.module_from_spec(spec)
    if Path(path).name == "__init__.py":
        module.__package__ = fqn
        module.__path__ = [str(Path(path).parent)]
    else:
        module.__package__ = fqn.rsplit(".", 1)[0] if "." in fqn else fqn
    sys.modules[fqn] = module
    spec.loader.exec_module(module)
    return module


if f"{_PKG_COMP}.const" not in sys.modules:
    for _sub in (
        "common", "hub", "inverter", "group", "evse", "plug",
        "hot_water_tank", "power_station", "modes",
    ):
        _load_module_as(f"{_PKG_COMP}.const.{_sub}", _const_dir / f"{_sub}.py")
    _load_module_as(f"{_PKG_COMP}.const", _const_dir / "__init__.py")

if f"{_PKG_CALC}.models" not in sys.modules:
    _load_module_as(f"{_PKG_CALC}.models", _calc_dir / "models.py")
# fleet.py does `from ..calculations import PhaseValues`; the stub calculations
# package re-exports it without pulling in the whole engine.
sys.modules[_PKG_CALC].PhaseValues = sys.modules[f"{_PKG_CALC}.models"].PhaseValues

if f"{_PKG_ENGINE}.fleet" not in sys.modules:
    _load_module_as(f"{_PKG_ENGINE}.fleet", _engine_dir / "fleet.py")

from custom_components.dynamic_ocpp_evse.calculations.models import PhaseValues
from custom_components.dynamic_ocpp_evse.const import (
    WIRING_TOPOLOGY_PARALLEL,
    WIRING_TOPOLOGY_SERIES,
)
from custom_components.dynamic_ocpp_evse.engine.fleet import (
    FleetMember,
    member_solar,
    member_solar_production,
    output_power_estimate,
    output_power_measured,
    output_power_total,
    solar_total,
    sum_outputs,
)

V = 230.0


def _close(a, b, tol=1e-6):
    return abs(a - b) < tol


def _amps(watts, voltage=V):
    return watts / voltage


def _pv_watts(*phases):
    """PhaseValues in amps from per-phase watts (readability in the fixtures)."""
    return PhaseValues(*[None if w is None else _amps(w) for w in phases])


# The display's headroom rule, restated from engine/hub_calculation.py
# (_build_hub_result): rating minus current output, clamped into [0, rating].
# The upper clamp is the decision this file pins: a NEGATIVE measured output
# means power is flowing into the inverter, which does not raise its own AC
# output capability above its nameplate, so it must not buy extra headroom.
def _headroom(rating, output):
    return max(0.0, min(float(rating), rating - output))


def _inverter(entry_id="inv1", topology=WIRING_TOPOLOGY_PARALLEL, **kwargs):
    return FleetMember(entry_id=entry_id, topology=topology, **kwargs)


def _hybrid(entry_id="hybrid", topology=WIRING_TOPOLOGY_SERIES, power=None, **kwargs):
    """A member with a battery power sensor (series unless told otherwise)."""
    return FleetMember(
        entry_id=entry_id,
        topology=topology,
        has_battery=True,
        has_battery_power_entity=True,
        battery_power=power,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# (a) Measured output — the same measurement means the same headroom, whatever
#     the wiring topology says
# ---------------------------------------------------------------------------

def test_measured_output_is_topology_independent():
    """8900 W of PV measured at the inverter's AC terminals is 8900 W of output
    on a series site and on a parallel one alike — the measurement already
    contains whatever the battery is doing."""
    series = _hybrid(topology=WIRING_TOPOLOGY_SERIES, power=-2000.0,
                     max_power=10000.0, output=_pv_watts(8900.0, None, None))
    parallel = _hybrid(topology=WIRING_TOPOLOGY_PARALLEL, power=-2000.0,
                       max_power=10000.0, output=_pv_watts(8900.0, None, None))

    out_series = output_power_total([series], V, solar_w=8900.0, battery_power_w=-2000.0)
    out_parallel = output_power_total([parallel], V, solar_w=8900.0, battery_power_w=-2000.0)

    assert _close(out_series, 8900.0)
    assert _close(out_parallel, 8900.0)
    assert _close(_headroom(10000.0, out_series), 1100.0)
    assert _close(_headroom(10000.0, out_parallel), 1100.0)


def test_measured_output_beats_the_old_series_formula_on_a_parallel_site():
    """The #13 bug, in numbers: a parallel site producing 8900 W while charging
    the battery with 2000 W off the AC bus. The old form read
    8900 + (−2000) = 6900 W of output and advertised 3100 W of headroom; the
    inverter is really 8900 W into its 10 kW rating, so only 1100 W is left."""
    parallel = _hybrid(topology=WIRING_TOPOLOGY_PARALLEL, power=-2000.0,
                       max_power=10000.0, output=_pv_watts(8900.0, None, None))
    old_form = max(0.0, 8900.0 + (-2000.0))
    assert _close(_headroom(10000.0, old_form), 3100.0)  # what the bug reported

    measured = output_power_total([parallel], V, solar_w=8900.0, battery_power_w=-2000.0)
    assert _close(_headroom(10000.0, measured), 1100.0)


def test_measured_output_sums_per_phase_over_the_fleet():
    a = _inverter("a", output=_pv_watts(1000.0, 1000.0, 1000.0))
    b = _inverter("b", output=_pv_watts(500.0, None, 500.0))
    assert _close(output_power_measured([a, b], V), 4000.0)


def test_measured_output_is_none_without_output_entities():
    assert output_power_measured([_inverter()], V) is None
    assert output_power_measured([], V) is None


def test_member_without_output_entities_adds_its_own_estimate():
    """A mixed fleet: one metered inverter plus a hybrid that only has a
    production sensor and a battery. The site's export-derived solar cannot be
    split per member, so the unmetered member contributes exactly what it knows
    about itself — 4000 W of PV minus 1500 W of DC-side charging."""
    metered = _inverter("metered", output=_pv_watts(3000.0, None, None))
    unmetered = _hybrid("hybrid", power=-1500.0, has_solar_entity=True,
                        solar_measured=4000.0)
    assert _close(output_power_total([metered, unmetered], V), 3000.0 + 2500.0)


# ---------------------------------------------------------------------------
# (b) Fallback estimate — topology decides whether charging reduces the output
# ---------------------------------------------------------------------------

def test_estimate_series_charging_reduces_output():
    """Series (DC-coupled): the battery is in front of the inverter, so 2000 W
    of charging never reaches the AC side."""
    series = _hybrid(topology=WIRING_TOPOLOGY_SERIES, power=-2000.0, max_power=10000.0)
    out = output_power_total([series], V, solar_w=8900.0, battery_power_w=-2000.0)
    assert _close(out, 6900.0)
    assert _close(_headroom(10000.0, out), 3100.0)


def test_estimate_parallel_charging_does_not_reduce_output():
    """Parallel (AC-coupled): the battery charges FROM the bus, so the PV
    inverter still puts out its full 8900 W — 2000 W less headroom than the
    series site above, from the same scalars."""
    parallel = _hybrid(topology=WIRING_TOPOLOGY_PARALLEL, power=-2000.0,
                       max_power=10000.0)
    out = output_power_total([parallel], V, solar_w=8900.0, battery_power_w=-2000.0)
    assert _close(out, 8900.0)
    assert _close(_headroom(10000.0, out), 1100.0)


def test_estimate_discharge_adds_to_output_in_both_topologies():
    """Discharge always passes through the inverter, whatever the coupling."""
    for topology in (WIRING_TOPOLOGY_SERIES, WIRING_TOPOLOGY_PARALLEL):
        m = _hybrid(topology=topology, power=1500.0)
        out = output_power_total([m], V, solar_w=5000.0, battery_power_w=1500.0)
        assert _close(out, 6500.0), topology


def test_estimate_mixed_fleet_uses_each_members_own_topology():
    """A series hybrid charging at 2000 W beside an AC-coupled battery charging
    at 1000 W: only the DC-coupled one subtracts. Site PV 6000 W → 4000 W out."""
    series = _hybrid("s", topology=WIRING_TOPOLOGY_SERIES, power=-2000.0)
    parallel = _hybrid("p", topology=WIRING_TOPOLOGY_PARALLEL, power=-1000.0)
    out = output_power_total([series, parallel], V, solar_w=6000.0,
                             battery_power_w=-3000.0)
    assert _close(out, 4000.0)


def test_estimate_without_any_battery_sensor_is_just_solar():
    """No battery power sensor anywhere: the fleet reading is None, so the
    battery term is 0 and the estimate reduces to the site's production."""
    m = _inverter(max_power=5000.0)
    out = output_power_total([m], V, solar_w=3200.0, battery_power_w=None)
    assert _close(out, 3200.0)
    assert _close(_headroom(5000.0, out), 1800.0)


def test_estimate_with_no_members_at_all_falls_back_to_site_scalars():
    """Defensive: an empty fleet degenerates to the site scalars (parallel
    rule), never to a crash."""
    assert _close(output_power_estimate([], 4000.0, -1000.0), 4000.0)
    assert _close(output_power_total([], V, solar_w=4000.0, battery_power_w=500.0), 4500.0)


# ---------------------------------------------------------------------------
# (c) Signed readings — the cascade case (#15)
# ---------------------------------------------------------------------------

def test_negative_member_output_nets_against_a_positive_sibling():
    """The motivating site: an AC-coupled inverter (B, +5000 W) sits on the
    hybrid's (A) load port, so A's own output sensor reads −1500 W. The pair
    delivers 3500 W to the site, which is exactly what the signed sum reports.
    ``abs()`` would have claimed 6500 W."""
    a = _inverter("a", topology=WIRING_TOPOLOGY_SERIES, output=_pv_watts(-1500.0, None, None))
    b = _inverter("b", topology=WIRING_TOPOLOGY_PARALLEL, output=_pv_watts(5000.0, None, None))
    assert _close(output_power_measured([a, b], V), 3500.0)
    assert _close(output_power_total([a, b], V), 3500.0)
    # What abs() used to produce, for contrast.
    assert not _close(output_power_measured([a, b], V), 6500.0)


def test_sum_outputs_nets_signed_readings_per_phase():
    a = _inverter("a", output=PhaseValues(-5.0, -5.0, -5.0))
    b = _inverter("b", output=PhaseValues(10.0, 10.0, 10.0))
    summed = sum_outputs([a, b])
    assert (summed.a, summed.b, summed.c) == (5.0, 5.0, 5.0)
    assert _close(summed.total, 15.0)


def test_negative_phase_still_counts_as_a_fed_phase():
    """A phase reading −3 A is still a phase this inverter is connected to —
    None is the only "phase absent" signal."""
    m = _inverter(output=PhaseValues(-3.0, None, None))
    assert m.spans_phase("a") and not m.spans_phase("b")
    summed = sum_outputs([m])
    assert summed.a == -3.0 and summed.b is None


def test_net_negative_fleet_output_cannot_exceed_the_rating_in_headroom():
    """A site absorbing 1000 W net through its inverters has its full rating
    available, and no more: feeding power in through the load port does not
    raise the inverter's own AC output capability."""
    a = _inverter("a", output=_pv_watts(-3000.0, None, None))
    b = _inverter("b", output=_pv_watts(2000.0, None, None))
    out = output_power_total([a, b], V)
    assert _close(out, -1000.0)
    assert _close(_headroom(5000.0, out), 5000.0)  # not 6000


def test_output_above_the_rating_gives_zero_headroom():
    m = _inverter(output=_pv_watts(6000.0, None, None), max_power=5000.0)
    assert _close(_headroom(5000.0, output_power_total([m], V)), 0.0)


# ---------------------------------------------------------------------------
# (d) Aggregate clamps — where physics demands non-negativity
# ---------------------------------------------------------------------------

def test_member_solar_clamps_a_negative_output_to_zero():
    """A negative reading means power flowing INTO this inverter — never its own
    production. The child's production is counted on the child's member, so the
    clamp cannot lose it."""
    parent = _inverter(topology=WIRING_TOPOLOGY_SERIES, output=_pv_watts(-1500.0, None, None))
    assert member_solar(parent, V) == 0.0
    parallel_parent = _inverter(output=_pv_watts(-1500.0, None, None))
    assert member_solar(parallel_parent, V) == 0.0


def test_cascade_solar_total_counts_the_child_once():
    """Parent hybrid reads −1500 W while its battery absorbs the child's
    1500 W: parent production = −1500 − (−1500) = 0, child = 1500 → 1500 W."""
    parent = _hybrid("parent", topology=WIRING_TOPOLOGY_SERIES, power=-1500.0,
                     output=_pv_watts(-1500.0, None, None))
    child = _inverter("child", output=_pv_watts(1500.0, None, None))
    assert _close(solar_total([parent, child], V), 1500.0)


def test_cascade_solar_total_when_the_backfeed_goes_to_the_grid():
    """Same cascade, battery idle: the 1500 W flows out through the parent to
    the grid. The parent contributes 0 (clamped), the child its full 1500 W."""
    parent = _hybrid("parent", topology=WIRING_TOPOLOGY_SERIES, power=0.0,
                     output=_pv_watts(-1500.0, None, None))
    child = _inverter("child", output=_pv_watts(1500.0, None, None))
    assert _close(solar_total([parent, child], V), 1500.0)


def test_solar_total_never_goes_negative_at_the_aggregate():
    """A measured production sensor can read slightly negative (night-time
    offset, inverter self-consumption). The site total must not."""
    noisy = _inverter(has_solar_entity=True, solar_measured=-50.0)
    assert member_solar_production(noisy, V) == -50.0  # the member reports what it reads
    assert solar_total([noisy], V) == 0.0

    other = _inverter("other", has_solar_entity=True, solar_measured=-70.0)
    assert solar_total([noisy, other], V) == 0.0


def test_solar_total_stays_none_when_nothing_is_known():
    """None is the caller's cue to fall back to grid export + charging draw —
    the aggregate clamp must not turn that into a hard 0."""
    assert solar_total([_inverter()], V) is None
    assert solar_total([], V) is None


def test_solar_total_unaffected_for_ordinary_positive_readings():
    a = _inverter("a", has_solar_entity=True, solar_measured=3000.0)
    b = _inverter("b", output=_pv_watts(2000.0, None, None))
    assert _close(solar_total([a, b], V), 5000.0)


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
