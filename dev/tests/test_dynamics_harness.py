"""The dynamics harness closes its loop through the production site cycle.

Machine-authored tests - not yet human-reviewed.

dev/tests/dynamics.py screens control-loop changes at a thousand times wall
clock, so what it gets wrong about production, every tuning decision made on
it inherits. Until 2026-09-24 it assembled the cycle itself: its own call to
the grid EMA, then the managed draws subtracted RAW. Production had smoothed
the draw on the grid's EMA since 201e2bb - and advanced that EMA twice a cycle
until 4cbbdd1, which permitted a charger ramping on a breaker-limited phase up
to 414 W past its allowance. The harness could see neither the design nor the
bug: whatever production did to the draw, the harness read it raw.

It now drives ``run_hub_calculation`` itself (dynamics.Engine), so the input
EMAs, the managed-draw subtraction and the Excess latch are production's, in
production's order, advanced as often as production advances them. These
tests hold it to that on the ramp 4cbbdd1 was about (dynamics.RampSim, the
same loop dev/tests/test_managed_draw_smoothing.py closes through Home
Assistant, and the same figures to the watt).

And to the rig's station ring (8151a69): a station with no AC output sensor,
booked by production's builder at its command, its register written behind
the command gate, on the rig's 5 s instrument clock (dynamics.Sim).

Pure Python, no Home Assistant dependencies. Runnable two ways:
  python3 dev/tests/test_dynamics_harness.py   (standalone, no pytest needed)
  pytest dev/tests/test_dynamics_harness.py    (CI tier)
"""

import sys
from collections import Counter
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dynamics  # noqa: E402 - loads the pure modules on import

from custom_components.dynamic_ocpp_evse.const import (  # noqa: E402
    STATION_CHARGE_POWER_STEP as STEP_W,
)
from custom_components.dynamic_ocpp_evse.engine import (  # noqa: E402
    hub_calculation,
)

V = dynamics.V
# One step of the permit's own 0.1 A rounding (entities/load.py).
BUDGET_W = 0.1 * V
RAMPS = (("car at 1 A/s", 1.0), ("instant step", 100.0))


@contextmanager
def _managed_draw_filter(variant):
    """Swap production's managed-draw smoothing for a known-wrong one.

    ``twice``: the EMA advanced twice a cycle, the site view reading the first
    step - what run_hub_calculation did from 201e2bb until 4cbbdd1. ``raw``: no
    smoothing at all - what this harness subtracted until 2026-09-24.
    """
    real = hub_calculation._managed_phase_draws

    def twice(site, ema_inputs=None):
        first = real(site, ema_inputs)
        real(site, ema_inputs)
        return first

    def raw(site, ema_inputs=None):
        return real(site, None)

    hub_calculation._managed_phase_draws = {"twice": twice, "raw": raw}[variant]
    try:
        yield
    finally:
        hub_calculation._managed_phase_draws = real


def test_a_ramping_charger_holds_its_allowance_through_the_harness():
    """Production, through the harness: the permit never passes 17 A.

    With 4cbbdd1's engine change reverted in the working tree this fails at
    414 W over (car at 1 A/s) and 690 W (instant step) - the harness is what
    sees it, unmodified.
    """
    for label, rate in RAMPS:
        run = dynamics.ramp(rate)
        assert run["permit_over"] <= BUDGET_W, (
            f"{label}: permit {run['permit_over']:.0f} W over the allowance"
        )
        assert run["site_over"] <= BUDGET_W, (
            f"{label}: site {run['site_over']:.0f} W over the breaker"
        )
        # And not by holding the car back: it ramped all the way up.
        assert run["final_draw"] >= run["allowance"] - 0.1, label


def test_the_harness_sees_a_draw_filter_that_runs_ahead_of_the_grid():
    """Break the draw's smoothing and the harness's ramp overshoots.

    Measured through this harness, W over the allowance at peak (permit / site
    import over the breaker) - the same table 4cbbdd1 measured through Home
    Assistant:

    =================================  ============  ============
    managed draw                       car 1 A/s     instant step
    =================================  ============  ============
    smoothed, advanced twice a cycle   414 / 345     690 / 414
    raw (the harness until now)        1035 / 897    2737 / 1380
    smoothed, once a cycle             0 / 0         0 / 0
    =================================  ============  ============

    The raw row is the point. A harness that subtracts raw draws reports that
    overshoot whatever production does, so it could not tell the bug from the
    fix; this one reports production's. (The raw variant also makes the engine
    log "household -> 0 after feedback" as the car ramps: a raw draw outrunning
    the smoothed grid is exactly what that warning is for.)
    """
    for variant in ("twice", "raw"):
        with _managed_draw_filter(variant):
            for label, rate in RAMPS:
                run = dynamics.ramp(rate)
                # Two budgets would already be a failure; this asks for ten, so
                # the overshoot is the filter and not the rounding.
                assert run["permit_over"] > 10 * BUDGET_W, (
                    f"{variant}, {label}: only {run['permit_over']:.0f} W over"
                )


class _CountingEma(dict):
    """The hub's input-EMA dict, counting how often each key is written."""

    def __init__(self):
        super().__init__()
        self.writes = Counter()

    def __setitem__(self, key, value):
        self.writes[key] += 1
        super().__setitem__(key, value)


def test_every_input_ema_advances_once_per_cycle():
    """No EMA state is stepped twice in one run_hub_calculation.

    The general form of 4cbbdd1's bug, so the next filter wired in twice fails
    here too without anyone having to think of its ramp: the managed draw's
    three keys were each written twice a cycle, one per view that subtracted
    them.
    """
    sim = dynamics.RampSim(car_ramp_a_s=1.0)
    ema = _CountingEma()
    # run_hub_calculation takes the dict it finds (setdefault), so it keeps
    # this one - production's own state, only watched.
    sim.engine.runtime["_ema_inputs"] = ema
    seen = set()
    for _ in range(sim.plug_in_cycle + 20):
        ema.writes.clear()
        sim.step()
        twice = {key: n for key, n in ema.writes.items() if n > 1}
        assert not twice, f"cycle {sim.cycle - 1}: advanced more than once: {twice}"
        seen.update(ema.writes)
    # Not vacuous: the grid and the managed draw were both being smoothed.
    assert {"grid_0", "managed_draw_0"} <= seen, sorted(seen)


def _late_rings(tau, phases=dynamics.TICK_PHASES, **kw):
    """Register peak-to-peak on a flat input, mean of the second half's 30 s
    windows (the rig's measure), at every phase of the 5 s instrument tick -
    which a rig restart lands at random."""
    with dynamics.permit_tau(tau):
        return [
            dynamics.fixed_point_ring(tick_phase_s=phase, **kw)[0]
            for phase in phases
        ]


def test_the_rigs_station_rings_under_a_short_permit_filter_and_not_at_7_s():
    """dynamics.Sim's defaults are the rig's station, and they ring like it.

    Register peak-to-peak on a flat 14.7 kW, W, second half. The rig (8151a69,
    one value per cold run) beside the harness (mean / worst of five phases):

    ========  ===========  =============
    tau       rig          harness
    ========  ===========  =============
    1.0 s     300          290 / 625
    2.0 s     150, 250     190 / 400
    7.0 s     0, 0, 0      0 / 0
    ========  ===========  =============

    Until 2026-09-24 the harness showed no ring from 1.5 s up, because it
    booked the station at what it drew and commanded it every cycle.
    """
    for tau in (1.0, 2.0):
        rings = _late_rings(tau)
        assert sum(rings) / len(rings) >= STEP_W, f"tau {tau}: {rings}"
    rings = _late_rings(7.0)
    assert max(rings) < STEP_W, f"tau 7.0 rings: {rings}"


def test_neither_the_booked_command_nor_the_rigs_clock_rings_alone():
    """The ring needs the command booked AND the rig's clock under it.

    Read through both AC sensors (metered), the station on the same clock is
    booked at what its AC input shows, which lags as the CTs do, and nothing
    rings. Booked at its command but on the old continuous clock (CTs a pure
    7 s delay, the converter every cycle), it does not ring either - the
    harness's blind spot until 2026-09-24.
    """
    # The continuous clock has no tick to be out of phase with: one run.
    for kw in (dict(station_metered=True), dict(tick_s=None, phases=(0.0,))):
        rings = _late_rings(1.0, **kw)
        assert max(rings) < STEP_W, f"{kw}: {rings}"


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
    print(f"\n{'FAILED' if failed else 'OK'} - {len(failed)} failure(s)")
    sys.exit(1 if failed else 0)
