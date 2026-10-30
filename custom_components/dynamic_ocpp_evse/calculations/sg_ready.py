"""SG Ready heat pump (JUG-24) - the pure state machine. No Home Assistant imports.

A heat pump with the SG Ready label has four operating states, chosen by two
potential-free contacts (relay 1, relay 2):

    Block        (1, 0)  utility lockout, the pump is off
    Normal       (0, 0)  regular operation - also what a dead Shelly falls back to
    Recommended  (0, 1)  run harder; a recommendation
    Force        (1, 1)  run-up command

The device has no operating mode: the state follows the site, first match wins
(see ``step``):

    1 overload that reached this device         -> Block (a breaker overload at
                                                   once, max import power after 2 min)
    2 actual excess                             -> Force
    3 an early-start verdict (predicted excess) -> Recommended
    4 battery above its target SOC              -> Recommended
    5 otherwise                                 -> Normal

The standard's timing limits are enforced HERE, on the sender: every state
stays at least ``min_state_s``; one Block lasts at most ``max_block_s``; at most
``max_blocks`` Blocks start in any rolling 24 hours. Because every state lasts
at least the minimum, "a state can only be re-entered that long after it ended"
follows from the first rule and needs no bookkeeping of its own.

The engine feeds ``step`` what it already knows (engine/hub_calculation.
_apply_sg_ready); the memory is a small dataclass kept in the load's runtime
dict, so a restart starts in Normal with no history.
"""
from __future__ import annotations

from dataclasses import dataclass, field

BLOCK = "block"
NORMAL = "normal"
RECOMMENDED = "recommended"
FORCE = "force"

# (relay 1, relay 2) closed?
RELAYS = {
    BLOCK: (True, False),
    NORMAL: (False, False),
    RECOMMENDED: (False, True),
    FORCE: (True, True),
}
STATE_OF_RELAYS = {v: k for k, v in RELAYS.items()}

# Intermediate-state preference when both relays must change (best first): a
# failed second call then leaves the pump in the mildest valid state.
_MILDNESS = (NORMAL, RECOMMENDED, BLOCK, FORCE)

DAY_S = 24 * 3600.0
# Noise filter on a BREAKER overload (s): it must hold this long before Block. Far
# under the ~90 s a Slovenian meter/breaker tolerates above 110 % of its rating. The
# input is already smoothed by the engine's EMA (time constant EMA_TAU_S, 5.6 s by
# default, ~13 s to 90 % of a step), so the total reaction is about 13 s + this + one
# cycle, a quarter of what the breaker allows.
BREAKER_HOLD_S = 10.0
# A MAX IMPORT POWER overload (the 15-minute-block limit) must have held this long
# (owner, 2026-10-19): a short peak inside the block costs nothing, a sustained one does.
IMPORT_HOLD_S = 120.0
# Open question for the owner (JUG-24 point 4): should the pump also go to
# Recommended on the PV-clipping early start (hub option
# forecast_clip_early_enabled)? Following it is this one flag; the default
# follows the forecast-excess early start only.
FOLLOW_CLIP_EARLY = False


@dataclass(frozen=True)
class SgConfig:
    min_state_s: float = 600.0
    max_block_s: float = 7200.0
    max_blocks: int = 3
    breaker_hold_s: float = BREAKER_HOLD_S
    import_hold_s: float = IMPORT_HOLD_S


@dataclass
class SgMemory:
    """What the machine remembers. ``entered_at`` None = unknown (start-up):
    the state counts as long served."""

    state: str = NORMAL
    entered_at: float | None = None
    block_starts: list = field(default_factory=list)
    allowance_noted: bool = False
    # since when the breaker / the max import power has been over, None = not over
    breaker_since: float | None = None
    import_since: float | None = None


@dataclass(frozen=True)
class SgInputs:
    # The shortage (W) that reached this device, per cause; 0 when none
    # (see overloaded_devices). Debounced here, per cause (see ``step``).
    breaker_over_w: float = 0.0
    import_over_w: float = 0.0
    # Margin kept on top of the Normal draw before a Block is released (W): the
    # hub's Excess trigger margin, the integration's lead below a limit.
    release_margin_w: float = 0.0
    # Signed grid headroom (W) on the device's phases with the pump as it is now;
    # None = no grid limit to judge by (off-grid): a Block is then released at once.
    headroom_w: float | None = None
    # The pump's draw in Normal (last measured, else nominal), W.
    normal_draw_w: float = 0.0
    # The allocation granted the device's extra draw AND the site is in actual excess.
    actual_excess: bool = False
    # An early-start verdict applies to this device (already source-filtered).
    early_start: bool = False
    battery_above_target: bool = False
    # The relays can be read and obey. False -> Normal at once.
    relays_ok: bool = True


@dataclass(frozen=True)
class SgDecision:
    state: str
    reason: str
    blocks_24h: int
    min_left_s: float
    allowance_used_up: bool
    changed: bool


def blocks_in_day(mem: SgMemory, now: float) -> int:
    return sum(1 for t in mem.block_starts if now - t < DAY_S)


def can_block(mem: SgMemory, cfg: SgConfig, now: float) -> bool:
    """Is a Block still in the allowance (3 in the rolling 24 h)?"""
    return blocks_in_day(mem, now) < cfg.max_blocks


def relay_path(current: str, target: str) -> list[tuple[bool, bool]]:
    """The relay combinations to step through, ending at ``target``. Both relays
    differing: go through the mildest valid state, so a second call that fails
    leaves a safe one (never Block or Force on the way unless it is the goal)."""
    cur, tgt = RELAYS[current], RELAYS[target]
    if cur == tgt:
        return []
    if cur[0] == tgt[0] or cur[1] == tgt[1]:
        return [tgt]
    options = [(tgt[0], cur[1]), (cur[0], tgt[1])]
    middle = min(options, key=lambda c: _MILDNESS.index(STATE_OF_RELAYS[c]))
    return [middle, tgt]


def overloaded_devices(shortage_w: float, devices) -> set:
    """Which devices the shortage reaches, in priority order.

    ``devices``: ``(id, rank, normal_draw_w, state, can_relieve)`` per SG device;
    a higher rank number = lower priority = blocked first. A device already in
    Block is no relief (its draw is gone from the shortage). Lower-ranked
    devices that can still block take their Normal draw off the shortage before
    the next one is asked.
    """
    remaining, hit = shortage_w, set()
    for dev_id, _rank, draw_w, state, relieves in sorted(devices, key=lambda d: -d[1]):
        if remaining <= 0:
            break
        hit.add(dev_id)
        if state != BLOCK and relieves:
            remaining -= draw_w
    return hit


def _enter(mem: SgMemory, state: str, now: float) -> None:
    mem.state = state
    mem.entered_at = now
    if state == BLOCK:
        mem.block_starts = [t for t in mem.block_starts if now - t < DAY_S] + [now]


def _label(state: str, inp: SgInputs) -> str:
    if state == RECOMMENDED:
        return "predicted excess" if inp.early_start else "battery above target"
    return {BLOCK: "overload", NORMAL: "battery below target or no excess", FORCE: "excess"}[state]


def step(mem: SgMemory, inp: SgInputs, cfg: SgConfig, now: float) -> SgDecision:
    """One cycle: the state the pump should be in. Mutates ``mem``."""
    before = mem.state
    used_up = False
    held = ""

    def served() -> float:
        return float("inf") if mem.entered_at is None else now - mem.entered_at

    min_left = max(0.0, cfg.min_state_s - served())

    if not inp.relays_ok:
        # Safe state, no waiting: the relays cannot be trusted.
        if mem.state != NORMAL:
            _enter(mem, NORMAL, now)
        return SgDecision(NORMAL, "relay not available", blocks_in_day(mem, now), 0.0, False, before != NORMAL)

    # Overload per cause. The breaker is safety: a short noise filter, and it
    # does not wait for the minimum time of the state it interrupts. The max
    # import power is tariff: over for import_hold_s without a break.
    mem.breaker_since = (mem.breaker_since if mem.breaker_since is not None else now) if inp.breaker_over_w > 0 else None
    mem.import_since = (mem.import_since if mem.import_since is not None else now) if inp.import_over_w > 0 else None
    urgent = mem.breaker_since is not None and now - mem.breaker_since >= cfg.breaker_hold_s
    overloaded = urgent or (
        mem.import_since is not None and now - mem.import_since >= cfg.import_hold_s
    )

    # What the site asks for.
    if overloaded:
        if can_block(mem, cfg, now) or mem.state == BLOCK:
            want, reason = BLOCK, "overload"
        else:
            # Allowance used up: the overload goes unanswered here.
            want, reason, used_up = NORMAL, "block allowance used up", True
    elif inp.actual_excess:
        want, reason = FORCE, "excess"
    elif inp.early_start:
        want, reason = RECOMMENDED, "predicted excess"
    elif inp.battery_above_target:
        want, reason = RECOMMENDED, "battery above target"
    else:
        want, reason = NORMAL, "no excess"

    if mem.state == BLOCK:
        long_enough = served() >= cfg.min_state_s
        too_long = served() >= cfg.max_block_s
        if too_long:
            # A block at its maximum: Normal, never another Block straight away.
            want, reason, used_up = NORMAL, "block at its maximum time", True
        elif want == BLOCK:
            pass  # stay
        elif not long_enough:
            want, held = BLOCK, "minimum time"
        else:
            need = inp.normal_draw_w + inp.release_margin_w
            if inp.headroom_w is not None and inp.headroom_w < need:
                want, held = BLOCK, "waiting for headroom"
    elif want != mem.state and served() < cfg.min_state_s and not (urgent and want == BLOCK):
        want, held = mem.state, "minimum time"

    if want != mem.state:
        _enter(mem, want, now)
    if used_up and not mem.allowance_noted:
        mem.allowance_noted = True
    elif not used_up:
        mem.allowance_noted = False

    if held:
        reason = f"{_label(mem.state, inp)} (held: {held})"
    min_left = max(0.0, cfg.min_state_s - served())
    return SgDecision(mem.state, reason, blocks_in_day(mem, now), min_left, used_up, mem.state != before)
