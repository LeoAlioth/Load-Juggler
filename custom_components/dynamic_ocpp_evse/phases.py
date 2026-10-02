"""Which of a device's sensors are its per-phase meter readings, and which
phase each of a meter's channels really carries. Pure, stdlib only.

Canonical here in Load Insights. Load Juggler
(``custom_components/dynamic_ocpp_evse/phases.py``) carries a verbatim copy,
kept in sync by hand: change this file, then copy it there whole -
tests/test_phases.py fails while the two differ.

Picking a meter should be picking a DEVICE - Home Assistant already knows
which entities belong to which meter - so this reads a device's sensors
and works out which is the active power on phase A, the power factor on C,
and so on. Every integration names them differently, and several of the
names are traps:

  * a LINE-TO-LINE voltage (``..._voltage_ab``) is phase A's voltage as much
    as phase B's, so it is neither's - but the LINE-TO-NEUTRAL one beside it
    (``..._voltage_an``) is exactly phase A's own voltage, and a SolarEdge
    meter publishes both sets;
  * a TOTAL (``total_active_power``) is not a phase at all;
  * ``l1 / l2 / l3`` and ``a / b / c`` are the same three phases;
  * min / max / peak / daily variants sit beside the live reading.

So a candidate is rejected outright when it is a total or a line-to-line
pair, and otherwise scored: the plainest name for each (kind, phase) wins.
What comes out is shown to the user for confirmation, never applied blind.
"""
from __future__ import annotations

import itertools
import os
import re
from typing import Dict, Mapping, Optional, Sequence

# device_class -> our field prefix
KIND_BY_DEVICE_CLASS = {
    "power": "power",
    "power_factor": "pf",
    "current": "current",
    "voltage": "voltage",
    # the meter's own, SIGNED: V x I gives only its size, so a capacitive load
    # on an inductive floor reads wrong (Anze, 2026-09-30)
    "reactive_power": "var",
    # the meter's own apparent power: a Pro 3EM publishes it to 0.1 VA, where
    # its two-decimal power factor moves the derived VAr by ~26 at 900 W
    # (Anze, 2026-09-30: small steps read far poorer factors than big ones)
    "apparent_power": "va",
}

# never a per-phase live reading
REJECT = (
    "total", "sum", "average", "avg", "combined", "aggregate",
    "today", "yesterday", "daily", "monthly", "yearly", "lifetime",
    "min", "max", "peak", "energy", "cost", "frequency", "temperature",
)
# two phases named at once, plus a neutral that names none. "ac" is
# deliberately absent: SolarEdge prefixes every reading with it ("AC Current
# A"), so it means alternating current far more often than phases A-to-C.
REJECT_PAIRS = ("ab", "bc", "ca", "ac_ab", "ln", "nl",
                "l1_l2", "l2_l3", "l3_l1", "l2_l1", "l3_l2", "l1_l3")
# allowed, but a plainer candidate beats them
PENALTY = {"import": 6, "export": 6, "returned": 6, "delivered": 6, "reactive": 20,
           "apparent": 20, "fundamental": 10, "harmonic": 20, "raw": 4, "filtered": 4,
           # An inverter publishes both of its sides on one device. What the
           # house DRAWS is the output; the input is the grid or the
           # generator. Without this the shorter name simply won, and
           # Kozolec - which is OFF GRID, so its AC input is zero by
           # definition - spent its backfill watching a flat line and
           # learned nothing at all (Anze, 2026-09-17).
           "input": 8, "ac_in": 8}
# Enough to outrank any difference in NAME LENGTH, which is at most a few
# dozen, and deliberately NOT enough to outrank a role preference, which is
# worth ten times its weight. The ordering matters: a MultiPlus publishes
# power, current and voltage on its AC input as well as its output, and at
# Kozolec that input is a generator port sitting at zero. Coherence must
# never talk us onto the wrong side of an inverter - it breaks ties, it does
# not decide which circuit we want (Anze, 2026-09-18).
COHERENT_BONUS = 50
BONUS = {"output": 6, "ac_out": 6, "out": 4, "load": 4, "loads": 4, "consumption": 4}
# The same device usually publishes both sides, so which one is wanted
# depends on what it is being asked for: picking the meter for the GRID
# connection wants the opposite preference to picking it for the house.
ROLES = {
    "load": (PENALTY, BONUS),
    "grid": ({k: v for k, v in PENALTY.items() if k not in ("input", "ac_in")},
             # net over gross: an Enphase gateway publishes
             # current_power_consumption_l1 (what the house draws, never
             # negative) beside current_net_power_consumption_l1 (what crosses
             # the grid connection, signed), and the shorter name won
             {"input": 6, "ac_in": 6, "grid": 6, "mains": 4, "utility": 4, "net": 4}),
}

_L = re.compile(r"(?:^|[_\s])l([123])(?:$|[_\s])")
_PHASE = re.compile(r"(?:^|[_\s])phase[_\s]?([abc123])(?:$|[_\s])")
_ABC = re.compile(r"(?:^|[_\s])([abc])(?:$|[_\s])")
_CH = re.compile(r"(?:^|[_\s])(?:ch|channel)[_\s]?([abc123])(?:$|[_\s])")
# line to NEUTRAL: one phase against the star point, which is that phase's
# own voltage - "an" -> a, "l1n" -> a
_AN = re.compile(r"(?:^|[_\s])([abc])n(?:$|[_\s])")
_LN = re.compile(r"(?:^|[_\s])l([123])n(?:$|[_\s])")
# the separated spelling of a line-to-line pair, "a_b" beside "ab"
_SEP_PAIR = re.compile(r"(?:^|_)l?([abc])_l?([abc])(?:$|_)")
_DIGIT_TO_PHASE = {"1": "a", "2": "b", "3": "c"}


def _tokens(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (text or "").lower())


def phase_of(text: str) -> Optional[str]:
    """'a', 'b', 'c' - or None when the name names no single phase."""
    t = _tokens(text)
    for pair in REJECT_PAIRS:
        if re.search(rf"(?:^|_)(?:l{{0,1}}){pair}(?:$|_)", t):
            return None
    if _SEP_PAIR.search(t):
        return None
    for rx in (_PHASE, _CH):
        m = rx.search(t)
        if m:
            g = m.group(1)
            return _DIGIT_TO_PHASE.get(g, g)
    m = _AN.search(t)
    if m:
        return m.group(1)
    for rx in (_L, _LN):
        m = rx.search(t)
        if m:
            return _DIGIT_TO_PHASE[m.group(1)]
    m = _ABC.search(t)
    if m:
        return m.group(1)
    return None


def _score(entity_id: str, name: str, role: str = "load") -> Optional[int]:
    t = _tokens(f"{entity_id} {name}")
    # Rejected by what the reading is called itself, not by its device: with
    # has_entity_name the id starts with the device's name, and the readings
    # of an "ET340 Energy Meter" are not energy readings (Victron GX,
    # 2026-10-02). With no name of its own, the id is all there is.
    own = _tokens(name) if name else t
    for bad in REJECT:
        if re.search(rf"(?:^|_){bad}(?:$|_)", own):
            return None
    penalty, bonus = ROLES.get(role, ROLES["load"])
    score = 1000 - len(entity_id)
    for word, cost in penalty.items():
        if re.search(rf"(?:^|_){word}(?:$|_)", t):
            score -= cost * 10
    for word, gain in bonus.items():
        if re.search(rf"(?:^|_){word}(?:$|_)", t):
            score += gain * 10
    return score


def match_meter_entities(entities: Sequence[dict], role: str = "load") -> Dict[str, str]:
    """``entities`` are dicts with entity_id, device_class and name.
    Returns {"power_a": entity_id, "pf_c": ..., ...} - only what it is sure of.

    ``role`` says which side of an inverter is wanted: "load" for what the
    house draws, "grid" for the connection to the utility."""
    scored = []                      # (kind, phase, score, eid, device)
    for e in entities:
        kind = KIND_BY_DEVICE_CLASS.get((e.get("device_class") or "").lower())
        if not kind:
            continue
        eid, name = e.get("entity_id") or "", e.get("name") or ""
        # the phase may be named in either the id or the friendly name
        phase = phase_of(eid) or phase_of(name)
        if not phase:
            continue
        score = _score(eid, name, role)
        if score is None:
            continue
        scored.append((kind, phase, score, eid, e.get("device_id")))
    # Which devices publish voltage AND current (or a power factor) for a
    # phase. A power reading with those beside it is worth more than one
    # without, because its volts and amps are what make a power factor a
    # power factor - and it is a better tiebreak than the length of the name,
    # which is what decided between two readings of the same house at Kozolec
    # (Anze, 2026-09-18). Rows carrying no device are all one device, which
    # is how the config flow calls this.
    offers = {}
    for kind, phase, _, _, device in scored:
        offers.setdefault((device, phase), set()).add(kind)
    best: Dict[str, tuple] = {}
    for kind, phase, score, eid, device in scored:
        beside = offers.get((device, phase), set())
        if kind == "power" and ("pf" in beside or {"voltage", "current"} <= beside):
            score += COHERENT_BONUS
        key = f"{kind}_{phase}"
        if key not in best or score > best[key][0]:
            best[key] = (score, eid)
    return {k: v[1] for k, v in sorted(best.items())}


def closest_by_name(candidates: Sequence[str], reference: str) -> Optional[str]:
    """Of several readings on one device, the one whose name runs alongside
    ``reference`` longest.

    A meter that publishes both sides of an inverter offers watts for each,
    and the amps we hold belong to exactly one of them: ``mp_output_current_l1``
    goes with ``mp_output_power_l1``, never with the input's. Matching on the
    shared prefix keeps the pair on one circuit without having to know that
    "output" is the word that matters (Anze, 2026-09-18)."""
    if not candidates:
        return None
    return max(sorted(candidates), key=lambda eid: len(os.path.commonprefix([eid, reference])))


def beside(rows: Sequence[dict], reference: str, kind: str, phase: str) -> Optional[str]:
    """The device's ``kind`` reading on ``phase`` whose name runs alongside
    ``reference`` - the watts that go with a meter's amps, the reactive power
    that goes with its watts. ``rows`` are one device's, in the shape
    match_meter_entities reads; ``kind`` is a field prefix ("power", "var").
    Each row is matched on its own, so only REJECT can turn one away: a role
    weighs candidates against each other and there is only the one."""
    key = f"{kind}_{phase}"
    return closest_by_name([r["entity_id"] for r in rows
                            if match_meter_entities([r]).get(key)], reference)


def phase_mapping(votes: Mapping[str, Mapping[str, float]], phases: Sequence[str] = ("a", "b", "c"),
                  min_votes: float = 0, current: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    """Which phase each of a meter's own channels carries.

    ``votes[channel][phase]`` is the evidence that the channel carries that
    phase - in Load Insights, sessions that started on that channel and on
    that house phase at the same moment and the same size. The answer is a
    PERMUTATION - each channel its own phase - chosen to agree with the most
    votes, not a separate vote per channel: a two-phase load such as Home's
    kiln steps on two house phases at once, and one channel on its own would
    tie between them.

    ``current`` is what the channels are believed to carry now (default:
    each its own label). Only a strictly better answer replaces it: with
    fewer than ``min_votes`` votes in all, or on a tie, ``current`` stands,
    and between equally good answers the one relabelling fewest channels
    wins - one line found on another phase swaps with the line that had it.

    Load Juggler asks the same question about a charger's lines (Anze,
    2026-09-23): votes {"l1": {"A": .., "B": ..}}, phases "ABC", current the
    configured {"l1": "A", "l2": "B", "l3": "C"}."""
    prior = {c: c for c in votes}
    prior.update(current or {})
    channels = sorted(prior)
    best = tuple(prior[c] for c in channels)
    if not channels or sum(sum(r.values()) for r in votes.values()) < min_votes:
        return dict(zip(channels, best))

    def score(perm):
        return (sum((votes.get(c) or {}).get(p, 0) for c, p in zip(channels, perm)),
                sum(p == prior[c] for c, p in zip(channels, perm)))

    best_score = score(best)
    for perm in itertools.permutations(sorted(set(phases) | set(prior.values())), len(channels)):
        s = score(perm)
        if s > best_score:
            best, best_score = perm, s
    return dict(zip(channels, best))


def support(votes: Mapping[str, Mapping[str, float]], mapping: Mapping[str, str]) -> float:
    """The share of all votes ``mapping`` agrees with, 0..1 (0 with none)."""
    total = sum(sum(r.values()) for r in votes.values())
    return sum((votes.get(c) or {}).get(p, 0) for c, p in mapping.items()) / total if total else 0.0


def describe_match(found: Dict[str, str]) -> str:
    """One line for the confirmation form."""
    if not found:
        return "no per-phase readings recognised - fill them in below"
    kinds = {}
    for key in found:
        kind, phase = key.rsplit("_", 1)
        kinds.setdefault(kind, []).append(phase.upper())
    label = {"power": "power", "pf": "power factor", "current": "current", "voltage": "voltage",
             "var": "reactive power"}
    parts = [f"{label.get(k, k)} {'+'.join(sorted(v))}" for k, v in sorted(kinds.items())]
    return "found " + ", ".join(parts)
