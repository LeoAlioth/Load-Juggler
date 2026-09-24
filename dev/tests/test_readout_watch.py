"""Unit tests for the stuck-readout watch - engine/readout_watch.py - and the
blind-mode footprint rule in calculations/target_calculator._pool_deduction.

Machine-authored tests - not yet human-reviewed.

The report: a charger keeps obeying its profiles while its current readout
stops changing, and the engine keeps subtracting a frozen number from the grid
CTs. What the tests pin:

  * a frozen reading that goes on claiming more than the charger may now
    deliver - a cut limit, or a status that says no energy flows - is judged
    stuck, after twice the reading's own longest normal gap and not before;
  * a car drawing a genuinely steady current, and a car drawing LESS than it
    is offered, are never judged stuck, however long the value repeats;
  * a live reading still above a freshly cut limit is not stuck - every new
    value restarts the clock;
  * nothing is judged before the reading's cadence has been learned, the
    offered reading can stand in for it, and neither a suspended stretch nor a
    stuck episode is ever learned as "normal";
  * blind mode ends only after two new values, and assumes the limit in force
    on the legs that were carrying current;
  * a blind charger's footprint is the larger of its allocation and its last
    command: its permits stay inside the breaker like a metered charger's, and
    a cut that has not landed yet frees less than a metered charger's would.

Pure Python, no Home Assistant dependencies. Runnable two ways:
  python3 dev/tests/test_readout_watch.py     (standalone, no pytest needed)
  pytest dev/tests/test_readout_watch.py      (Docker / CI tier)
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from standalone_loader import load_pure_modules  # noqa: E402

load_pure_modules(engine_modules=("readout_watch",))

from custom_components.dynamic_ocpp_evse.engine import readout_watch as rw  # noqa: E402
from custom_components.dynamic_ocpp_evse.calculations.models import (  # noqa: E402
    LoadContext,
    PhaseValues,
    SiteContext,
)
from custom_components.dynamic_ocpp_evse.calculations.target_calculator import (  # noqa: E402
    _pool_deduction,
    calculate_all_load_targets,
)
from custom_components.dynamic_ocpp_evse.const import (  # noqa: E402
    LEG_DRAWING_CURRENT,
    SETTLE_PERMIT_MARGIN,
)

MARGIN = SETTLE_PERMIT_MARGIN
GAP = 10.0      # the learned charger reports a new value every 10 s
CYCLE = 2.0     # the site cycle the watch is driven at


def _legs(amps, phases=3):
    return tuple(float(amps) if i < phases else 0.0 for i in range(3))


def _learned(gap=GAP, start=1000.0, amps=16.0, phases=3):
    """A watch that has seen a healthy reading change every ``gap`` seconds
    while charging at its 16 A limit. Returns (state, now, last_legs)."""
    state = {}
    t = start
    legs = None
    for i in range(rw.MIN_GAPS + 1):
        legs = _legs(amps - 0.1 * i, phases)
        assert rw.observe(
            state, now=t, legs=legs, status="Charging", commanded=16.0,
            margin=MARGIN, drawing_current=LEG_DRAWING_CURRENT,
        ) is None
        t += gap
    assert rw.normal_gap(state) == gap
    return state, t, legs


def _run(state, start, seconds, legs, status="Charging", commanded=16.0,
         companion=None):
    """Drive the watch for ``seconds`` at the site cycle with a frozen value.
    Returns (events, now) - every non-None event with the time it fired."""
    events = []
    t = start
    end = start + seconds
    while t <= end:
        event = rw.observe(
            state, now=t, legs=legs, status=status, commanded=commanded,
            margin=MARGIN, drawing_current=LEG_DRAWING_CURRENT, companion=companion,
        )
        if event is not None:
            events.append((event, t))
        t += CYCLE
    return events, t


# ── Detection of a frozen readout ─────────────────────────────────────────


def test_a_frozen_reading_under_a_cut_limit_is_judged_stuck():
    """The case the owner described: the limit is cut to 8 A, the charger
    obeys, and the reading goes on showing the pre-cut value bit for bit."""
    state, t0, frozen = _learned()

    # Inside two normal gaps: a live reading could still be on its way.
    events, _ = _run(state, t0, rw.GAP_MULTIPLE * GAP - CYCLE, frozen, commanded=8.0)
    assert events == []
    assert not rw.is_stuck(state)

    # Past it: stuck, announced exactly once.
    events, t = _run(state, t0 + rw.GAP_MULTIPLE * GAP, 600, frozen, commanded=8.0)
    assert [e for e, _ in events] == ["entered"], events
    assert events[0][1] - t0 > rw.GAP_MULTIPLE * GAP
    assert rw.is_stuck(state)
    assert state["stuck_value"] == frozen
    assert state["stuck_limit"] == 8.0


def test_the_window_is_the_chargers_own_cadence():
    """A 60 s reporter gets a two-minute window, a 5 s one ten seconds - the
    time scale is measured, not configured."""
    for gap in (5.0, 60.0):
        state, t0, frozen = _learned(gap=gap)
        events, _ = _run(state, t0, 20 * gap, frozen, commanded=8.0)
        assert len(events) == 1
        fired = events[0][1] - t0
        assert rw.GAP_MULTIPLE * gap < fired <= rw.GAP_MULTIPLE * gap + CYCLE, (gap, fired)


def test_a_suspended_car_behind_a_frozen_reading_is_stuck():
    """SuspendedEV is the charger saying the car takes no current - a reading
    frozen at 16 A contradicts it as squarely as a cut limit does. This is the
    finished-car case, where the phantom would otherwise last all night."""
    state, t0, frozen = _learned()
    events, _ = _run(state, t0, 120, frozen, status="SuspendedEV", commanded=16.0)
    assert [e for e, _ in events] == ["entered"]
    assert rw.assumed_legs(state, "SuspendedEV", 16.0) == (0.0, 0.0, 0.0)


def test_nothing_is_judged_before_the_cadence_is_known():
    """A watch that has never seen this reading move cannot know what "too
    long" means for it - so it waits rather than guessing."""
    state = {}
    events, _ = _run(state, 1000.0, 3600, _legs(16.0), commanded=8.0)
    assert events == []
    assert rw.normal_gap(state) is None


def test_the_offered_reading_can_teach_the_cadence():
    """A restart that finds the readout already frozen never sees it move, so
    its own gaps never arrive. The offered current comes from the same meter
    reports and does move while the charger follows its commands."""
    state = {}
    t = 1000.0
    frozen = _legs(16.0)
    offered = 16.0
    # Offered changes every 15 s while the draw reading never does.
    for step in range(rw.MIN_GAPS + 1):
        for _ in range(int(15 / CYCLE)):
            rw.observe(state, now=t, legs=frozen, status="Charging",
                       commanded=16.0, margin=MARGIN, drawing_current=LEG_DRAWING_CURRENT, companion=offered)
            t += CYCLE
        offered -= 0.5
    gap = rw.normal_gap(state)
    assert gap is not None and 14.0 <= gap <= 16.0, gap

    events, _ = _run(state, t, 200, frozen, commanded=8.0, companion=offered)
    assert [e for e, _ in events] == ["entered"]


# ── No false detection on normal behaviour ────────────────────────────────


def test_a_genuinely_steady_draw_is_never_stuck():
    """A car at the limit on a charger that reports whole amps repeats one
    value for hours. Steady is not a contradiction."""
    state, t0, _ = _learned()
    events, _ = _run(state, t0, 7200, _legs(16.0), commanded=16.0)
    assert events == []
    # ...nor a steady draw a hair above a limit it is within the margin of.
    events, _ = _run(state, t0 + 7202, 3600, _legs(16.0), commanded=15.5)
    assert events == []
    assert not rw.is_stuck(state)


def test_a_car_drawing_less_than_offered_is_never_stuck():
    """At its own limit, tapering, or full: a car below its permit is below
    every limit we give it, so a frozen-looking reading is never ruled out -
    even while the limit is cut, as long as it stays at or above the draw."""
    state, t0, _ = _learned()
    held = _legs(10.0)
    t = t0
    for commanded in (16.0, 13.0, 11.0, 10.0, 9.5):
        events, t = _run(state, t, 1800, held, commanded=commanded)
        assert events == [], commanded
    assert not rw.is_stuck(state)


def test_a_live_reading_answering_a_cut_is_not_stuck():
    """A charger that ramps down slowly stays above a freshly cut limit for
    far longer than two gaps - but every report brings a new value, and a new
    value is evidence of life."""
    state, t0, _ = _learned()
    t = t0
    amps = 16.0
    while amps > 8.0:
        events, t = _run(state, t, GAP - CYCLE, _legs(amps), commanded=8.0)
        assert events == [], amps
        amps -= 1.0   # the next report: still above 8 A + margin for a while
    assert not rw.is_stuck(state)


def test_a_suspended_car_reading_zero_is_not_stuck():
    state, t0, _ = _learned()
    events, _ = _run(state, t0, 3600, _legs(0.0), status="SuspendedEV")
    assert events == []
    # A standby trickle under the margin is not a contradiction either.
    events, _ = _run(state, t0 + 3602, 3600, _legs(0.6, phases=1), status="SuspendedEV")
    assert events == []


def test_the_clock_restarts_when_the_limit_stops_ruling_the_value_out():
    """Cut, then restored before the window closed: the car could genuinely be
    back at the frozen value, so the evidence starts again from nothing."""
    state, t0, frozen = _learned()
    _, t = _run(state, t0, rw.GAP_MULTIPLE * GAP - 2 * CYCLE, frozen, commanded=8.0)
    _, t = _run(state, t, 60, frozen, commanded=16.0)
    events, _ = _run(state, t, rw.GAP_MULTIPLE * GAP - CYCLE, frozen, commanded=8.0)
    assert events == []
    events, _ = _run(state, t + rw.GAP_MULTIPLE * GAP, 30, frozen, commanded=8.0)
    assert [e for e, _ in events] == ["entered"]


# ── What is learned as "normal" ───────────────────────────────────────────


def test_a_suspended_stretch_is_not_learned_as_a_normal_gap():
    """An hour of an unchanging 0.0 A under SuspendedEV says nothing about how
    often this charger reports while charging."""
    state, t0, _ = _learned()
    _, t = _run(state, t0, 3600, _legs(0.0), status="SuspendedEV")
    # The car resumes and the reading moves again.
    for i in range(3):
        rw.observe(state, now=t, legs=_legs(12.0 + i), status="Charging",
                   commanded=16.0, margin=MARGIN, drawing_current=LEG_DRAWING_CURRENT)
        t += GAP
    assert rw.normal_gap(state) == GAP


def test_a_stuck_episode_is_not_learned_as_a_normal_gap():
    state, t0, frozen = _learned()
    _, t = _run(state, t0, 3600, frozen, commanded=8.0)
    assert rw.is_stuck(state)
    for amps in (8.1, 7.9, 8.0):
        rw.observe(state, now=t, legs=_legs(amps), status="Charging",
                   commanded=8.0, margin=MARGIN, drawing_current=LEG_DRAWING_CURRENT)
        t += GAP
    assert not rw.is_stuck(state)
    assert rw.normal_gap(state) == GAP


# ── Entering and leaving blind mode ───────────────────────────────────────


def test_blind_mode_ends_after_two_new_values_not_one():
    state, t0, frozen = _learned()
    _, t = _run(state, t0, 120, frozen, commanded=8.0)
    assert rw.is_stuck(state)

    # One update - a reconnect's single report - is not a reading that moves.
    assert rw.observe(state, now=t, legs=_legs(8.2), status="Charging",
                      commanded=8.0, margin=MARGIN, drawing_current=LEG_DRAWING_CURRENT) is None
    events, t = _run(state, t + CYCLE, 300, _legs(8.2), commanded=8.0)
    assert events == [] and rw.is_stuck(state)

    assert rw.observe(state, now=t, legs=_legs(7.9), status="Charging",
                      commanded=8.0, margin=MARGIN, drawing_current=LEG_DRAWING_CURRENT) == "left"
    assert not rw.is_stuck(state)


def test_reset_drops_the_episode_and_keeps_what_was_learned():
    state, t0, frozen = _learned()
    _run(state, t0, 120, frozen, commanded=8.0)
    assert rw.is_stuck(state)
    assert rw.reset(state) is True
    assert not rw.is_stuck(state)
    assert rw.normal_gap(state) == GAP
    assert rw.reset(state) is False


def test_assumed_legs_are_the_limit_in_force_on_the_legs_that_carried_current():
    """A 1-phase car on a 3-phase charger is booked on its one leg only; a
    leg reading a meter's noise floor is not a leg carrying current."""
    state, t0, _ = _learned(phases=1)
    frozen = (15.7, 0.4, 0.0)
    _run(state, t0, 120, frozen, commanded=8.0)
    assert rw.is_stuck(state)
    assert rw.assumed_legs(state, "Charging", 8.0) == (8.0, 0.0, 0.0)
    # The engine may raise it again while blind: the assumption follows.
    assert rw.assumed_legs(state, "Charging", 12.0) == (12.0, 0.0, 0.0)
    # No limit known at all: nothing is assumed (subtracting a draw we cannot
    # bound from the grid readings is the unsafe direction).
    assert rw.assumed_legs(state, "Charging", None) == (0.0, 0.0, 0.0)


# ── Frozen LOW: the household follows our commands ────────────────────────

HOUSE = 1000.0 / 230   # a 1 kW house, on phase A
START = 21.7           # what the engine hands the charger: 5 kW


def _jitter(i, amp=0.1):
    """Deterministic household noise, a few tenths of an amp."""
    return amp * ((i * 7) % 5 - 2) / 2


def _frozen_low(phases=1):
    """A watch that has learned a 10 s cadence, whose reading then dropped to
    0.0 and froze there. Returns (state, now)."""
    state, t, _ = _learned(phases=phases)
    rw.observe(state, now=t, legs=(0.0, 0.0, 0.0), status="Charging",
               commanded=16.0, margin=MARGIN, drawing_current=LEG_DRAWING_CURRENT)
    return state, t + CYCLE


def _drive(state, t, cycles, *, commanded, status, household, legs=(0.0, 0.0, 0.0),
           leg_phases=("A",), start_index=0):
    """``cycles`` site cycles of one world: a reading, a status, a command and
    a household per phase (a callable of the cycle index, or a constant).
    Returns (events, now) - every non-None event of either path."""
    events = []
    for i in range(cycles):
        hh = household(start_index + i) if callable(household) else household
        for event in (
            rw.observe(state, now=t, legs=legs, status=status, commanded=commanded,
                       margin=MARGIN, drawing_current=LEG_DRAWING_CURRENT),
            rw.observe_household(state, now=t, household=hh, leg_phases=leg_phases,
                                 status=status, commanded=commanded,
                                 drawing_current=LEG_DRAWING_CURRENT),
        ):
            if event is not None:
                events.append((event, t))
        t += CYCLE
    return events, t


def _house(extra=0.0):
    return lambda i: {"A": HOUSE + extra + _jitter(i)}


def test_a_readout_frozen_at_zero_is_caught_by_the_household_following_us():
    """The field case: allocated 5 kW, the house "goes from 1 kW to 6 kW",
    the charger is cut, the house "comes back". Two answered changes, one each
    way - the verdict lands during the pause, before the charger is restarted
    into the same trap."""
    state, t = _frozen_low()
    events, t = _drive(state, t, 20, commanded=0.0, status="SuspendedEVSE",
                       household=_house())
    events, t = _drive(state, t, 8, commanded=START, status="Charging",
                       household=_house(START))
    assert events == [] and not rw.is_stuck(state)   # one answered change so far
    events, t = _drive(state, t, 90, commanded=0.0, status="SuspendedEVSE",
                       household=_house())
    assert [e for e, _ in events] == ["entered"], events
    assert events[0][1] < t - 60 * CYCLE, "decided early in the pause, not at the restart"
    assert state["stuck_how"] == rw.HOUSEHOLD_LOCKSTEP
    assert state["stuck_phases"] == ["A"]
    # Blind: nothing while paused, the command once it runs again.
    assert rw.assumed_legs(state, "SuspendedEVSE", 0.0) == (0.0, 0.0, 0.0)
    assert rw.assumed_legs(state, "Charging", START) == (START, 0.0, 0.0)


def test_the_legs_come_from_where_the_household_stepped():
    """All zeros say nothing about which legs carry the car. The household
    does: a 1-phase car on a 3-phase charger steps phase A only, and B and C -
    resolvable every time, never stepping - are positive evidence of no draw."""
    for car_phases, expected in (
        ("A", (True, False, False)),
        ("ABC", (True, True, True)),
    ):
        state, t = _frozen_low(phases=3)
        legs = ("A", "B", "C")

        def world(on):
            return lambda i: {
                p: HOUSE + _jitter(i + ord(p)) + (16.0 if on and p in car_phases else 0.0)
                for p in legs
            }
        _, t = _drive(state, t, 20, commanded=0.0, status="SuspendedEVSE",
                      household=world(False), leg_phases=legs)
        _, t = _drive(state, t, 8, commanded=16.0, status="Charging",
                      household=world(True), leg_phases=legs)
        events, t = _drive(state, t, 30, commanded=0.0, status="SuspendedEVSE",
                           household=world(False), leg_phases=legs)
        assert [e for e, _ in events] == ["entered"], (car_phases, events)
        assert state["stuck_legs"] == expected, (car_phases, state["stuck_legs"])


def test_a_house_load_coinciding_once_is_not_a_charger():
    """The reading is TRUE here - the car is not drawing - and a kettle the
    size of the command happens to switch on as we raise the limit. That is
    one answered change; the next change is not answered, and the run ends."""
    state, t = _frozen_low()
    events, t = _drive(state, t, 20, commanded=6.0, status="Charging", household=_house())
    events_2, t = _drive(state, t, 8, commanded=16.0, status="Charging",
                         household=_house(10.0))              # kettle on, by chance
    events_3, t = _drive(state, t, 30, commanded=6.0, status="Charging",
                         household=_house(10.0))              # kettle stays on
    events_4, t = _drive(state, t, 30, commanded=16.0, status="Charging",
                         household=_house(0.0))               # ...and goes off as we raise
    assert events + events_2 + events_3 + events_4 == []
    assert not rw.is_stuck(state)


def test_two_coincidences_the_same_way_are_not_a_charger():
    """Our ramp up, twice, each time as something in the house switched on.
    A charger's draw goes both ways with its command; this did not."""
    state, t = _frozen_low()
    _, t = _drive(state, t, 20, commanded=6.0, status="Charging", household=_house())
    _, t = _drive(state, t, 8, commanded=12.0, status="Charging", household=_house(6.0))
    events, t = _drive(state, t, 30, commanded=18.0, status="Charging",
                       household=_house(12.0))
    assert events == [] and not rw.is_stuck(state)
    assert len(state["ls_chain"]) == 1 or all(e["dir"] == 1 for e in state["ls_chain"])


def test_a_charging_car_that_draws_nothing_is_not_stuck():
    """Status Charging, a reading of exactly 0.0 that is simply TRUE - a full
    battery, a car balancing - and our limit moving around it. The grid does
    not answer, so nothing ever counts."""
    state, t = _frozen_low()
    for commanded in (16.0, 6.0, 16.0, 0.0, 16.0, 8.0, 16.0):
        events, t = _drive(state, t, 10, commanded=commanded, status="Charging",
                           household=_house())
        assert events == [], commanded
    assert not rw.is_stuck(state)
    assert not state.get("ls_chain")


def test_a_live_reading_voids_the_run():
    """A healthy charger whose reading lags the grid: the household follows
    the start until the reading catches up - and the moment it moves, the
    evidence is gone."""
    state, t = _frozen_low()
    _, t = _drive(state, t, 20, commanded=0.0, status="SuspendedEVSE", household=_house())
    _, t = _drive(state, t, 6, commanded=START, status="Charging", household=_house(START))
    # The reading reports the draw; the household is exact again.
    _, t = _drive(state, t, 4, commanded=START, status="Charging", household=_house(),
                  legs=(START, 0.0, 0.0))
    # The cut: the reading lags again, so for a few cycles the household reads
    # the stop as house load leaving - one answered change, and no more.
    events, t = _drive(state, t, 4, commanded=0.0, status="SuspendedEVSE",
                       household=_house(-START), legs=(START, 0.0, 0.0))
    assert events == [] and len(state.get("ls_chain") or []) <= 1
    # ...until it reports the stop, which is the end of that evidence too.
    events_2, t = _drive(state, t, 30, commanded=0.0, status="SuspendedEVSE",
                         household=_house(), legs=(0.0, 0.0, 0.0))
    assert events + events_2 == []
    assert not rw.is_stuck(state) and not state.get("ls_chain")


def test_a_change_lost_in_the_noise_counts_neither_way():
    """Half a 2 A step does not clear a phase swinging +/- 3 A: it can
    neither support a run nor break one."""
    state, t = _frozen_low()

    def noisy(extra):
        return lambda i: {"A": HOUSE + extra + 3.0 * ((i % 4) - 1.5)}
    _, t = _drive(state, t, 20, commanded=6.0, status="Charging", household=noisy(0))
    _, t = _drive(state, t, 10, commanded=8.0, status="Charging", household=noisy(2.0))
    events, t = _drive(state, t, 10, commanded=6.0, status="Charging", household=noisy(0))
    assert events == [] and not state.get("ls_chain")


def test_the_reading_must_be_silent_past_its_own_cadence():
    """A 60 s reporter whose car ran for 16 s between two reports looks, for
    a while, exactly like a stuck one. The run waits until the reading has
    been silent for two of its gaps."""
    state, t, _ = _learned(gap=60.0)
    rw.observe(state, now=t, legs=(0.0, 0.0, 0.0), status="Charging",
               commanded=16.0, margin=MARGIN, drawing_current=LEG_DRAWING_CURRENT)
    froze_at = t
    t += CYCLE
    _, t = _drive(state, t, 3, commanded=0.0, status="SuspendedEVSE", household=_house())
    _, t = _drive(state, t, 8, commanded=START, status="Charging", household=_house(START))
    events, t = _drive(state, t, 20, commanded=0.0, status="SuspendedEVSE",
                       household=_house())
    assert events == []           # ~60 s of silence: a live reading could still speak
    events, _ = _drive(state, t, 60, commanded=0.0, status="SuspendedEVSE",
                       household=_house(), start_index=20)
    assert [e for e, _ in events] == ["entered"], events
    assert events[0][1] - froze_at > rw.GAP_MULTIPLE * 60.0


# ── The blind footprint keeps the other loads inside the breaker ───────────


def _evse(load_id, priority, **kwargs):
    return LoadContext(
        load_id=load_id, entity_id=load_id, min_current=6,
        max_current=kwargs.pop("max_current", 16), phases=3, priority=priority,
        **kwargs,
    )


def test_a_blind_footprint_is_the_larger_of_allocation_and_last_command():
    blind = _evse("a", 1, l1_current=16.0, l2_current=16.0, l3_current=16.0,
                  draw_blind=True)
    assert _pool_deduction(blind, 10.0) == 16.0   # being cut: it may still take 16
    assert _pool_deduction(blind, 20.0) == 20.0   # being raised: reserve the raise
    # The existing rules are untouched.
    settled = _evse("b", 1, l1_current=9.0, draw_settled=True)
    assert _pool_deduction(settled, 16.0) == 9.0
    moving = _evse("c", 1, l1_current=9.0)
    assert _pool_deduction(moving, 16.0) == 16.0


def _site(*loads, household=5.0, breaker=25.0):
    site = SiteContext(
        voltage=230,
        main_breaker_rating=breaker,
        consumption=PhaseValues(household, household, household),
        export_current=PhaseValues(0.0, 0.0, 0.0),
        distribution_mode="priority",
        loads=list(loads),
    )
    calculate_all_load_targets(site)
    return site


def test_blind_permits_stay_inside_the_breaker():
    """The engine's own contract - household plus every permit within the
    breaker - holds for a blind charger exactly as for a metered one, whatever
    it was last told: its assumed draw only ever enters as a draw to subtract
    and a footprint to reserve, never as headroom."""
    household, breaker = 5.0, 25.0
    for assumed in (6.0, 10.0, 16.0):
        site = _site(
            _evse("blind", 1, l1_current=assumed, l2_current=assumed,
                  l3_current=assumed, draw_blind=True),
            _evse("other", 2),
            household=household, breaker=breaker,
        )
        permits = sum(c.available_current for c in site.loads)
        assert household + permits <= breaker, (assumed, permits)


def test_a_blind_charger_being_cut_frees_less_than_a_metered_one():
    """Its max slider was lowered to 10 A while blind. Until that command
    lands the charger still holds the 16 A it was told, so what lies above the
    second charger's minimum is not handed out on the strength of a cut that
    has not happened yet. A metered-but-moving charger, cut the same way,
    frees it at once.

    (The second charger's MINIMUM is reserved before anyone's surplus - that
    is the distribution's first pass, for live readings too - so the one
    command interval it takes the cut to land is the same for both; blind
    mode just does not widen it.)"""
    def pair(**first):
        site = _site(
            _evse("first", 1, max_current=10,
                  l1_current=16.0, l2_current=16.0, l3_current=16.0, **first),
            _evse("second", 2),
        )
        return {c.load_id: c.available_current for c in site.loads}

    blind = pair(draw_blind=True)
    metered = pair()
    assert blind["first"] == metered["first"] == 10.0
    assert blind["second"] < metered["second"], (blind, metered)
    household, breaker = 5.0, 25.0
    # Once the cut has landed, both sit inside the breaker.
    assert household + sum(blind.values()) <= breaker
    assert household + sum(metered.values()) <= breaker


# ---------------------------------------------------------------------------
# Standalone runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    failures = 0
    tests = [
        (name, fn) for name, fn in sorted(globals().items())
        if name.startswith("test_") and callable(fn)
    ]
    for name, fn in tests:
        try:
            fn()
            print(f"PASS {name}")
        except Exception as exc:  # noqa: BLE001 - report every failure
            failures += 1
            print(f"FAIL {name}: {exc!r}")
    print(f"\n{'OK' if not failures else 'FAILED'} - {failures} failure(s)")
    sys.exit(1 if failures else 0)
