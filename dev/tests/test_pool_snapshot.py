"""The pool snapshot — what the Overview page and the diagnostics dump show.

Machine-authored tests — not yet human-reviewed.

Every watt figure the publisher shows for a pool is RE-DERIVED from the site's
headroom terms: ``available_grid_power``, ``available_solar_power`` and the
rest are computed a second time in ``engine/hub_result.py`` from
``grid_headroom``, ``solar_available`` and friends. The allocator meanwhile
works from three ``PhaseConstraints`` objects built in
``calculations/target_calculator.py``. Two derivations of the same quantity is
two chances to be right, and through the Excess over-commitment of 2026-09-07
they were not: the page reported a surplus the distribution never saw.

``SiteContext.pool_snapshot`` closes that by publishing the pools THEMSELVES —
per phase, per combination, with the basis flag — so a dump answers "what did
the allocator actually have?" instead of "what would this arithmetic say?".

Two things about it are easy to misread, and both are pinned below:

* **The basis.** The Excess pool nets (an importing phase cancels an exporting
  one); every other pool is gross (each phase's flow stands alone). The same
  seven fields mean different things under each, so the flag travels with the
  snapshot.
* **"Left" is measured, not permitted.** The pools are deducted by each load's
  real footprint (``_pool_deduction``), so an untouched pool beside a granted
  permit is the normal reading for an idle plug — not a missed deduction.

Pure Python, no Home Assistant dependencies. Runnable two ways:
  python3 dev/tests/test_pool_snapshot.py   (standalone, no pytest)
  pytest dev/tests/test_pool_snapshot.py    (Docker / CI tier)
"""

import sys
from dataclasses import fields as dataclass_fields
from pathlib import Path

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
    calculate_all_load_targets,
)

_POOLS = ("physical", "solar", "excess")


def _site(consumption, export, loads=(), **kwargs):
    """A site with sane defaults; only what a test cares about is passed in."""
    site = SiteContext(
        consumption=PhaseValues(*consumption),
        export_current=PhaseValues(*export),
        main_breaker_rating=kwargs.pop("main_breaker_rating", 25),
        voltage=230,
        loads=list(loads),
        **kwargs,
    )
    calculate_all_load_targets(site)
    return site


def _evse(**kwargs):
    return LoadContext(
        load_id="evse", entity_id="evse", min_current=6, max_current=16,
        phases=3, **kwargs,
    )


def _plug(**kwargs):
    return LoadContext(
        load_id="plug", entity_id="plug", min_current=8, max_current=8,
        phases=1, device_type="plug", **kwargs,
    )


def test_the_snapshot_names_only_the_phases_the_site_has():
    """A single-phase snapshot must not read as a three-phase site with two
    dead legs — the pools carry a 0.0 for a phase that does not exist, which is
    indistinguishable from a phase with nothing spare."""
    assert _site((5.0, 5.0, 5.0), (0.0, 0.0, 0.0)).pool_snapshot["phases"] == "ABC"
    assert _site((4.0, None, None), (0.0, None, None)).pool_snapshot["phases"] == "A"
    # A B+C-only installation is explicitly supported: the phases are not a
    # prefix of A/B/C, so the letters cannot be derived from a count.
    assert _site((None, 4.0, 4.0), (None, 0.0, 0.0)).pool_snapshot["phases"] == "BC"


def test_every_pool_the_allocator_uses_carries_every_field():
    """Asserted against ``PhaseConstraints``' own fields rather than a literal
    list, so a new field cannot be added without reaching the dump."""
    expected = {f.name for f in dataclass_fields(PhaseConstraints)}
    snapshot = _site((5.0, 5.0, 5.0), (0.0, 0.0, 0.0), [_evse()]).pool_snapshot
    for pool in _POOLS:
        for phase in ("start", "left"):
            assert set(snapshot[pool][phase]) == expected, (pool, phase)


def test_the_excess_pool_is_flagged_net_and_the_others_gross():
    """The basis is not cosmetic: under netting the total is the algebraic sum,
    so a reader who assumes gross will add an importing phase's deficit to the
    surplus instead of subtracting it."""
    snapshot = _site((5.0, 5.0, 5.0), (0.0, 0.0, 0.0), [_evse()]).pool_snapshot
    assert snapshot["excess"]["start"]["netting"] is True
    assert snapshot["excess"]["left"]["netting"] is True
    for pool in ("physical", "solar"):
        assert snapshot[pool]["start"]["netting"] is False, pool
        assert snapshot[pool]["left"]["netting"] is False, pool


def test_left_is_what_survived_the_measured_draws():
    """The whole point of publishing "left": it is the answer to "why did the
    last load get nothing?".

    A SETTLED EVSE holding 8 A below its 16 A permit is footprint-accounted at
    8 A, and the 8 A it declined stays in the pool for lower-ranked loads —
    that gap is exactly what "left" exists to show.
    """
    site = _site(
        (5.0, 5.0, 5.0), (0.0, 0.0, 0.0),
        [_evse(l1_current=8, l2_current=8, l3_current=8, draw_settled=True)],
    )
    physical = site.pool_snapshot["physical"]
    for phase in ("A", "B", "C"):
        assert physical["left"][phase] == round(physical["start"][phase] - 8, 2), phase
    assert physical["left"]["ABC"] == round(physical["start"]["ABC"] - 24, 2)


def test_an_unsettled_evse_holds_its_whole_permit_in_the_pool_view():
    """The counterpart, and the reason "left" is labelled "after measured
    draws" rather than "after allocations": while an EVSE's draw is still
    tracking our ramping permit it is not a ceiling, so the engine reserves the
    full permit and "left" shows the permit gone, not the 8 A being drawn."""
    site = _site(
        (5.0, 5.0, 5.0), (0.0, 0.0, 0.0),
        [_evse(l1_current=8, l2_current=8, l3_current=8)],
    )
    permit = site.loads[0].available_current
    physical = site.pool_snapshot["physical"]
    assert permit == 16
    assert physical["left"]["A"] == round(physical["start"]["A"] - permit, 2)


def test_an_idle_load_takes_nothing_however_large_its_permit():
    """A switched-off plug holds a permit and draws nothing, so the pool is
    untouched. Left == start beside a non-zero permit is CORRECT, and the page
    says "after measured draws" so it does not read as a missed deduction."""
    site = _site((4.0, None, None), (2.0, None, None), [_plug()])
    assert site.loads[0].available_current > 0      # it was permitted
    assert site.loads[0].allocated_current == 0     # and it is drawing nothing
    physical = site.pool_snapshot["physical"]
    assert physical["left"] == physical["start"]


def test_a_site_with_no_loads_still_reports_its_pools():
    """The pools are built before the loads are looked at, which is what makes
    the snapshot useful on a site where nothing is running: it shows whether
    there was anything to run ON."""
    snapshot = _site((5.0, 5.0, 5.0), (0.0, 0.0, 0.0)).pool_snapshot
    for pool in _POOLS:
        assert snapshot[pool]["start"] == snapshot[pool]["left"], pool
    assert snapshot["physical"]["start"]["ABC"] > 0


def test_every_distribution_mode_hands_its_pools_back():
    """Each of the four modes returns what it did not spend, and the snapshot
    is built from that. A mode that forgot to return would zip against None and
    raise — this pins the deduction actually arriving, mode by mode."""
    for mode in ("priority", "shared", "strict", "optimized"):
        site = _site(
            (5.0, 5.0, 5.0), (0.0, 0.0, 0.0),
            [_evse(l1_current=8, l2_current=8, l3_current=8, draw_settled=True)],
            distribution_mode=mode,
        )
        physical = site.pool_snapshot["physical"]
        assert physical["left"]["ABC"] == round(physical["start"]["ABC"] - 24, 2), mode


def test_the_asymmetric_inverter_pool_keeps_its_shared_total():
    """``from_pool`` puts the SHARED total in the two-phase fields rather than
    a sum, because such an inverter can move its output between legs. The
    snapshot must preserve that — it is what tells a reader why the site offers
    a three-phase load more than its per-phase figures suggest."""
    site = _site(
        (2.0, 2.0, 2.0), (5.0, 1.0, 1.0),
        solar_production_total=5000,
        inverter_max_power=6000,
        inverter_max_power_per_phase=2500,
        inverter_supports_asymmetric=True,
    )
    solar = site.pool_snapshot["solar"]["start"]
    assert solar["ABC"] > 0
    # Pooled, not summed: the pair fields hold the total, so they are SMALLER
    # than A + B would be on a symmetric build.
    assert solar["AB"] == solar["ABC"]
    assert solar["A"] + solar["B"] > solar["AB"]


def test_the_snapshot_is_plain_json_safe_data():
    """It goes into hub_data, which the diagnostics dump serialises wholesale.
    A ``PhaseConstraints`` object in there would not survive the JSON encoder."""
    snapshot = _site((5.0, 5.0, 5.0), (0.0, 0.0, 0.0), [_evse()]).pool_snapshot
    import json

    assert json.loads(json.dumps(snapshot)) == snapshot
    for pool in _POOLS:
        for value in snapshot[pool]["start"].values():
            assert isinstance(value, (float, bool)), (pool, value)


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
