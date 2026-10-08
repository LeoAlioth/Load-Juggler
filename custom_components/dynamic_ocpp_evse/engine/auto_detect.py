"""Auto-detection of grid CT inversion and phase mapping misconfigurations.

Called once per hub calculation cycle from engine/hub_calculation.py.
State lives in hub_runtime["_auto_detect"] - functions are stateless.
Returns notification payload dicts; the async caller fires them.
"""

from __future__ import annotations

import logging
import time

from ..phases import phase_mapping, support

_LOGGER = logging.getLogger(__name__)

# --- Inversion detection parameters ---
# A grid meter cannot read export on a phase where our loads draw current
# while nothing on the site generates: no sun, no battery discharging. A
# reading that does it for _INV_HOLD_S is reversed - physics, not a
# correlation. It replaced one (2026-10-08): every cycle compared the grid's
# change with the managed draw's change and counted opposite signs, 10 of 15
# firing. A charger's reading steps once per meter value (30 s) while the
# grid's change in that one cycle is house load, or Load Juggler's own answer
# to it; on Andrej's site 11 of 24 samples read "inverted" over 20 hours, a
# coin toss, and with 10 of 15 in reach of one, two correctly wired sites were
# warned after their restarts.
_INV_MIN_DRAW_A = 3.0     # Our loads' measured draw on the phase (A)
_INV_GEN_IDLE_W = 50.0    # Solar / battery discharge at or below this is none
_INV_HOLD_S = 120.0       # How long the export must hold

# --- Phase mapping detection parameters ---
_PM_MIN_DELTA_A = 0.5       # Minimum draw / grid-phase delta (A) to correlate
_PM_NOTIFY_SCORE = 6.0      # Weighted score threshold for notification
_PM_REMAP_SCORE = 15.0      # Weighted score threshold for auto-remap
_PM_CONFIDENCE = 0.70       # Required ratio (best / total)
_PM_DECAY_FACTOR = 0.5      # Score multiplier on inconclusive data
_PM_DECAY_THRESHOLD = 10.0  # Total score before decay triggers
_PM_WEIGHT_CAP = 15.0       # Max |delta_draw| for weight calc
_PM_WEIGHT_DIVISOR = 5.0    # Denominator: weight = min(|delta|, cap) / divisor
_PM_LINE_ACTIVE_A = 1.0     # Min current (A) to consider a load line active


# ------------------------------------------------------------------ #
# Feature 1: Grid CT Inversion Detection
# ------------------------------------------------------------------ #

def _nothing_generates(site) -> bool:
    """True when the site is known to produce nothing: its solar is measured
    (not worked out from the grid meter) and idle, and its battery - if it has
    one - is not discharging. An unread battery may be."""
    if not site.solar_is_metered or site.solar_production_total > _INV_GEN_IDLE_W:
        return False
    if site.battery_power is None:
        return site.battery_soc is None
    return site.battery_power <= _INV_GEN_IDLE_W


def check_inversion(state: dict, smoothed_phases: list, site,
                    hub_entry_id: str, hub_name: str, now=None) -> dict | None:
    """Detect a reversed grid reading: export on a phase where our loads draw
    while nothing on the site generates, held for _INV_HOLD_S.

    Only measured draws count - a draw assumed in place of a reading
    (``draw_blind``) is Load Juggler's own guess. Returns a notification dict
    once per Home Assistant run, or None.
    """
    inv = state.setdefault("inversion", {"since": {}, "notified": False})
    if inv["notified"] or site.is_off_grid:
        return None
    now = time.monotonic() if now is None else now
    since = inv.setdefault("since", {})

    draws = [0.0, 0.0, 0.0]
    if _nothing_generates(site):
        for load in site.loads:
            if load.dynamic_control and not load.draw_blind:
                for i, draw in enumerate(load.get_site_phase_draw()):
                    draws[i] += draw

    reversed_phases = []
    for i, label in enumerate(("A", "B", "C")):
        grid = smoothed_phases[i] if i < len(smoothed_phases) else None
        if (grid is None or draws[i] < _INV_MIN_DRAW_A
                or grid > -draws[i] / 2):
            since.pop(label, None)
            continue
        started = since.setdefault(label, now)
        if now - started >= _INV_HOLD_S:
            reversed_phases.append((label, grid, draws[i]))

    if not reversed_phases:
        return None
    inv["notified"] = True
    labels = ", ".join(label for label, _, _ in reversed_phases)
    evidence = "; ".join(
        f"phase {label} read {-grid:.1f} A of export with {draw:.1f} A drawn"
        for label, grid, draw in reversed_phases
    )
    _LOGGER.warning(
        "AutoDetect: grid reading reversed on hub '%s' - %s, nothing generating, "
        "for over %.0f s", hub_name, evidence, _INV_HOLD_S,
    )
    return {
        "title": "Load Juggler \u2014 Possible Grid CT Inversion",
        "message": (
            f"The grid current sensors for hub '{hub_name}' read export on "
            f"phase {labels} for over {_INV_HOLD_S / 60:.0f} minutes while "
            "Load Juggler's loads drew current there and nothing on the site "
            f"was producing - no solar, no battery discharging ({evidence}). "
            "A site cannot export power it is not making, so those readings "
            "are reversed.\n\n"
            "If every phase is affected, go to:\n"
            "Settings \u2192 Devices & Services \u2192 Load Juggler \u2192 "
            f"'{hub_name}' \u2192 Configure \u2192 Grid Settings \u2192 "
            "'Invert phase readings' and switch it.\n\n"
            "If only some phases are, the CT clamp on those phases is "
            "installed backwards - its arrow should point toward the grid."
        ),
        "notification_id": f"dynamic_ocpp_evse_grid_inversion_{hub_entry_id}",
    }


# ------------------------------------------------------------------ #
# Feature 2: Phase Mapping Detection
# ------------------------------------------------------------------ #

def check_phase_mapping(state: dict, smoothed_phases: list, loads: list,
                        hub_entry_id: str) -> list[dict]:
    """Detect phase mapping mismatches for loads on multi-phase sites.

    Uses confidence-weighted scoring: stronger signals (larger delta_draw)
    accumulate more points, allowing fast detection during oscillation
    (wrong mapping → start/stop cycling) while remaining cautious with
    small changes.

    Two complementary detection methods:
    - 1-phase car: correlates total draw with per-phase grid changes
      → identifies which site phase L1 is connected to.
    - 2-phase car: finds the grid phase that does NOT correlate with draw
      → identifies which site phase the inactive load line is on.

    After both a 1-phase and 2-phase car have charged, the complete
    L1/L2/L3 → A/B/C mapping is verified.

    Returns a list of notification dicts (one per mismatched load).
    """
    # Need 3-phase site (otherwise nothing to mis-map)
    if sum(1 for p in smoothed_phases if p is not None) < 3:
        return []

    pm_state = state.setdefault("phase_map", {})
    notifications = []

    grid_a = smoothed_phases[0] if smoothed_phases[0] is not None else 0.0
    grid_b = smoothed_phases[1] if smoothed_phases[1] is not None else 0.0
    grid_c = smoothed_phases[2] if smoothed_phases[2] is not None else 0.0

    for load in loads:
        total_draw = load.l1_current + load.l2_current + load.l3_current
        is_active = load.connector_status in (
            "Charging", "SuspendedEVSE", "SuspendedEV",
        )

        notif = _check_draw_phase_correlation(
            pm_state, grid_a, grid_b, grid_c, total_draw,
            load, hub_entry_id, is_active,
        )
        if notif:
            notifications.append(notif)

    return notifications


# --- Helpers ---

def _detect_inactive_line(load) -> str:
    """Return the load line with the lowest current ("l1", "l2", or "l3")."""
    currents = {
        "l1": load.l1_current,
        "l2": load.l2_current,
        "l3": load.l3_current,
    }
    return min(currents, key=lambda k: currents[k])


def _evaluate_score(score: dict, line: str, configured: dict):
    """Check score-based confidence for phase mapping detection.

    Uses weighted scores instead of flat sample counts.  Higher delta_draw
    values contribute more points, allowing strong signals (oscillation)
    to trigger faster. ``score`` is the evidence that ``line`` sits on each
    site phase, ``configured`` the load's lines as configured.

    Returns:
        dict: the mapping the score supports - ``configured`` itself when
            it agrees, otherwise ``line`` on its phase and swapped with the
            line that had that phase
        False: inconclusive - caller should apply soft decay
        None: not enough data yet
    """
    total = sum(score.values())
    if total < _PM_NOTIFY_SCORE:
        return None
    votes = {line: score}
    mapping = phase_mapping(votes, "ABC", current=configured)
    if support(votes, mapping) < _PM_CONFIDENCE:
        if total >= _PM_DECAY_THRESHOLD:
            return False  # enough data but noisy → decay
        return None  # moderate data, keep collecting
    return mapping


def _handle_mismatch(cs: dict, load, hub_entry_id: str, line: str,
                     configured: dict, mapping: dict,
                     best_score: float, notify_key: str) -> dict | None:
    """Handle a detected phase mismatch - notify (stage 1) or auto-remap (stage 2).

    Returns a notification dict, or None if waiting for more confidence.
    """
    cid = load.load_id
    detected_phase, configured_phase = mapping[line], configured[line]
    line_label = line.upper()
    # --- Stage 1: Notification ---
    if not cs.get(notify_key, False):
        cs[notify_key] = True
        _LOGGER.warning(
            "AutoDetect: Phase mismatch for %s. %s configured: %s, detected: %s "
            "(score: %.1f, remap at %.1f)",
            load.entity_id, line_label, configured_phase, detected_phase,
            best_score, _PM_REMAP_SCORE,
        )
        return {
            "title": (
                f"Load Juggler \u2014 Phase Mismatch: "
                f"{load.entity_id}"
            ),
            "message": (
                f"Charger '{load.entity_id}' line {line_label} is connected "
                f"to site **Phase {detected_phase}**, but is mapped to "
                f"**Phase {configured_phase}**.\n\n"
                f"To fix this manually, change '{line_label} \u2192 Site Phase' "
                f"from **{configured_phase}** to **{detected_phase}** in:\n"
                "Settings \u2192 Devices & Services \u2192 Load Juggler "
                f"\u2192 '{load.entity_id}' \u2192 Configure.\n\n"
                "If no action is taken, the mapping will be auto-corrected "
                "once sufficient confidence is reached."
            ),
            "notification_id": (
                f"dynamic_ocpp_evse_phase_map_{hub_entry_id}_{cid}"
            ),
        }

    # --- Stage 2: Auto-remap ---
    if best_score < _PM_REMAP_SCORE:
        return None

    # a single-phase load's mapping names L1 only; L2/L3 stay as they are
    remap = {f"{k}_phase": mapping.get(k, getattr(load, f"{k}_phase"))
             for k in ("l1", "l2", "l3")}
    cs["remapped"] = True
    _LOGGER.warning(
        "AutoDetect: Auto-remapping %s (score: %.1f). "
        "L1:%s\u2192%s L2:%s\u2192%s L3:%s\u2192%s",
        load.entity_id, best_score,
        load.l1_phase, remap["l1_phase"],
        load.l2_phase, remap["l2_phase"],
        load.l3_phase, remap["l3_phase"],
    )
    return {
        "title": (
            f"Load Juggler \u2014 Phase Mapping Auto-Corrected: "
            f"{load.entity_id}"
        ),
        "message": (
            f"Phase mapping for '{load.entity_id}' was automatically "
            f"corrected.\n\n"
            f"L1: {load.l1_phase} \u2192 {remap['l1_phase']}\n"
            f"L2: {load.l2_phase} \u2192 {remap['l2_phase']}\n"
            f"L3: {load.l3_phase} \u2192 {remap['l3_phase']}\n\n"
            "To make this permanent, update the charger configuration.\n"
            "This auto-correction resets on restart."
        ),
        "notification_id": (
            f"dynamic_ocpp_evse_phase_map_{hub_entry_id}_{cid}"
        ),
        "auto_remap": {
            "load_id": cid,
            "l1_phase": remap["l1_phase"],
            "l2_phase": remap["l2_phase"],
            "l3_phase": remap["l3_phase"],
        },
    }


# --- Main correlation logic ---

def _check_draw_phase_correlation(pm_state: dict,
                                  grid_a: float, grid_b: float, grid_c: float,
                                  total_draw: float,
                                  load, hub_entry_id: str,
                                  is_active: bool) -> dict | None:
    """Detect phase mapping by correlating load draw with grid phases.

    Uses confidence-weighted scoring: stronger signals (larger delta_draw)
    accumulate more points, allowing fast detection during oscillation
    (wrong mapping → start/stop cycling) while remaining cautious with
    small changes.

    Weight per sample = min(|delta_draw|, 15) / 5  (range 0.1 – 3.0)

    Handles two complementary scenarios:
    - 1-phase car (1 active line): one grid phase correlates with draw changes
      → identifies which site phase L1 is connected to.
    - 2-phase car (2 active lines): one grid phase does NOT correlate
      → identifies which site phase the inactive line is connected to.
    - 3-phase car (3 active lines): symmetric draw, all phases correlate
      equally → inconclusive → skipped (mapping irrelevant for symmetric).

    Always updates prev snapshots (even when inactive) for accurate deltas.
    """
    cid = load.load_id
    cs = pm_state.setdefault(cid, {
        "prev_draw": 0.0,
        "prev_grid_a": 0.0, "prev_grid_b": 0.0, "prev_grid_c": 0.0,
        # 1-phase tracking: which grid phase correlates with draw
        "score": {"A": 0.0, "B": 0.0, "C": 0.0},
        # 2-phase tracking: which grid phase does NOT correlate
        "score_2ph": {"A": 0.0, "B": 0.0, "C": 0.0},
        "inactive_line": None,
        # Control flags
        "notify_sent_1ph": False,
        "notify_sent_2ph": False,
        "confirmed_1ph": False,
        "confirmed_2ph": False,
        "remapped": False,
    })

    # Done: remap issued (state reset externally) or both types confirmed
    if cs["remapped"]:
        return None
    if cs["confirmed_1ph"] and cs["confirmed_2ph"]:
        return None

    delta_draw = total_draw - cs["prev_draw"]
    delta_g = {
        "A": grid_a - cs["prev_grid_a"],
        "B": grid_b - cs["prev_grid_b"],
        "C": grid_c - cs["prev_grid_c"],
    }

    # --- Accumulate weighted scores based on active line count ---
    if is_active and abs(delta_draw) >= _PM_MIN_DELTA_A:
        weight = min(abs(delta_draw), _PM_WEIGHT_CAP) / _PM_WEIGHT_DIVISOR
        active_lines = sum(
            1 for c in (load.l1_current, load.l2_current,
                        load.l3_current)
            if c > _PM_LINE_ACTIVE_A
        )
        if active_lines == 1:
            # Single-phase: track which grid phase correlates
            for phase, dg in delta_g.items():
                if abs(dg) >= _PM_MIN_DELTA_A and (delta_draw > 0) == (dg > 0):
                    cs["score"][phase] += weight
            _LOGGER.debug(
                "AutoDetect 1ph %s: delta=%.1fA weight=%.2f "
                "scores=A:%.1f B:%.1f C:%.1f",
                load.entity_id, delta_draw, weight,
                cs["score"]["A"], cs["score"]["B"], cs["score"]["C"],
            )

        elif active_lines == 2 and load.phases >= 3:
            # Two-phase: track which grid phase does NOT correlate
            inactive = _detect_inactive_line(load)
            # Reset if inactive line changed (different car)
            if (cs["inactive_line"] is not None
                    and cs["inactive_line"] != inactive):
                cs["score_2ph"] = {"A": 0.0, "B": 0.0, "C": 0.0}
                cs["notify_sent_2ph"] = False
                cs["confirmed_2ph"] = False
            cs["inactive_line"] = inactive
            # Phase with smallest |delta| is where the inactive line sits
            min_phase = min(delta_g, key=lambda p: abs(delta_g[p]))
            cs["score_2ph"][min_phase] += weight
            _LOGGER.debug(
                "AutoDetect 2ph %s: delta=%.1fA weight=%.2f inactive=%s "
                "scores=A:%.1f B:%.1f C:%.1f",
                load.entity_id, delta_draw, weight, inactive,
                cs["score_2ph"]["A"], cs["score_2ph"]["B"],
                cs["score_2ph"]["C"],
            )

        # active_lines == 3: symmetric draw, mapping irrelevant → skip

    # Always update snapshots so transitions are visible next cycle
    cs["prev_draw"] = total_draw
    cs["prev_grid_a"] = grid_a
    cs["prev_grid_b"] = grid_b
    cs["prev_grid_c"] = grid_c

    # --- Evaluate: the 1-phase car's L1, then the 2-phase car's idle line ---
    for kind, score_key, line in (("1ph", "score", "l1"),
                                  ("2ph", "score_2ph", cs.get("inactive_line"))):
        if cs[f"confirmed_{kind}"] or not line:
            continue
        score = cs[score_key]
        configured = {"l1": load.l1_phase, "l2": load.l2_phase, "l3": load.l3_phase}
        if (kind == "1ph" and load.active_phases_mask
                and len(load.active_phases_mask) == 1):
            configured = {"l1": load.active_phases_mask}  # plug
        mapping = _evaluate_score(score, line, configured)
        if mapping is False:
            # Soft decay instead of hard reset. The notify flag is intentionally
            # NOT reset here - clearing it makes the same mismatch notification
            # re-fire every time the score oscillates around the threshold.
            for p in score:
                score[p] *= _PM_DECAY_FACTOR
            _LOGGER.debug(
                "AutoDetect %s for %s: inconclusive, decaying scores "
                "(A:%.1f B:%.1f C:%.1f)",
                kind, load.entity_id, score["A"], score["B"], score["C"],
            )
        elif mapping == configured:
            cs[f"confirmed_{kind}"] = True
            _LOGGER.debug(
                "AutoDetect: %s for %s confirmed on phase %s",
                line.upper(), load.entity_id, configured[line],
            )
        elif mapping is not None:
            return _handle_mismatch(
                cs, load, hub_entry_id, line, configured, mapping,
                score[mapping[line]], f"notify_sent_{kind}",
            )

    # Log when full mapping is verified
    if cs["confirmed_1ph"] and cs["confirmed_2ph"]:
        inactive_line = cs.get("inactive_line", "")
        inactive_label = inactive_line.upper() if inactive_line else "?"
        inactive_phase = (
            getattr(load, f"{inactive_line}_phase", "?")
            if inactive_line else "?"
        )
        _LOGGER.info(
            "AutoDetect: Full phase mapping verified for %s "
            "(L1\u2192%s, %s\u2192%s, remaining line by elimination)",
            load.entity_id, load.l1_phase,
            inactive_label, inactive_phase,
        )

    return None
