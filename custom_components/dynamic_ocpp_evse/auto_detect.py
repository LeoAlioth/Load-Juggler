"""Auto-detection of grid CT inversion and phase mapping misconfigurations.

Called once per hub calculation cycle from dynamic_ocpp_evse.py.
State lives in hub_runtime["_auto_detect"] — functions are stateless.
Returns notification payload dicts; the async caller fires them.
"""

import logging

_LOGGER = logging.getLogger(__name__)

# --- Inversion detection parameters ---
_INV_MIN_DELTA_A = 1.0     # Minimum charger draw change (A) to count as significant
_INV_WINDOW_SIZE = 15       # Rolling window length (samples with significant delta)
_INV_THRESHOLD = 10         # Inversion signals needed in a full window to fire

# --- Phase mapping detection parameters ---
_PM_MIN_DELTA_A = 0.5       # Minimum draw / grid-phase delta (A) to correlate
_PM_NOTIFY_SCORE = 6.0      # Weighted score threshold for notification
_PM_REMAP_SCORE = 15.0      # Weighted score threshold for auto-remap
_PM_CONFIDENCE = 0.70       # Required ratio (best / total)
_PM_DECAY_FACTOR = 0.5      # Score multiplier on inconclusive data
_PM_DECAY_THRESHOLD = 10.0  # Total score before decay triggers
_PM_WEIGHT_CAP = 15.0       # Max |delta_draw| for weight calc
_PM_WEIGHT_DIVISOR = 5.0    # Denominator: weight = min(|delta|, cap) / divisor
_PM_LINE_ACTIVE_A = 1.0     # Min current (A) to consider a charger line active


# ------------------------------------------------------------------ #
# Feature 1: Grid CT Inversion Detection
# ------------------------------------------------------------------ #

def check_inversion(state: dict, smoothed_phases: list, chargers: list,
                    hub_entry_id: str, hub_name: str) -> dict | None:
    """Detect inverted grid CTs by correlating charger draw vs grid changes.

    Returns a notification dict or None.
    """
    inv = state.setdefault("inversion", {
        "prev_grid_total": None,
        "prev_charger_total": None,
        "window": [],
        "notified": False,
    })

    if inv["notified"]:
        return None

    # Grid total (signed): positive = import, negative = export
    grid_total = sum(p for p in smoothed_phases if p is not None)

    # Total charger draw across all site phases
    charger_total = 0.0
    for c in chargers:
        a, b, cc = c.get_site_phase_draw()
        charger_total += a + b + cc

    prev_grid = inv["prev_grid_total"]
    prev_draw = inv["prev_charger_total"]

    result = None
    try:
        if prev_grid is not None and prev_draw is not None:
            delta_grid = grid_total - prev_grid
            delta_draw = charger_total - prev_draw

            if abs(delta_draw) >= _INV_MIN_DELTA_A:
                if delta_draw * delta_grid < 0:
                    inv["window"].append(1)   # inversion signal
                else:
                    inv["window"].append(-1)  # normal signal

                # Trim to rolling window
                if len(inv["window"]) > _INV_WINDOW_SIZE:
                    inv["window"] = inv["window"][-_INV_WINDOW_SIZE:]

                inv_count = sum(1 for s in inv["window"] if s == 1)
                _LOGGER.debug(
                    "AutoDetect inversion: delta_draw=%.2fA, delta_grid=%.2fA, "
                    "signal=%s (%d/%d in window)",
                    delta_draw, delta_grid,
                    "INV" if delta_draw * delta_grid < 0 else "OK",
                    inv_count, len(inv["window"]),
                )

                if (len(inv["window"]) >= _INV_WINDOW_SIZE
                        and inv_count >= _INV_THRESHOLD):
                    inv["notified"] = True
                    _LOGGER.warning(
                        "AutoDetect: Grid CT inversion detected for hub '%s' "
                        "(%d/%d signals)", hub_name, inv_count, _INV_WINDOW_SIZE,
                    )
                    result = {
                        "title": "Dynamic OCPP EVSE \u2014 Possible Grid CT Inversion",
                        "message": (
                            f"Your grid current sensors for hub '{hub_name}' may be "
                            "installed backwards (inverted).\n\n"
                            "When EV charging increased, the measured grid import "
                            "decreased \u2014 the opposite of what is physically expected.\n\n"
                            "To fix this, go to:\n"
                            "Settings \u2192 Devices & Services \u2192 Dynamic OCPP EVSE \u2192 "
                            f"'{hub_name}' \u2192 Configure \u2192 Grid Settings \u2192 "
                            "enable 'Invert phase readings'.\n\n"
                            "If already enabled, your CT clamps may still be physically "
                            "reversed \u2014 check that the arrow on each clamp points "
                            "toward the grid."
                        ),
                        "notification_id": (
                            f"dynamic_ocpp_evse_grid_inversion_{hub_entry_id}"
                        ),
                    }
    finally:
        inv["prev_grid_total"] = grid_total
        inv["prev_charger_total"] = charger_total

    return result


# ------------------------------------------------------------------ #
# Feature 2: Phase Mapping Detection
# ------------------------------------------------------------------ #

def check_phase_mapping(state: dict, smoothed_phases: list, chargers: list,
                        hub_entry_id: str) -> list[dict]:
    """Detect phase mapping mismatches for chargers on multi-phase sites.

    Uses confidence-weighted scoring: stronger signals (larger delta_draw)
    accumulate more points, allowing fast detection during oscillation
    (wrong mapping → start/stop cycling) while remaining cautious with
    small changes.

    Two complementary detection methods:
    - 1-phase car: correlates total draw with per-phase grid changes
      → identifies which site phase L1 is connected to.
    - 2-phase car: finds the grid phase that does NOT correlate with draw
      → identifies which site phase the inactive charger line is on.

    After both a 1-phase and 2-phase car have charged, the complete
    L1/L2/L3 → A/B/C mapping is verified.

    Returns a list of notification dicts (one per mismatched charger).
    """
    # Need 3-phase site (otherwise nothing to mis-map)
    if sum(1 for p in smoothed_phases if p is not None) < 3:
        return []

    pm_state = state.setdefault("phase_map", {})
    notifications = []

    grid_a = smoothed_phases[0] if smoothed_phases[0] is not None else 0.0
    grid_b = smoothed_phases[1] if smoothed_phases[1] is not None else 0.0
    grid_c = smoothed_phases[2] if smoothed_phases[2] is not None else 0.0

    for charger in chargers:
        total_draw = charger.l1_current + charger.l2_current + charger.l3_current
        is_active = charger.connector_status in (
            "Charging", "SuspendedEVSE", "SuspendedEV",
        )

        notif = _check_draw_phase_correlation(
            pm_state, grid_a, grid_b, grid_c, total_draw,
            charger, hub_entry_id, is_active,
        )
        if notif:
            notifications.append(notif)

    return notifications


# --- Helpers ---

def _detect_inactive_line(charger) -> str:
    """Return the charger line with the lowest current ("l1", "l2", or "l3")."""
    currents = {
        "l1": charger.l1_current,
        "l2": charger.l2_current,
        "l3": charger.l3_current,
    }
    return min(currents, key=lambda k: currents[k])


def _evaluate_score(score: dict, notify_threshold: float):
    """Check score-based confidence for phase mapping detection.

    Uses weighted scores instead of flat sample counts.  Higher delta_draw
    values contribute more points, allowing strong signals (oscillation)
    to trigger faster.

    Returns:
        str: best phase (e.g., "A") if confident and total >= threshold
        False: inconclusive — caller should apply soft decay
        None: not enough data yet
    """
    total = sum(score.values())
    if total < notify_threshold:
        return None
    best = max(score, key=lambda p: score[p])
    confidence = score[best] / total if total > 0 else 0
    if confidence < _PM_CONFIDENCE:
        if total >= _PM_DECAY_THRESHOLD:
            return False  # enough data but noisy → decay
        return None  # moderate data, keep collecting
    return best


def _build_phase_swap(charger, line_key: str, detected_phase: str) -> dict:
    """Build a phase remap by swapping line_key to detected_phase.

    Swaps with whichever line currently occupies detected_phase to avoid
    duplicate phase assignments.
    """
    remap = {
        "l1_phase": charger.l1_phase,
        "l2_phase": charger.l2_phase,
        "l3_phase": charger.l3_phase,
    }
    configured_phase = remap[line_key]
    for key in ("l1_phase", "l2_phase", "l3_phase"):
        if remap[key] == detected_phase:
            remap[key] = configured_phase
            break
    remap[line_key] = detected_phase
    return remap


def _handle_mismatch(cs: dict, charger, hub_entry_id: str, cid: str,
                     line_key: str, detected_phase: str,
                     configured_phase: str, line_label: str,
                     best_score: float, notify_key: str) -> dict | None:
    """Handle a detected phase mismatch — notify (stage 1) or auto-remap (stage 2).

    Returns a notification dict, or None if waiting for more confidence.
    """
    # --- Stage 1: Notification ---
    if not cs.get(notify_key, False):
        cs[notify_key] = True
        _LOGGER.warning(
            "AutoDetect: Phase mismatch for %s. %s configured: %s, detected: %s "
            "(score: %.1f, remap at %.1f)",
            charger.entity_id, line_label, configured_phase, detected_phase,
            best_score, _PM_REMAP_SCORE,
        )
        return {
            "title": (
                f"Dynamic OCPP EVSE \u2014 Phase Mismatch: "
                f"{charger.entity_id}"
            ),
            "message": (
                f"Charger '{charger.entity_id}' line {line_label} is connected "
                f"to site **Phase {detected_phase}**, but is mapped to "
                f"**Phase {configured_phase}**.\n\n"
                f"To fix this manually, change '{line_label} \u2192 Site Phase' "
                f"from **{configured_phase}** to **{detected_phase}** in:\n"
                "Settings \u2192 Devices & Services \u2192 Dynamic OCPP EVSE "
                f"\u2192 '{charger.entity_id}' \u2192 Configure.\n\n"
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

    remap = _build_phase_swap(charger, line_key, detected_phase)
    cs["remapped"] = True
    _LOGGER.warning(
        "AutoDetect: Auto-remapping %s (score: %.1f). "
        "L1:%s\u2192%s L2:%s\u2192%s L3:%s\u2192%s",
        charger.entity_id, best_score,
        charger.l1_phase, remap["l1_phase"],
        charger.l2_phase, remap["l2_phase"],
        charger.l3_phase, remap["l3_phase"],
    )
    return {
        "title": (
            f"Dynamic OCPP EVSE \u2014 Phase Mapping Auto-Corrected: "
            f"{charger.entity_id}"
        ),
        "message": (
            f"Phase mapping for '{charger.entity_id}' was automatically "
            f"corrected.\n\n"
            f"L1: {charger.l1_phase} \u2192 {remap['l1_phase']}\n"
            f"L2: {charger.l2_phase} \u2192 {remap['l2_phase']}\n"
            f"L3: {charger.l3_phase} \u2192 {remap['l3_phase']}\n\n"
            "To make this permanent, update the charger configuration.\n"
            "This auto-correction resets on restart."
        ),
        "notification_id": (
            f"dynamic_ocpp_evse_phase_map_{hub_entry_id}_{cid}"
        ),
        "auto_remap": {
            "charger_id": cid,
            "l1_phase": remap["l1_phase"],
            "l2_phase": remap["l2_phase"],
            "l3_phase": remap["l3_phase"],
        },
    }


# --- Main correlation logic ---

def _check_draw_phase_correlation(pm_state: dict,
                                  grid_a: float, grid_b: float, grid_c: float,
                                  total_draw: float,
                                  charger, hub_entry_id: str,
                                  is_active: bool) -> dict | None:
    """Detect phase mapping by correlating charger draw with grid phases.

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
    cid = charger.charger_id
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
            1 for c in (charger.l1_current, charger.l2_current,
                        charger.l3_current)
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
                charger.entity_id, delta_draw, weight,
                cs["score"]["A"], cs["score"]["B"], cs["score"]["C"],
            )

        elif active_lines == 2 and charger.phases >= 3:
            # Two-phase: track which grid phase does NOT correlate
            inactive = _detect_inactive_line(charger)
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
                charger.entity_id, delta_draw, weight, inactive,
                cs["score_2ph"]["A"], cs["score_2ph"]["B"],
                cs["score_2ph"]["C"],
            )

        # active_lines == 3: symmetric draw, mapping irrelevant → skip

    # Always update snapshots so transitions are visible next cycle
    cs["prev_draw"] = total_draw
    cs["prev_grid_a"] = grid_a
    cs["prev_grid_b"] = grid_b
    cs["prev_grid_c"] = grid_c

    # --- Evaluate 1-phase detection ---
    if not cs["confirmed_1ph"]:
        result_1ph = _evaluate_score(cs["score"], _PM_NOTIFY_SCORE)
        if result_1ph is False:
            # Soft decay instead of hard reset
            for p in cs["score"]:
                cs["score"][p] *= _PM_DECAY_FACTOR
            cs["notify_sent_1ph"] = False
            _LOGGER.debug(
                "AutoDetect 1ph for %s: inconclusive, decaying scores "
                "(A:%.1f B:%.1f C:%.1f)",
                charger.entity_id,
                cs["score"]["A"], cs["score"]["B"], cs["score"]["C"],
            )
        elif result_1ph is not None:
            configured = charger.l1_phase
            if (charger.active_phases_mask
                    and len(charger.active_phases_mask) == 1):
                configured = charger.active_phases_mask  # plug
            if result_1ph == configured:
                cs["confirmed_1ph"] = True
                _LOGGER.debug(
                    "AutoDetect: L1 for %s confirmed on phase %s",
                    charger.entity_id, configured,
                )
            else:
                return _handle_mismatch(
                    cs, charger, hub_entry_id, cid,
                    "l1_phase", result_1ph, configured,
                    "L1", cs["score"][result_1ph], "notify_sent_1ph",
                )

    # --- Evaluate 2-phase detection ---
    if not cs["confirmed_2ph"] and cs.get("inactive_line"):
        result_2ph = _evaluate_score(cs["score_2ph"], _PM_NOTIFY_SCORE)
        if result_2ph is False:
            # Soft decay instead of hard reset
            for p in cs["score_2ph"]:
                cs["score_2ph"][p] *= _PM_DECAY_FACTOR
            cs["notify_sent_2ph"] = False
            _LOGGER.debug(
                "AutoDetect 2ph for %s: inconclusive, decaying scores "
                "(A:%.1f B:%.1f C:%.1f)",
                charger.entity_id,
                cs["score_2ph"]["A"], cs["score_2ph"]["B"],
                cs["score_2ph"]["C"],
            )
        elif result_2ph is not None:
            inactive_line = cs["inactive_line"]
            line_key = f"{inactive_line}_phase"  # e.g. "l3_phase"
            configured = getattr(charger, line_key)
            line_label = inactive_line.upper()  # e.g. "L3"
            if result_2ph == configured:
                cs["confirmed_2ph"] = True
                _LOGGER.debug(
                    "AutoDetect: %s for %s confirmed on phase %s",
                    line_label, charger.entity_id, configured,
                )
            else:
                return _handle_mismatch(
                    cs, charger, hub_entry_id, cid,
                    line_key, result_2ph, configured,
                    line_label, cs["score_2ph"][result_2ph],
                    "notify_sent_2ph",
                )

    # Log when full mapping is verified
    if cs["confirmed_1ph"] and cs["confirmed_2ph"]:
        inactive_line = cs.get("inactive_line", "")
        inactive_label = inactive_line.upper() if inactive_line else "?"
        inactive_phase = (
            getattr(charger, f"{inactive_line}_phase", "?")
            if inactive_line else "?"
        )
        _LOGGER.info(
            "AutoDetect: Full phase mapping verified for %s "
            "(L1\u2192%s, %s\u2192%s, remaining line by elimination)",
            charger.entity_id, charger.l1_phase,
            inactive_label, inactive_phase,
        )

    return None
