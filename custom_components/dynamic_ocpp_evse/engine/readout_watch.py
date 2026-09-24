"""Stuck-readout watch: is an EVSE's reported draw still a measurement?

The report (2026-09): a charger goes on obeying its charging profiles while the
current/power it reports stops changing. Home Assistant holds a sensor's last
state, so the engine keeps reading a draw that was true once. The feedback
loop subtracts every managed draw from the grid CTs to reconstruct the
household (engine/hub_calculation._apply_feedback_loop), so whichever way the
number is wrong, the household is wrong by the same amount the other way:

* frozen ABOVE the real draw - the car tapered, finished, or we cut its limit
  and it complied: every frozen amp the car is no longer taking is handed out
  again as headroom, against a breaker that is that much smaller in reality.
* frozen BELOW the real draw - the field case, a readout pinned at 0 while the
  car charges: every amp the car takes is booked as HOUSE load. Its own start
  eats the allowance, it is cut, the "house" load vanishes, it is let go
  again - the charger hunts, stopping and starting for as long as the reading
  stays stuck.

Neither is detectable from steadiness. A car at its own limit holds one current
for an hour, and a charger that reports whole amps repeats the value bit for
bit the whole time. What gives a frozen number away is a CONTRADICTION, and
there is one for each direction - two evidence paths to one verdict:

* ABOVE_LIMIT (``observe``): a leg reads more than ``margin`` above the limit
  in force - the commanded limit, or 0 A while the connector's own status says
  no energy is flowing - the value has not changed at all since that became
  true, and it has stayed that way for longer than GAP_MULTIPLE times the
  longest gap this reading has recently shown between two values while the
  car was charging. A compliant charger cannot be delivering 16 A after a cut
  to 8 A; a live reading would have said so within its own reporting gap.
* HOUSEHOLD_LOCKSTEP (``observe_household``): the household the engine
  reconstructs steps with our own commands to this charger, on its phases,
  repeatedly and both ways, while its reading does not move a bit. House loads
  do not follow our commands; the charger's own unseen draw does. The rule and
  its guards are with the function, further down. It also catches a reading
  frozen HIGH, which steps the household down when we cut.

Nothing is tuned to a charger:

* the limit is what the charger was actually sent (control/ocpp.py records it
  only after set_charge_rate returned), and the no-energy statuses are OCPP's;
* the margin is the one the engine already uses for "drawing what it was
  offered" (SETTLE_PERMIT_MARGIN), widened for W-encoded profiles by the same
  allowance their draw is given elsewhere (WATTS_PROFILE_TOLERANCE) - the
  caller passes it in;
* the time scale is MEASURED on this charger: the longest gap between two
  values of its reading while a car was charging and the reading was trusted.
  A charger that reports every 60 s gets a two-minute window, one reporting
  every 5 s gets ten seconds;
* the lockstep compares a step with the command that should have caused it and
  with the household's own measured spread - ratios of measured quantities,
  and counts.

Why ABOVE_LIMIT cannot trip on normal behaviour: a car drawing LESS than it is
offered - at its own limit, tapering, full - reads below the limit and is never
ruled out; a genuine steady draw at the limit reads within the margin; a live
reading still above a freshly cut limit changes at its next report, and any
change restarts the clock. Until enough gaps have been seen it does not judge
at all - it cannot know what "too long" means for a charger it has not
watched.

Deliberately NOT a rule: "the reading has not changed for a long time". On its
own that cannot tell stuck from steady, and blind mode's assumption (the
commanded limit) sits ABOVE a genuinely self-limited car's draw - acting on
steadiness alone would book a draw that is not there.

While stuck, ``assumed_legs`` gives the draw the engine uses instead: the limit
in force on the legs the verdict decided carry current (see
engine/load_builders.py for the rest of blind mode). The watch lets go once the
reading produces RESUME_VALUES new values - moving, not moved once - or when
the caller resets it (session over, monitor unreadable, Dynamic Control off).

Pure Python with no package imports, so the pure test tier can load it straight
from its path. The caller owns the state dict (the load's runtime bucket) and
the clock (``now``, monotonic seconds).
"""

# PEP 604 unions in signatures; nothing evaluates annotations at runtime, so
# this keeps the module importable on the Python 3.9 standalone runners.
from __future__ import annotations

# OCPP 1.6 connector statuses in which a car may be connected but no energy is
# transferred: waiting to start, suspended by the car or by the charger, or the
# transaction already stopped. Against these the limit in force is 0 A,
# whatever was last commanded. (Available is not here: it means no car, and the
# caller resets the watch for it.)
NO_ENERGY_STATUSES = frozenset(
    {"Preparing", "SuspendedEV", "SuspendedEVSE", "Finishing"}
)

# The only status the normal gap is learned in: a car taking current. A gap
# that spans a suspended or finishing stretch would teach the watch that an
# hour of an unchanging 0.0 A is normal for this charger.
LEARNING_STATUS = "Charging"

# How many of the longest recent gaps the contradiction must outlast. Two,
# because the ruling-out can begin anywhere inside a reporting gap - one whole
# gap may pass before the next report is even due - and the car's own response
# to the new limit has to fit inside the other.
GAP_MULTIPLE = 2

# How many recent gaps the normal gap is the maximum of. A memory length, not a
# property of any charger: long enough that one lucky run of quick reports
# cannot set a hair trigger, short enough that a charger whose reporting
# changes (a reconfigured sample interval) is re-learned within a few dozen
# reports.
GAP_MEMORY = 20

# Gaps the watch must have seen before it judges at all - the least evidence
# that describes a pattern rather than one coincidence.
MIN_GAPS = 3

# New values a stuck reading must produce before it is trusted again: moving,
# not moved once. A charger that sends one update on a reconnect and freezes
# again stays blind.
RESUME_VALUES = 2

# How a stuck verdict was reached - published as the readout_stuck_evidence
# attribute, and what decides which legs the assumption goes on.
ABOVE_LIMIT = "above_limit"
HOUSEHOLD_LOCKSTEP = "household_lockstep"

# The tracker prefixes: the reading itself, and an optional companion reading
# from the same charger (its offered current or power). The companion only
# ever stands in for the reading's own gaps, while the reading has not produced
# enough of them - which is the case that matters on a restart that finds the
# readout already frozen: it will never produce one.
_OWN = ""
_COMPANION = "companion_"


def limit_in_force(status: str | None, commanded: float | None) -> float | None:
    """The current the charger may deliver right now, in A, or None if unknown.

    0 A in a no-energy status, whatever was commanded; otherwise the commanded
    limit, which is None until the first command has been accepted.
    """
    if status in NO_ENERGY_STATUSES:
        return 0.0
    return None if commanded is None else float(commanded)


def normal_gap(state: dict) -> float | None:
    """The longest recent gap between two values of the reading, in seconds.

    Learned while a car was charging and the reading was trusted. Falls back to
    the companion reading's gaps while the reading's own are too few; None
    until either has MIN_GAPS - the watch does not judge before then.
    """
    for prefix in (_OWN, _COMPANION):
        gaps = state.get(f"{prefix}gaps") or []
        if len(gaps) >= MIN_GAPS:
            return max(gaps)
    return None


def is_stuck(state: dict | None) -> bool:
    """Whether the watch currently judges the reading stuck."""
    return bool(state and state.get("stuck"))


def _track(state: dict, prefix: str, value, now: float, charging: bool,
           learn: bool = True) -> bool:
    """Advance one value tracker. Returns True when the value changed.

    Bit-identical comparison on purpose: a reading that has not moved by a
    single bit is the one thing a frozen report and nothing else produces
    reliably. A gap is recorded only when it was spent entirely under the
    learning status (``clean``) - and never while the reading is judged stuck
    (``learn``), since that span is the anomaly, not the pattern.
    """
    if value is None:
        # No reading: the gap now spans a hole and says nothing about cadence.
        state[f"{prefix}clean"] = False
        return False
    last = state.get(f"{prefix}value")
    if value == last:
        if not charging:
            state[f"{prefix}clean"] = False
        return False
    if last is not None and learn and state.get(f"{prefix}clean"):
        gaps = state.setdefault(f"{prefix}gaps", [])
        gaps.append(now - state[f"{prefix}changed_at"])
        del gaps[:-GAP_MEMORY]
    state[f"{prefix}value"] = value
    state[f"{prefix}changed_at"] = now
    state[f"{prefix}clean"] = charging
    return last is not None


def _clear_episode(state: dict) -> None:
    """Forget the verdict and the pending evidence of both paths; keep what
    was learned about the charger."""
    for key in (
        "stuck", "stuck_how", "stuck_since", "stuck_value", "stuck_limit",
        "stuck_gap", "stuck_legs", "stuck_phases", "stuck_run", "stuck_source",
        "resume_values", "ruled_out_since",
    ):
        state.pop(key, None)
    _reset_lockstep(state)


def reset(state: dict) -> bool:
    """Start over on the reading: no verdict, no pending contradiction, no
    value being held. The learned gaps survive - they describe the charger,
    not the session. Returns True if a stuck verdict was dropped.

    For the caller to use whenever the watch has nothing to judge: no car on
    the connector, the monitor unreadable, the load handed back to the user.
    """
    was_stuck = is_stuck(state)
    _clear_episode(state)
    for prefix in (_OWN, _COMPANION):
        state.pop(f"{prefix}value", None)
        state.pop(f"{prefix}changed_at", None)
        state[f"{prefix}clean"] = False
    state.pop("moved", None)
    return was_stuck


def observe(
    state: dict,
    *,
    now: float,
    legs,
    status: str | None,
    commanded: float | None,
    margin: float,
    drawing_current: float,
    companion=None,
) -> str | None:
    """Advance the watch by one site cycle - the reading's own tracker, and
    the frozen-HIGH evidence path (the reading claims more than the limit in
    force). The frozen-LOW path is ``observe_household``, which the caller runs
    after this, once the whole site's draws are known.

    ``legs`` is the charger's reported draw per leg (A), exactly as read;
    ``commanded`` the limit it last accepted (A, None before the first);
    ``margin`` how far above the limit a genuine draw may sit (A);
    ``drawing_current`` the per-leg current above which a leg counts as
    carrying current (A);
    ``companion`` an optional second reading from the same charger (its
    offered current or power; None when unconfigured or unreadable).

    Returns "entered" on the cycle the reading is first judged stuck, "left" on
    the cycle it is trusted again, and None otherwise - so the caller can log
    once per episode rather than once per cycle.
    """
    legs = tuple(float(v) for v in legs)
    charging = status == LEARNING_STATUS
    stuck = is_stuck(state)

    _track(state, _COMPANION, companion, now, charging)
    changed = _track(state, _OWN, legs, now, charging, learn=not stuck)
    # For observe_household, later in the same cycle: a reading that moved is
    # alive, and no lockstep evidence survives it.
    state["moved"] = changed

    if stuck:
        if changed:
            state["resume_values"] = state.get("resume_values", 0) + 1
            if state["resume_values"] >= RESUME_VALUES:
                _clear_episode(state)
                return "left"
        return None

    in_force = limit_in_force(status, commanded)
    ruled_out = in_force is not None and max(legs) > in_force + margin
    if changed or not ruled_out:
        # Nothing ruled out - or a fresh value, which is evidence of life and
        # restarts the clock even when it is still above the limit.
        state["ruled_out_since"] = None
    if not ruled_out:
        return None
    if state.get("ruled_out_since") is None:
        state["ruled_out_since"] = now
    gap = normal_gap(state)
    if gap is None or now - state["ruled_out_since"] <= GAP_MULTIPLE * gap:
        return None

    _enter(
        state, now, ABOVE_LIMIT, legs, in_force, gap,
        tuple(v > drawing_current for v in legs),
    )
    return "entered"


def _enter(state, now, how, legs, in_force, gap, stuck_legs):
    state["stuck"] = True
    state["stuck_how"] = how
    state["stuck_since"] = now
    state["stuck_value"] = tuple(legs)
    state["stuck_limit"] = in_force
    state["stuck_gap"] = gap
    state["stuck_legs"] = tuple(bool(x) for x in stuck_legs)
    state["resume_values"] = 0


def assumed_legs(state: dict, status: str | None, commanded: float | None) -> tuple:
    """The per-leg draw (A) to use in place of a stuck reading.

    The limit in force on every leg the verdict decided carries current, 0 on
    the others. "The charger draws everything it was told" is the owner's rule
    for when the reading is gone; which legs it is drawn on is the one thing
    each evidence path still knows:

    * frozen HIGH - the legs the frozen value showed carrying current, so a
      1-phase car on a 3-phase charger is not booked on legs it never used;
    * frozen LOW - the charger's legs, less any whose phase showed positive
      evidence of carrying nothing (it was resolvable at every answered
      command and never stepped). A leg kept on too little evidence errs
      toward assuming a draw that is not there, which the blind footprint then
      reserves in full (see target_calculator._pool_deduction), rather than
      toward the frozen zero that caused the hunting.

    With nothing in force known at all, nothing is assumed: subtracting a draw
    we cannot bound from the grid readings would be the unsafe direction.
    """
    in_force = limit_in_force(status, commanded)
    if in_force is None:
        in_force = 0.0
    carrying = state.get("stuck_legs") or (False, False, False)
    return tuple(in_force if leg else 0.0 for leg in carrying)


# ── Frozen LOW: the household follows our own commands ──────────────────
#
# The field case (Anze, 2026-09-23): the readout sticks at 0 while the car
# really draws. The feedback loop subtracts each managed draw from the grid
# CTs, so every amp the reading misses is booked as HOUSE load - the charger
# starts at 5 kW, the "household" jumps 1 -> 6 kW, the 6 kW allowance is gone,
# the charger is cut, the household falls back to 1 kW, the charger is let go
# again, and round it goes. Frozen low hunts.
#
# Nothing about the reading contradicts anything (0 A is below every limit),
# so the evidence has to come from the grid: the household as the engine
# reconstructs it - grid minus every managed draw - steps UP by about the
# charger's limit when we start or raise it and DOWN when we cut or stop it,
# on the charger's own phases, while the charger's reading does not move a
# bit. Independent house loads do not follow our commands, and certainly not
# repeatedly and both ways. So:
#
#     each change of the limit in force (a command, or a status that stops or
#     resumes the flow) is ANSWERED when, on at least one of the charger's
#     phases, the household's level after it has moved from its level before
#     it in the same direction and by an amount nearer the change itself than
#     zero - the nearer of the two hypotheses "the charger draws what it is
#     told, unseen" and "it does not";
#     a change the household does not answer (on a phase where it could have
#     been told from the household's own noise) breaks the run, as does any
#     movement of the reading;
#     LOCKSTEP_EVENTS answered changes in a row, at least one each way, on a
#     common phase, and the reading bit-identical for longer than GAP_MULTIPLE
#     of its own normal gaps, judge it stuck.
#
# "Before" and "after" are the household's median over the samples of the
# interval that change separates - the charger had the whole interval, up to
# the next change, to respond, and a median shrugs off the cycle or two it
# takes. A change counts either way only where half of it exceeds the
# household's spread (median absolute deviation) in both intervals - the
# command size against the phase's measured noise; the half is the midpoint
# between the two hypotheses, not a tuning.
#
# Why it will not trip on normal behaviour:
#   * a healthy reading moves when the car's draw moves, and every movement
#     voids the run; its household does not step with our commands at all,
#     because the feedback subtracts the very draw that stepped;
#   * a car in Charging drawing nothing - waiting, balancing, full - does not
#     move the grid when we change its limit, so every change is unanswered;
#   * the engine's own reaction to a house load is the OPPOSITE sign - the
#     house steps up, then we cut - so it can only ever break a run;
#   * one coincidence is one answered change; a run needs a second one, the
#     other way, on the same phase, with nothing unanswered in between.
#
# What it cannot tell apart: a live reading slower than the whole run - a
# charger reporting every 60 s whose car ran for 15 s between two reports.
# Where the normal gap is known the reading must also have been silent for
# GAP_MULTIPLE of it; where it is not (nothing learned since Home Assistant
# started), the run alone decides. The cost of that mistake is benign by
# construction: the household followed our commands, so the car WAS drawing
# about what it was told, which is exactly what blind mode then assumes - and
# the reading's next two values end the episode.

# Answered changes in a row, at least one in each direction, before the
# household following our commands is taken for the charger's own draw.
LOCKSTEP_EVENTS = 2

# Household samples an interval keeps for its level and spread - the most
# recent ones, so the level before a change is what the household was doing
# just before it. A memory length, like GAP_MEMORY.
LEVEL_MEMORY = GAP_MEMORY

# Statuses the run survives: the charger can be drawing (Charging) or has
# said it is not (the no-energy set, where the limit in force is 0). Anything
# else - Unknown, Faulted, Unavailable - is no reference at all.
_LOCKSTEP_STATUSES = frozenset({LEARNING_STATUS}) | NO_ENERGY_STATUSES

_PHASES = ("A", "B", "C")
_LS_KEYS = ("ls_in_force", "ls_samples", "ls_pending", "ls_chain")


def _reset_lockstep(state: dict) -> None:
    for key in _LS_KEYS:
        state.pop(key, None)


def _median(values):
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def _summarise(samples) -> dict:
    """Per phase, the (level, spread) of one interval's household samples:
    their median, and their median absolute deviation from it."""
    out = {}
    for phase in _PHASES:
        values = [s[phase] for s in samples if s.get(phase) is not None]
        if values:
            level = _median(values)
            out[phase] = (level, _median([abs(v - level) for v in values]))
    return out


def _answer(pending: dict, after: dict, leg_phases) -> tuple | None:
    """How the household answered one change of the limit in force.

    None when the change could not be told from the household's own noise on
    any of the charger's phases - it then counts neither way. Otherwise
    ``(stepped, resolvable)``: the phases whose household moved by the change,
    and the phases where that question had an answer.
    """
    before = pending["before"]
    stepped, resolvable = set(), set()
    for phase in set(leg_phases):
        if phase not in before or phase not in after:
            continue
        # Two legs on one phase (a mis-mapped charger) step it twice over.
        expected = pending["delta"] * list(leg_phases).count(phase)
        (level_before, spread_before) = before[phase]
        (level_after, spread_after) = after[phase]
        if abs(expected) / 2 <= max(spread_before, spread_after):
            continue
        resolvable.add(phase)
        step = level_after - level_before
        if step * expected > 0 and abs(step - expected) < abs(step):
            stepped.add(phase)
    if not resolvable:
        return None
    return stepped, resolvable


def _judged(chain: list, pending: dict, after: dict, leg_phases, now: float) -> list:
    """The run after one change's answer: extended, restarted, or ended.
    Returns a new list; ``chain`` is not touched."""
    if pending.get("void"):
        # The reading moved while this change was being answered: it is alive.
        return []
    answer = _answer(pending, after, leg_phases)
    if answer is None:
        return list(chain)
    stepped, resolvable = answer
    if not stepped:
        return []
    common = set(stepped)
    for event in chain:
        common &= set(event["stepped"])
    # Stepped, but on another phase than before: not one charger's draw. This
    # answer may start a run of its own.
    run = list(chain) if common else []
    run.append({
        "dir": 1 if pending["delta"] > 0 else -1,
        "stepped": sorted(stepped),
        "resolvable": sorted(resolvable),
        "at": now,
    })
    return run


def _conclusive(state: dict, chain: list, now: float) -> bool:
    if len(chain) < LOCKSTEP_EVENTS or {e["dir"] for e in chain} != {1, -1}:
        return False
    gap = normal_gap(state)
    silent_for = now - state.get(f"{_OWN}changed_at", now)
    return gap is None or silent_for > GAP_MULTIPLE * gap


def observe_household(
    state: dict,
    *,
    now: float,
    household: dict,
    leg_phases,
    status: str | None,
    commanded: float | None,
    drawing_current: float,
) -> str | None:
    """Advance the frozen-LOW evidence path by one site cycle.

    Run after ``observe`` in the same cycle, once every managed load's draw is
    known. ``household`` is the site's reconstruction per phase - the signed
    grid reading minus every managed draw, in A, None for a phase with no
    usable reading; ``leg_phases`` the site phase each of the charger's legs
    is wired to, one per leg it has.

    Returns "entered" on the cycle the reading is judged stuck, else None.
    """
    if is_stuck(state):
        return None
    if state.get("moved"):
        state["ls_chain"] = []
        if state.get("ls_pending"):
            state["ls_pending"]["void"] = True
    in_force = limit_in_force(status, commanded)
    if in_force is None or status not in _LOCKSTEP_STATUSES:
        _reset_lockstep(state)
        return None

    sample = {
        phase: None if household.get(phase) is None else float(household[phase])
        for phase in _PHASES
    }
    previous = state.get("ls_in_force")
    if previous is None:
        state["ls_in_force"] = in_force
        state["ls_samples"] = [sample]
        return None
    chain = state.get("ls_chain") or []
    if in_force != previous:
        # A change: the interval it ends is the answer to the change pending
        # before it, and the level it leaves is the "before" of this one.
        closed = _summarise(state.get("ls_samples") or [])
        pending = state.get("ls_pending")
        if pending is not None:
            chain = _judged(chain, pending, closed, leg_phases, now)
        state["ls_chain"] = chain
        state["ls_pending"] = {
            "delta": in_force - previous,
            "before": closed,
            "void": False,
            "at": now,
        }
        state["ls_in_force"] = in_force
        state["ls_samples"] = [sample]
        if not _conclusive(state, chain, now):
            return None
    else:
        samples = state.setdefault("ls_samples", [])
        samples.append(sample)
        del samples[:-LEVEL_MEMORY]
        # The change still being answered may already settle the run: the
        # median of the interval so far has moved only once MOST of it shows
        # the step, so nothing is decided on the cycle or two a car takes to
        # respond. Committed only when conclusive - otherwise it is judged
        # again, on the whole interval, when the next change closes it.
        pending = state.get("ls_pending")
        if pending is None:
            return None
        trial = _judged(chain, pending, _summarise(samples), leg_phases, now)
        if not _conclusive(state, trial, now):
            return None
        chain = state["ls_chain"] = trial
        state["ls_pending"] = None

    # Legs to assume on: the charger's legs, less those whose phase gave
    # positive evidence of carrying nothing - resolvable at every answered
    # change and never stepped (a 1-phase car on a 3-phase charger). Plus any
    # leg the frozen value itself shows carrying current.
    silent = set(_PHASES)
    for event in chain:
        silent &= set(event["resolvable"]) - set(event["stepped"])
    frozen = state.get(f"{_OWN}value") or (0.0, 0.0, 0.0)
    legs = tuple(
        (i < len(leg_phases) and leg_phases[i] not in silent)
        or frozen[i] > drawing_current
        for i in range(3)
    )
    _enter(state, now, HOUSEHOLD_LOCKSTEP, frozen, in_force, normal_gap(state), legs)
    state["stuck_phases"] = sorted(
        set.intersection(*[set(e["stepped"]) for e in chain])
    )
    state["stuck_run"] = len(chain)
    return "entered"
