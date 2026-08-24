"""The Excess stay-on rule — calculations.excess_margin under a running load.

Machine-authored tests — not yet human-reviewed.

ISSUES.md #41. The rule: *if turning a load off would re-trigger Excess, it
stays on.* Mechanically that is one identity —

    margin read while the load runs == margin the same site would read with
    the load switched off

— and it must hold no matter HOW the inverter served the load: by exporting
less, or by charging the battery less. Where it failed, an engaged load
suppressed the very verdict that engaged it and the relay cycled every cycle.

These tests build the *physical* meter readings for a running load, push them
through the real reconstruction the engine applies (``grid_without_managed_draws``,
the pure core of ``_apply_feedback_loop``) and compare the margin against the
same site with the load off. The identity is the assertion; the individual
numbers are only there to show the sums.

Pure Python, no Home Assistant dependencies. Runnable two ways:
  python3 dev/tests/test_excess_stayon.py     (standalone, no pytest needed)
  pytest dev/tests/test_excess_stayon.py      (Docker / CI tier)
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
    PhaseValues,
    SiteContext,
)
from custom_components.dynamic_ocpp_evse.calculations.target_calculator import (  # noqa: E402
    excess_margin,
    reconstructed_export_power,
)
from custom_components.dynamic_ocpp_evse.calculations.utils import (  # noqa: E402
    grid_without_managed_draws,
)
from custom_components.dynamic_ocpp_evse.const.hub import (  # noqa: E402
    DEFAULT_EXCESS_HYSTERESIS,
)

V = 230.0
CHARGE_MAX = 5000.0   # battery charge allowance
SOC_FULL = 97.0
THRESHOLD = 13000.0   # export allowance (grid export limit − trigger margin)


def _plug(watts, phase="A"):
    """A binary managed load drawing ``watts`` on one site phase."""
    return LoadContext(
        load_id="plug",
        entity_id="plug",
        min_current=0,
        max_current=watts / V,
        phases=1,
        active_phases_mask=phase,
        l1_current=watts / V,
        l1_phase=phase,
        device_type="plug",
    )


def _site(grid_a, grid_b=None, grid_c=None, battery_w=None, soc=60.0,
          charge_max=CHARGE_MAX, threshold=THRESHOLD, loads=(), off_grid=False):
    """A site from SIGNED per-phase meter readings (+ import, − export, Amps).

    ``battery_w`` follows the site convention: positive discharging, negative
    charging. The readings are what the CTs physically show with ``loads``
    running — the reconstruction below is what the engine does to them.
    """
    signed = (grid_a, grid_b, grid_c)
    cons = [None if g is None else max(0.0, g) for g in signed]
    exp = [None if g is None else max(0.0, -g) for g in signed]
    return SiteContext(
        voltage=V,
        consumption=PhaseValues(*cons),
        export_current=PhaseValues(*exp),
        battery_power=battery_w,
        battery_soc=soc,
        battery_soc_full=SOC_FULL,
        battery_soc_target=80.0,
        battery_max_charge_power=charge_max,
        excess_export_threshold=threshold,
        is_off_grid=off_grid,
        loads=list(loads),
    )


def _apply_feedback(site):
    """Take the managed draws off the grid readings, as the engine does.

    The pure core of ``_apply_feedback_loop`` — ``grid_without_managed_draws``.
    """
    draws = [0.0, 0.0, 0.0]
    for c in site.loads:
        a, b, cc = c.get_site_phase_draw()
        draws[0] += a
        draws[1] += b
        draws[2] += cc
    if not site.is_off_grid and any(d > 0 for d in draws):
        site.consumption, site.export_current = grid_without_managed_draws(
            site.consumption, site.export_current, tuple(draws)
        )
    return site


def _margin(site, hysteresis=0.0):
    """Reconstruct the load-off grid view, then read the margin.

    Mirrors the engine's order: _apply_feedback_loop() (whose arithmetic is
    grid_without_managed_draws) runs first, excess_margin() second.
    """
    return excess_margin(_apply_feedback(site), hysteresis)


def _close(a, b, tol=1e-6):
    return abs(a - b) < tol


# ---------------------------------------------------------------------------
# The identity: an engaged load reads its own load-off margin
# ---------------------------------------------------------------------------

def test_export_displaced_load_reads_its_load_off_margin():
    """The easy case: the load's 2 kW came out of what was being exported.

    Load off: 13.5 kW export + 5 kW charging against an 18 kW allowance = +500 W.
    Load on: the meter shows 2 kW less export, charging unchanged.
    """
    off = _margin(_site(-13500 / V, battery_w=-CHARGE_MAX))
    on = _margin(_site(-11500 / V, battery_w=-CHARGE_MAX, loads=[_plug(2000)]))
    assert _close(off, 500.0)
    assert _close(on, off)


def test_battery_displaced_load_reads_its_load_off_margin():
    """The inverter served the load by charging the battery 2 kW slower, so the
    METER NEVER MOVED — only battery_power did.

    The draw still has to come back exactly once. Here the phase is exporting,
    so the reconstruction has two consistent halves that cancel: the freed 2 kW
    goes back onto the charge term (3 kW → 5 kW) and its per-phase demand comes
    off the export side (15.5 kW → 13.5 kW). Same 18.5 kW the site placed with
    the load off.
    """
    off = _margin(_site(-13500 / V, battery_w=-CHARGE_MAX))
    on = _margin(_site(-13500 / V, battery_w=-3000.0, loads=[_plug(2000)]))
    assert _close(off, 500.0)
    assert _close(on, off)


def test_battery_displaced_on_an_importing_phase_reads_its_load_off_margin():
    """The geometry that actually cycled in the field (ISSUES.md #41).

    Symmetric inverter, unbalanced household: 10 A on phase A, 10 A/phase of
    solar, the battery taking 4.6 kW of its 5 kW allowance. The meter reads
    +6.67 A on A and −3.33 A on B and C, so the site exports 1533 W — 133 W
    past a 1 kW allowance, and Excess engages.

    The plug's 2 kW then comes out of the charge rate (2.6 kW). Subtracting the
    draw from phase A cannot show up as export because that phase still reads
    net import — it clamps at zero — while the charge term drops the whole
    2 kW, so the margin read −533 W with the plug running against +133 W with
    it off. That 667 W step is the on/off cycling. Both states must read +133 W.
    """
    off = _margin(_site(6.667, -3.333, -3.333, battery_w=-4600.0, threshold=1000.0))
    on = _margin(
        _site(12.464, -6.232, -6.232, battery_w=-2600.0, threshold=1000.0,
              loads=[_plug(2000)])
    )
    assert off > 0 and _close(off, 133.2, tol=0.3)
    assert _close(on, off, tol=0.5)


def test_the_importing_phase_error_stays_zero_at_every_draw_size():
    """Why the hysteresis band cannot be the answer to the case above.

    The step is the part of the draw the clamped phase could not show as
    export, net of the extra export the battery's slower charging frees on the
    other phases — which works out to draw/phases. A 1 kW load steps 333 W, a
    3 kW load steps 1000 W: it outgrows any fixed band (500 W by default) as
    soon as the load passes phases × band. Completing the reconstruction leaves
    no step at all, whatever the load draws.
    """
    off = _margin(_site(6.667, -3.333, -3.333, battery_w=-4600.0, threshold=1000.0))
    for watts in (1000.0, 2000.0, 3000.0, 4000.0):
        drop = watts / 3 / V          # slower charging, spread over three phases
        on = _margin(
            _site(6.667 - drop + watts / V, -3.333 - drop, -3.333 - drop,
                  battery_w=-(4600.0 - watts), threshold=1000.0,
                  loads=[_plug(watts)])
        )
        assert _close(on, off, tol=0.5), f"{watts} W draw stepped the margin"


def test_the_draw_is_counted_exactly_once_at_every_size():
    """No double count and no leak, swept: whatever the load draws, the margin
    it reads is the margin of the site without it. Export displacement, so the
    meter moves by the whole draw each time."""
    off = _margin(_site(-13500 / V, battery_w=-CHARGE_MAX))
    for watts in (500.0, 2000.0, 5000.0, 9000.0, 13000.0):
        on = _margin(
            _site((-13500 + watts) / V, battery_w=-CHARGE_MAX, loads=[_plug(watts)])
        )
        assert _close(on, off), f"{watts} W draw shifted the margin"


def test_two_loads_on_different_phases_still_read_the_load_off_margin():
    """Several managed loads, several phases — one identity, no per-load bias."""
    off = _margin(_site(-20.0, -20.0, -20.0, battery_w=-CHARGE_MAX, threshold=3000.0))
    on_site = _site(
        -20.0 + 2000 / V, -20.0, -20.0 + 3000 / V,
        battery_w=-CHARGE_MAX, threshold=3000.0,
        loads=[_plug(2000, "A"), _plug(3000, "C")],
    )
    assert _close(_margin(on_site), off)


# ---------------------------------------------------------------------------
# Off-grid: the probe path must not move
# ---------------------------------------------------------------------------

def test_off_grid_probe_is_unchanged():
    """Off-grid there is no meter, so excess_margin() adds the managed draws
    itself and each load acts as a probe on a curtailing inverter. These three
    readings are the contract, unchanged by anything above.
    """
    # Idle, battery at its 5 kW allowance: exactly at the trigger.
    assert _close(_margin(_site(0.0, battery_w=-CHARGE_MAX, off_grid=True)), 0.0)
    # A 2 kW load whose power came entirely out of the charge rate: still 0 —
    # there was no surplus, and the battery keeps charging, only slower.
    assert _close(
        _margin(_site(0.0, battery_w=-3000.0, off_grid=True, loads=[_plug(2000)])),
        0.0,
    )
    # The inverter ramped up to serve the load and charging held at 5 kW: the
    # margin reports the 2 kW of surplus that was previously curtailed.
    assert _close(
        _margin(_site(0.0, battery_w=-CHARGE_MAX, off_grid=True, loads=[_plug(2000)])),
        2000.0,
    )


def test_off_grid_readings_are_not_touched_by_the_reconstruction():
    """The synthetic zeros never contained the draws; subtracting would
    fabricate export equal to the load's own consumption."""
    site = _site(0.0, battery_w=-CHARGE_MAX, off_grid=True, loads=[_plug(2000)])
    _margin(site)
    assert site.export_current.total == 0.0
    assert site.consumption.total == 0.0


# ---------------------------------------------------------------------------
# Hysteresis: the release band once engaged
# ---------------------------------------------------------------------------

def _verdict(prev_on, site, hysteresis):
    """The engine's latch: the band widens only while Excess was already on."""
    margin = _margin(site, hysteresis if prev_on else 0.0)
    return margin >= 0, margin


def test_hysteresis_holds_an_engaged_load_below_the_trigger():
    """200 W short of the trigger: never engages from off, stays on once on."""
    def fresh():
        return _site(-12800 / V, battery_w=None, soc=None, charge_max=None)

    on_from_off, margin_off = _verdict(False, fresh(), DEFAULT_EXCESS_HYSTERESIS)
    assert not on_from_off and _close(margin_off, -200.0)
    stays_on, margin_on = _verdict(True, fresh(), DEFAULT_EXCESS_HYSTERESIS)
    assert stays_on and _close(margin_on, 300.0)


def test_engaged_load_releases_once_the_surplus_leaves_the_band():
    """The release point: an engaged load holds while the site is within the
    hysteresis of the trigger and lets go one watt past it. 500 W band, so
    12.5 kW of export against a 13 kW allowance is the edge."""
    band = DEFAULT_EXCESS_HYSTERESIS

    def site_at(export_w):
        return _site(-export_w / V, battery_w=None, soc=None, charge_max=None)

    # Exactly at the edge — allowance shrunk to 12.5 kW, margin 0, still on.
    assert _verdict(True, site_at(THRESHOLD - band), band)[0]
    # One watt further down and the engaged load releases.
    assert not _verdict(True, site_at(THRESHOLD - band - 1), band)[0]


def test_a_custom_hysteresis_value_is_honored():
    """The band is a hub setting now, not a constant: a wider band holds a
    load through a deeper dip, a zero band releases at the trigger itself."""
    def site_at(export_w):
        return _site(-export_w / V, battery_w=None, soc=None, charge_max=None)

    deep = site_at(THRESHOLD - 1500)
    assert _verdict(True, deep, 2000.0)[0]        # 2 kW band holds it
    assert not _verdict(True, deep, 500.0)[0]     # the default band does not
    assert _close(_verdict(True, site_at(THRESHOLD), 0.0)[1], 0.0)
    assert not _verdict(True, site_at(THRESHOLD - 1), 0.0)[0]


def test_hysteresis_cannot_manufacture_a_pool_that_does_not_exist():
    """Off-grid with a full battery has no allowance to shrink — the clamp keeps
    the margin at 0 instead of reporting 500 W of power the site does not have."""
    site = _site(0.0, battery_w=0.0, soc=98.0, off_grid=True)
    assert _close(_margin(site, DEFAULT_EXCESS_HYSTERESIS), 0.0)


# ---------------------------------------------------------------------------
# Export stays gross and per-phase clamped
# ---------------------------------------------------------------------------

def test_import_on_one_phase_buys_no_export_headroom_on_another():
    """An export limit is physical and contractual per exported flow. A site
    pushing 10 A out on two phases while pulling 10 A in on the third IS
    exporting 20 A — the import does not net it away, so the same gross export
    reads the same margin whatever the third phase is doing."""
    both = _margin(_site(10.0, -10.0, -10.0, battery_w=None, soc=None,
                         charge_max=None, threshold=3000.0))
    exporting_only = _margin(_site(0.0, -10.0, -10.0, battery_w=None, soc=None,
                                   charge_max=None, threshold=3000.0))
    assert _close(both, 20.0 * V - 3000.0)
    assert _close(both, exporting_only)


def test_a_saturated_battery_gives_the_plain_gross_reading():
    """The load-off reconstruction only moves power the battery could actually
    take. Full, absent or already at its charge limit, there is no headroom and
    the margin is the plain gross export plus charging — nothing is added."""
    saturated = _margin(_site(-13500 / V, battery_w=-CHARGE_MAX, loads=[_plug(2000)]))
    full = _margin(_site(-13500 / V, battery_w=0.0, soc=98.0, loads=[_plug(2000)]))
    no_battery = _margin(_site(-13500 / V, battery_w=None, soc=None,
                               charge_max=None, loads=[_plug(2000)]))
    # 13.5 kW export + 2 kW folded back by the feedback loop = 15.5 kW gross.
    assert _close(saturated, 15500.0 + CHARGE_MAX - (THRESHOLD + CHARGE_MAX))
    assert _close(full, 15500.0 - THRESHOLD)
    assert _close(no_battery, 15500.0 - THRESHOLD)


# ---------------------------------------------------------------------------
# The clipping window: the allowance is what the battery is PERMITTED to take
# ---------------------------------------------------------------------------
#
# ``battery_max_charge_power`` is the rate the battery may take, which is the
# NAMEPLATE rate only while nothing is holding it back. When the PV clipping
# forecast has our charge control holding the register at 6.5 kW of a 10 kW
# rating, the missing 3.5 kW is not somewhere the site can put production, and
# an allowance that still counts it reads a clipping window — the one moment the
# site has surplus it cannot place — as a site with room to spare.
#
# The numbers are a real site's: 8.7 kW export limit, 500 W trigger margin (so
# the Excess threshold is 8.2 kW), a 10 kW battery rating and the 6.5 kW the
# forecast settles the charge limit at.

LIVE_EXPORT_LIMIT = 8700.0
LIVE_THRESHOLD = LIVE_EXPORT_LIMIT - 500.0  # export limit − trigger margin
NAMEPLATE = 10000.0
ENFORCED = 6500.0


def _clipping_site(charge_max, battery_w=-ENFORCED, export=LIVE_EXPORT_LIMIT, loads=()):
    """Midday, export pinned at the hard limit, the array able to give more."""
    return _site(-export / V, battery_w=battery_w, soc=70.0,
                 charge_max=charge_max, threshold=LIVE_THRESHOLD, loads=list(loads))


def test_the_nameplate_allowance_reads_a_clipping_window_as_no_surplus():
    """The bug, kept as the contrast. 8.7 kW is leaving the site and 6.5 kW is
    going into the battery, every watt the site can place — and against an
    allowance built from the 10 kW rating the control is actively forbidding
    that reads as 3 kW short of Excess, for the whole window."""
    assert _close(_margin(_clipping_site(NAMEPLATE)), -3000.0)


def test_the_enforced_allowance_engages_excess_in_the_clipping_window():
    """The same site, the same instant, the allowance the battery is permitted:
    the margin is the 500 W the site is genuinely placing beyond the threshold,
    Excess is on, and the surplus goes into a load instead of being curtailed."""
    assert _close(_margin(_clipping_site(ENFORCED)), 500.0)


def test_an_engaged_load_is_not_dropped_when_the_cap_engages():
    """The drop-on-engage case, reversed.

    A 2 kW plug is running on displaced export, Excess engaged. Then the forecast
    cap engages under it: the battery goes from its 10 kW rating to 6.5 kW, and
    since export is already pinned at the hard limit the 3.5 kW it stops taking is
    curtailed rather than exported — the readings do not move at all. Nothing
    about the site got worse, so the verdict must not move either.
    """
    displaced = -(LIVE_EXPORT_LIMIT - 2000) / V
    running = dict(soc=70.0, threshold=LIVE_THRESHOLD, loads=[_plug(2000)])
    before = _site(displaced, battery_w=-NAMEPLATE, charge_max=NAMEPLATE, **running)
    after = _site(displaced, battery_w=-ENFORCED, charge_max=ENFORCED, **running)
    # Counting the forbidden 3.5 kW is what used to drop the load mid-window.
    stuck = _site(displaced, battery_w=-ENFORCED, charge_max=NAMEPLATE, **running)

    assert _close(_margin(before, DEFAULT_EXCESS_HYSTERESIS), 1000.0)
    assert _close(_margin(after, DEFAULT_EXCESS_HYSTERESIS), 1000.0)
    assert _margin(stuck, DEFAULT_EXCESS_HYSTERESIS) < 0


def test_the_enforced_allowance_keeps_the_load_off_identity():
    """The stay-on identity is what makes the narrowed allowance safe: a battery
    sitting on an enforced limit has no headroom, so the load's draw stays on the
    export side of the reconstruction and the running site reads exactly the
    margin the idle one does. No new flapping can come out of this."""
    idle = _clipping_site(ENFORCED)
    running = _clipping_site(ENFORCED, export=LIVE_EXPORT_LIMIT - 2000,
                             loads=[_plug(2000)])
    assert _close(_margin(running), _margin(idle))


# ---------------------------------------------------------------------------
# The same reconstruction, read as a steering signal
# ---------------------------------------------------------------------------
#
# The forecast's charge-limit advice carries a slow integral trim steered by
# RECONSTRUCTED export against (export limit − trigger margin). That is only
# safe to close a loop on because of the identity above: our own loads' draw is
# credited back, so a car charging on the surplus is not read as an export
# shortfall and cannot steer the battery's charge limit. These pin that
# invariance on the same geometries, through the same reconstruction.


def _reconstructed(site):
    """The load-off export figure, after the engine's feedback loop."""
    _apply_feedback(site)
    return reconstructed_export_power(site)


def test_reconstructed_export_ignores_an_export_displaced_load():
    off = _reconstructed(_site(-13500 / V, battery_w=-CHARGE_MAX))
    on = _reconstructed(
        _site(-11500 / V, battery_w=-CHARGE_MAX, loads=[_plug(2000)])
    )
    assert _close(off, 13500.0)
    assert _close(on, off)


def test_reconstructed_export_ignores_a_battery_displaced_load():
    # The meter never moved — only battery_power did — and the reconstruction
    # still lands on the same export the site would show with the plug off.
    off = _reconstructed(_site(-13500 / V, battery_w=-CHARGE_MAX))
    on = _reconstructed(
        _site(-13500 / V, battery_w=-3000.0, loads=[_plug(2000)])
    )
    assert _close(on, off)


def test_reconstructed_export_ignores_a_load_on_the_importing_phase_geometry():
    # The geometry that cycled in the field, and the one a naive export reading
    # would have handed the trim as a 2 kW error.
    off = _reconstructed(
        _site(6.667, -3.333, -3.333, battery_w=-4600.0, threshold=1000.0)
    )
    on = _reconstructed(
        _site(12.464, -6.232, -6.232, battery_w=-2600.0, threshold=1000.0,
              loads=[_plug(2000)])
    )
    assert _close(on, off, tol=1.0)


def test_reconstructed_export_ignores_an_engaged_load_in_a_clipping_window():
    # The trim's own operating point: the charge cap engaged, export pinned at
    # the limit, an Excess load running on the surplus.
    idle = _reconstructed(_clipping_site(ENFORCED))
    running = _reconstructed(
        _clipping_site(ENFORCED, export=LIVE_EXPORT_LIMIT - 2000,
                       loads=[_plug(2000)])
    )
    assert _close(idle, LIVE_EXPORT_LIMIT)
    assert _close(running, idle)


def test_reconstructed_export_still_sees_the_household():
    # The other half of the property: unmanaged draw is NOT credited back, so a
    # real house step is a real error — the trim's slowness is what keeps a
    # kettle off the register, not blindness to it.
    quiet = _reconstructed(_site(-13500 / V, battery_w=-CHARGE_MAX))
    kettle = _reconstructed(_site(-11500 / V, battery_w=-CHARGE_MAX))
    assert _close(quiet - kettle, 2000.0)


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
