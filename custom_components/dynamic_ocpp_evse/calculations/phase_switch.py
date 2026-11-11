"""Phase switching (1 / 3 phases) - pure, no Home Assistant.

A three-leg charger that can switch runs on three phases whenever three reach
its minimum, and on one phase (its L1) only when three would pause it and one
would not - three at the minimum always deliver more than one at the maximum,
so "more power" and "avoid a pause" are the same rule. Back to three once all
three legs could take the minimum plus a margin for a hold time. With a car
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
    margin: float,
    above_s: float,
    hold_s: float,
    interval_s: float,
    resumable: bool = False,
    forced_one: bool = False,
) -> tuple[int, float, str]:
    """``(phases, since, reason)`` for a switchable load this cycle.

    ``on``/``since``: the phase count it runs on and when that was decided;
    ``connected``: a car is there (not Available); ``held``: Dynamic Control is
    off or its status draws nothing - the count stays; ``permit``: what it got
    on ``on`` phases; ``single_permit``: a callable giving what it would get on
    one (called only when three would pause it); ``potential``: per leg on all
    three while it runs on one; ``above_s``: how long ``potential`` has stayed
    at or above the minimum plus ``margin`` (the caller keeps the clock);
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
    """
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
        return THREE, now, f"not charging - resumes on three phases ({potential:.1f} A each)"
    if held:
        return on, since, "not charging - phase count kept"
    if on == THREE:
        if permit >= min_current:
            return THREE, since, f"three phases get {permit:.1f} A"
        single = single_permit()
        if single >= min_current:
            return ONE, now, (
                f"three phases would pause it ({permit:.1f} A < {min_current:.1f} A), "
                f"one phase gets {single:.1f} A"
            )
        return THREE, since, f"neither reaches {min_current:.1f} A (one phase: {single:.1f} A)"
    need = min_current + margin
    if potential < need:
        return ONE, since, f"three phases would get {potential:.1f} A, under {need:.1f} A"
    left = hold_s - above_s
    if left > 0:
        return ONE, since, (
            f"three phases would get {potential:.1f} A; back to three in "
            f"{math.ceil(left / 60)} min if it holds"
        )
    return THREE, now, f"three phases get {potential:.1f} A"
