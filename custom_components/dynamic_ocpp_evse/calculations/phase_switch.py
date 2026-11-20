"""Phase switching (1 / 3 phases) - pure, no Home Assistant.

A three-leg charger that can switch runs on three phases whenever three reach
its minimum, and on one phase (its L1) only when three would pause it and one
would not - three at the minimum always deliver more than one at the maximum,
so "more power" and "avoid a pause" are the same rule. Back to three once all
three legs could take the minimum and three would give at least a margin (W, per
charger) more than one phase gets now, for a hold time. With a car
connected no change comes sooner than an interval after the last one, in either
direction: a charger stops to switch, and that stop must not read as a reason to
switch back (the owner, 2026-11-05). Design and the owner's answers:
docs/plans/2026-11-01/phase-switching.md.
"""

import math
from copy import deepcopy

from .models import LoadContext, SiteContext
from .target_calculator import calculate_all_load_targets

ONE = 1
THREE = 3


def full_mask(load: LoadContext) -> str:
    """The site phases of the load's three legs."""
    return "".join(sorted({load.l1_phase, load.l2_phase, load.l3_phase}))


def set_phases(load: LoadContext, phases: int) -> None:
    """Put the load on ``phases`` (1: its L1's site phase, with what all three
    legs could take computed as its potential; 3: all its legs)."""
    load.phases = phases
    if phases == ONE:
        load.active_phases_mask = load.l1_phase
        load.potential_phases_mask = full_mask(load)
    else:
        load.active_phases_mask = full_mask(load)
        load.potential_phases_mask = ""


def single_phase_permit(site: SiteContext, load_id: str) -> float:
    """The permit (A) the load would get this cycle on its L1 alone: the whole
    calculation again on a copy of the site with that one load narrowed, so
    every other load, pool and circuit group is weighed exactly as for real."""
    trial = deepcopy(site)
    load = next(c for c in trial.loads if c.load_id == load_id)
    set_phases(load, ONE)
    calculate_all_load_targets(trial)
    return load.available_current


def _runs(load: LoadContext) -> bool:
    """The load has a permit it can run on."""
    return load.available_current > 0 and load.available_current >= load.min_current


def _trial(site: SiteContext, load_id: str, phases: int) -> SiteContext:
    """The site calculated again on a copy, with that one load on ``phases``."""
    trial = deepcopy(site)
    set_phases(next(c for c in trial.loads if c.load_id == load_id), phases)
    calculate_all_load_targets(trial)
    return trial


def spared_by_one_phase(site: SiteContext, load_id: str) -> tuple[float, str | None]:
    """``(own permit on one phase, a load three phases pause and one lets run)``:
    another load with something connected that has no runnable permit now and
    gets one when this load narrows to its L1. No such load, no trial run."""
    waiting = [
        c.load_id for c in site.loads
        if c.load_id != load_id and c.connector_status != "Available" and not _runs(c)
    ]
    if not waiting:
        return 0.0, None
    trial = {c.load_id: c for c in _trial(site, load_id, ONE).loads}
    spared = next((trial[i].entity_id for i in waiting if _runs(trial[i])), None)
    return trial[load_id].available_current, spared


def paused_by_three_phases(site: SiteContext, load_id: str) -> str | None:
    """A load that runs now and would be left without a runnable permit if
    this load went back to three phases (None: none)."""
    running = [c.load_id for c in site.loads if c.load_id != load_id and _runs(c)]
    if not running:
        return None
    trial = {c.load_id: c for c in _trial(site, load_id, THREE).loads}
    return next((trial[i].entity_id for i in running if not _runs(trial[i])), None)


def three_phase_gain_w(potential: float, permit: float, voltage: float) -> float:
    """What three phases at ``potential`` A each would give more than one phase
    at ``permit`` A (W)."""
    return (THREE * potential - permit) * voltage


def three_beats_one(potential: float, min_current: float, gain_w: float, margin_w: float) -> bool:
    """Three phases are worth going back to: every leg at the minimum or more,
    and at least ``margin_w`` more power than one phase gets now."""
    return potential >= min_current and gain_w >= margin_w


def decide(
    on: int,
    since: float,
    now: float,
    *,
    connected: bool,
    held: bool,
    permit: float,
    min_current: float,
    single_permit,
    potential: float,
    gain_w: float,
    margin_w: float,
    above_s: float,
    hold_s: float,
    interval_s: float,
    resumable: bool = False,
    forced_one: bool = False,
    unspaced: bool = False,
    spares=None,
    three_pauses=None,
    sharing: bool = False,
    voltage: float = 230.0,
) -> tuple[int, float, str]:
    """``(phases, since, reason)`` for a switchable load this cycle.

    ``on``/``since``: the phase count it runs on and when that was decided;
    ``connected``: a car is there (not Available); ``held``: Dynamic Control is
    off or its status draws nothing - the count stays; ``permit``: what it got
    on ``on`` phases; ``single_permit``: a callable giving what it would get on
    one (called only when three would pause it); ``potential``: per leg on all
    three while it runs on one; ``gain_w``: what three would give more than one
    phase gets now (``three_phase_gain_w``); ``above_s``: how long
    ``three_beats_one`` has held (the caller keeps the clock);
    ``resumable``: Dynamic Control on and the charger not charging (paused, the
    car pausing, not started, finishing) - on one phase it goes back to three
    without the hold when three would reach the minimum, so the next charge
    starts on three (GitHub issue 21, the owner 2026-11-01).

    With a car connected nothing changes within ``interval_s`` of ``since``
    (the last change): the charger's own stop for a switch is "not charging"
    too, and it switched the third site's go-e back 4 s after each change
    (2026-11-05). Without a car the count returns to three at once - nothing
    is drawing - and ``since`` is kept, so that return does not hold up the
    next session.

    ``forced_one`` (the select on One phase): one phase always, also with no
    car, so the next session starts on one; with a car on three it still waits
    out ``interval_s``.

    ``unspaced``: the charger is not charging and no switch of ours is under
    way or settling - ``interval_s`` does not apply, nothing is switched under
    load (the owner, 2026-11-20: a paused charger waited 15 minutes on three
    phases beside one it could have shared a phase with). The caller keeps the
    switch's own stop out of it, which is what the interval was made for.

    The other loads (the owner, 2026-11-20: under Shared a charger went back to
    three phases, took the whole import allowance at its minimum and paused the
    second charger - 4.1 kW into one car where 4.6 kW had gone into two).
    ``spares``: a callable giving ``(own permit on one phase, a load three
    phases pause and one phase lets run)`` (``spared_by_one_phase``); on three
    phases the charger narrows to one for that load when the site shares
    (``sharing``: the Shared distribution mode) or when three phases give it
    less than ``margin_w`` more than one would - under the priority modes a
    charger that gains more keeps what its rank gives it. ``three_pauses``: a
    callable naming a running load three phases would pause
    (``paused_by_three_phases``); a sharing site keeps the charger on one.
    """
    if unspaced:
        interval_s = 0.0
    if forced_one:
        if on == ONE:
            return ONE, since, "set to one phase"
        if connected and now - since < interval_s:
            return on, since, f"set to one phase - last change {math.ceil((now - since) / 60)} min ago"
        return ONE, now, "set to one phase"
    if not connected:
        return THREE, since, "no car" if on == THREE else "no car - a new session starts on three phases"
    wait = interval_s - (now - since)
    if wait > 0:
        return on, since, f"phase count kept - last change {math.ceil((now - since) / 60)} min ago"
    if on == ONE and resumable and potential >= min_current:
        if blocked := (sharing and three_pauses is not None and three_pauses()):
            return ONE, since, f"three phases would pause {blocked} - stays on one phase"
        return THREE, now, f"not charging - resumes on three phases ({potential:.1f} A each)"
    if held:
        return on, since, "not charging - phase count kept"
    if on == THREE:
        if permit >= min_current:
            single, other = spares() if spares is not None else (0.0, None)
            if other and single >= min_current and (
                sharing or three_phase_gain_w(permit, single, voltage) < margin_w
            ):
                return ONE, now, (
                    f"three phases pause {other}; one phase gets {single:.1f} A and lets it run"
                )
            return THREE, since, f"three phases get {permit:.1f} A"
        single = single_permit()
        if single >= min_current:
            return ONE, now, (
                f"three phases would pause it ({permit:.1f} A < {min_current:.1f} A), "
                f"one phase gets {single:.1f} A"
            )
        return THREE, since, f"neither reaches {min_current:.1f} A (one phase: {single:.1f} A)"
    if potential < min_current or gain_w < margin_w:
        return ONE, since, (
            f"three phases would get {potential:.1f} A each, {gain_w:+.0f} W against one phase, "
            f"under the {margin_w:.0f} W margin"
        )
    left = hold_s - above_s
    if left > 0:
        return ONE, since, (
            f"three phases would get {potential:.1f} A; back to three in "
            f"{math.ceil(left / 60)} min if it holds"
        )
    if blocked := (sharing and three_pauses is not None and three_pauses()):
        return ONE, since, f"three phases would pause {blocked} - stays on one phase"
    return THREE, now, f"three phases get {potential:.1f} A"
