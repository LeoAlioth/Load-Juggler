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
_PM_MIN_SAMPLES = 20        # Correlated samples before evaluating
_PM_CONFIDENCE = 0.70       # Required ratio (best / total)


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

    Uses total charger draw correlated against per-phase grid changes.
    Works for all device types:
    - 3-phase EVSE with 1/2-phase OBC car (asymmetric draw → detectable)
    - 3-phase EVSE with 3-phase OBC car (symmetric draw → inconclusive, correct)
    - Single-phase EVSE (total draw → single phase correlation)
    - Smart plugs (total draw → single phase correlation)

    Returns a list of notification dicts (one per mismatched charger).
    """
    # Need 3-phase site (otherwise nothing to mis-map)
    if sum(1 for p in smoothed_phases if p is not None) < 3:
        return []

    pm_state = state.setdefault("phase_map", {})
    notifications = []

    for charger in chargers:
        # Must be actively drawing power
        if charger.connector_status not in ("Charging", "SuspendedEVSE", "SuspendedEV"):
            continue
        if charger.l1_current == 0 and charger.l2_current == 0 and charger.l3_current == 0:
            continue

        notif = _check_draw_phase_correlation(
            pm_state, smoothed_phases, charger, hub_entry_id,
        )
        if notif:
            notifications.append(notif)

    return notifications


# --- Total draw → single phase correlation ---

def _check_draw_phase_correlation(pm_state: dict, smoothed_phases: list,
                                  charger, hub_entry_id: str) -> dict | None:
    """Detect which phase a charger's draw actually appears on.

    Uses total charger draw (l1+l2+l3) correlated against per-phase grid
    deltas. For 3-phase chargers with symmetric draws (all phases equal),
    the draw correlates equally with all grid phases → inconclusive → no
    notification (correct behavior — symmetric draws don't need mapping).
    """
    cid = charger.charger_id
    cs = pm_state.setdefault(cid, {
        "prev_draw": 0.0,
        "prev_grid_a": 0.0, "prev_grid_b": 0.0, "prev_grid_c": 0.0,
        "corr": {"A": 0, "B": 0, "C": 0},
        "sample_count": 0,
        "notified": False,
    })

    if cs["notified"]:
        return None

    total_draw = charger.l1_current + charger.l2_current + charger.l3_current
    grid_a = smoothed_phases[0] if smoothed_phases[0] is not None else 0.0
    grid_b = smoothed_phases[1] if smoothed_phases[1] is not None else 0.0
    grid_c = smoothed_phases[2] if smoothed_phases[2] is not None else 0.0

    delta_draw = total_draw - cs["prev_draw"]
    delta_g = {
        "A": grid_a - cs["prev_grid_a"],
        "B": grid_b - cs["prev_grid_b"],
        "C": grid_c - cs["prev_grid_c"],
    }

    if abs(delta_draw) >= _PM_MIN_DELTA_A:
        for phase, d_phase in delta_g.items():
            if abs(d_phase) >= _PM_MIN_DELTA_A and (delta_draw > 0) == (d_phase > 0):
                cs["corr"][phase] += 1
        cs["sample_count"] += 1

    cs["prev_draw"] = total_draw
    cs["prev_grid_a"] = grid_a
    cs["prev_grid_b"] = grid_b
    cs["prev_grid_c"] = grid_c

    if cs["sample_count"] < _PM_MIN_SAMPLES:
        return None

    # Find best matching phase
    best_phase = max(cs["corr"], key=lambda p: cs["corr"][p])
    best_count = cs["corr"][best_phase]
    total_count = sum(cs["corr"].values())

    if total_count == 0 or best_count / total_count < _PM_CONFIDENCE:
        _LOGGER.debug(
            "AutoDetect phase for %s: inconclusive after %d samples, resetting",
            charger.entity_id, cs["sample_count"],
        )
        cs["corr"] = {"A": 0, "B": 0, "C": 0}
        cs["sample_count"] = 0
        return None

    # Determine configured phase
    configured_phase = charger.l1_phase  # EVSE: which phase L1 is mapped to
    if charger.active_phases_mask and len(charger.active_phases_mask) == 1:
        configured_phase = charger.active_phases_mask  # plug: connected_to_phase

    if best_phase == configured_phase:
        _LOGGER.debug(
            "AutoDetect phase for %s: confirmed on phase %s",
            charger.entity_id, configured_phase,
        )
        cs["notified"] = True
        return None

    cs["notified"] = True
    _LOGGER.warning(
        "AutoDetect: Phase mismatch for %s. Configured: %s, Detected: %s",
        charger.entity_id, configured_phase, best_phase,
    )
    return {
        "title": (
            f"Dynamic OCPP EVSE \u2014 Possible Phase Mismatch: "
            f"{charger.entity_id}"
        ),
        "message": (
            f"The phase assignment for '{charger.entity_id}' may be incorrect.\n\n"
            f"Based on observed current patterns:\n"
            f"  Configured phase: {configured_phase}\n"
            f"  Detected phase:   {best_phase}\n\n"
            "To correct this, update the phase assignment in:\n"
            "Settings \u2192 Devices & Services \u2192 Dynamic OCPP EVSE \u2192 "
            f"'{charger.entity_id}' \u2192 Configure.\n\n"
            "You can disable phase mapping detection in the hub settings."
        ),
        "notification_id": (
            f"dynamic_ocpp_evse_phase_map_{hub_entry_id}_{charger.charger_id}"
        ),
    }
