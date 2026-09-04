"""The two forecast observers — accumulate, roll over daily, publish nothing else.

Observe-only by construction: these functions return figures for the engine to
publish and never touch the advice. What they measure, and why each is shaped
the way it is, lives in ``calculations/calibration.py``; this module owns only
the per-cycle bookkeeping that a pure function cannot.

STATE LIVES IN ``hub_runtime`` and is therefore lost on a restart, which is a
deliberate limitation of the observe phase rather than an oversight. The datum
that matters is each DAY's ratio, and that is published as the sensor's own
state, so the recorder keeps the history whatever the process does — the running
average is a convenience on top. Applying a learned gain later needs it to
survive restarts, and that is where ``helpers.storage.Store`` comes in (see
dev/TODO.md); an observer does not.
"""

import logging

from ..calculations.calibration import (
    GAIN_CLAMP_HIGH,
    GAIN_CLAMP_LOW,
    block_start,
    clip_pair,
    clipped_now,
    close_block,
    day_ratio,
    hourly_offsets,
    note_gain_sample,
    prune_series,
    series_days,
    series_gain,
)

_LOGGER = logging.getLogger(__name__)

# Runtime keys. One dict per observer, keyed inside by inverter entry id for the
# gain (an array's calibration is its own) and flat for peakiness (clipping is a
# site question — every array competes for the same export headroom).
_RT_GAIN = "_forecast_gain_observer"
_RT_PEAK = "_forecast_peak_observer"

# The window peakiness is measured over. Matched to the forecast's own
# resolution — Open-Meteo Solar Forecast publishes 15-minute blocks in its
# ``watts`` attribute — because the question is precisely "how much does
# averaging over ONE FORECAST BLOCK understate the clip?".
PEAK_WINDOW_MINUTES = 15


def _window_key(now):
    """The 15-minute window ``now`` falls in, as a comparable tuple."""
    return (now.date(), now.hour, (now.minute // PEAK_WINDOW_MINUTES))


def observe_gain(
    hub_runtime, entry_id, day, forecast_w, actual_w, dt_hours, constrained,
    now_local=None,
):
    """Fold one cycle into this inverter's gain observation.

    Returns ``forecast_accuracy_pct`` (today's running energy ratio, percent —
    the published state; it moves through the day and settles by evening),
    ``forecast_gain`` (the overall gain from the stored 15-minute series),
    ``forecast_gain_hourly`` (per hour-of-day offsets ON that overall gain),
    ``forecast_gain_days`` (distinct days in the series) and
    ``forecast_gain_blocks``.

    Two ledgers. Today's accumulators feed the accuracy figure and are not
    persisted anywhere. The SERIES of finished 15-minute blocks — forecast Wh,
    measured Wh, skipped (constrained) Wh — is what the gain is computed from,
    on every block rollover, over ``GAIN_SERIES_DAYS``; it is carried across
    restarts by the accuracy sensor's restore data (``gain_state`` /
    ``restore_gain_state``). ``day`` is the LOCAL date so a day means the
    daylight period; ``now_local`` keys the block.
    """
    store = hub_runtime.setdefault(_RT_GAIN, {})
    day_key = day.isoformat() if hasattr(day, "isoformat") else str(day)
    state = store.setdefault(entry_id, {})
    state.setdefault("day", day_key)
    state.setdefault("acc", {})
    state.setdefault("gain", 1.0)
    state.setdefault("days", 0)
    state.setdefault("last_ratio", None)
    state.setdefault("block", None)
    state.setdefault("block_acc", {})
    state.setdefault("series", [])
    state.setdefault("hourly", {})

    if state["day"] != day_key:
        ratio = day_ratio(state["acc"])
        if ratio is not None:
            state["last_ratio"] = ratio
            _LOGGER.info(
                "Forecast gain for %s: yesterday actual/forecast %.3f over"
                " %.1f kWh of unconstrained forecast (%.1f kWh excluded) —"
                " series gain %.3f over %d day(s)",
                entry_id,
                ratio,
                state["acc"].get("forecast_wh", 0.0) / 1000.0,
                state["acc"].get("skipped_wh", 0.0) / 1000.0,
                state["gain"],
                state["days"],
            )
        state["day"] = day_key
        state["acc"] = {}

    if now_local is not None:
        block = block_start(now_local)
        if state["block"] != block:
            if state["block"] is not None:
                state["series"] = prune_series(
                    close_block(state["series"], state["block"], state["block_acc"]),
                    now_local,
                )
                _recompute_gain(state)
            state["block"] = block
            state["block_acc"] = {}
        state["block_acc"] = note_gain_sample(
            state["block_acc"], forecast_w, actual_w, dt_hours, constrained
        )

    state["acc"] = note_gain_sample(
        state["acc"], forecast_w, actual_w, dt_hours, constrained
    )

    today = day_ratio(state["acc"])
    return {
        # Percent, so the sensor reads 100 when the forecast is exactly right
        # and the deviation is legible without doing arithmetic.
        "forecast_accuracy_pct": None if today is None else round(today * 100.0, 1),
        "forecast_gain": round(state["gain"], 4),
        "forecast_gain_hourly": dict(state["hourly"]),
        "forecast_gain_days": state["days"],
        "forecast_gain_blocks": len(state["series"]),
        "forecast_gain_clamp": [GAIN_CLAMP_LOW, GAIN_CLAMP_HIGH],
    }


def _recompute_gain(state):
    """The gain and its hourly offsets from the series as it now stands."""
    overall = series_gain(state["series"])
    state["gain"] = 1.0 if overall is None else overall
    state["hourly"] = hourly_offsets(state["series"], state["gain"]) if overall else {}
    state["days"] = series_days(state["series"])


def gain_state(hub_runtime, entry_id):
    """This inverter's observer state, JSON-ready, for the accuracy sensor to
    carry across a restart — or None before the observer has run."""
    state = (hub_runtime or {}).get(_RT_GAIN, {}).get(entry_id)
    if not state:
        return None
    return {
        "day": state.get("day"),
        "acc": dict(state.get("acc") or {}),
        "block": state.get("block"),
        "block_acc": dict(state.get("block_acc") or {}),
        "series": list(state.get("series") or []),
        "last_ratio": state.get("last_ratio"),
    }


def restore_gain_state(hub_runtime, entry_id, saved, today):
    """Seed the observer from a sensor's restored data — once, before the
    first cycle. The series always comes back (pruned against ``today``); the
    in-progress day and block come back only when they belong to ``today``
    (a restart after midnight must not resume yesterday's accuracy).
    Returns True when something was restored."""
    if not saved or not isinstance(saved, dict):
        return False
    store = hub_runtime.setdefault(_RT_GAIN, {})
    if entry_id in store:
        return False
    today_key = today.isoformat() if hasattr(today, "isoformat") else str(today)
    same_day = saved.get("day") == today_key
    state = {
        "day": today_key,
        "acc": dict(saved.get("acc") or {}) if same_day else {},
        "block": saved.get("block") if same_day else None,
        "block_acc": dict(saved.get("block_acc") or {}) if same_day else {},
        "series": [b for b in (saved.get("series") or []) if isinstance(b, dict)],
        "last_ratio": saved.get("last_ratio"),
        "gain": 1.0,
        "days": 0,
        "hourly": {},
    }
    _recompute_gain(state)
    store[entry_id] = state
    _LOGGER.info(
        "Forecast gain for %s restored: %d block(s) over %d day(s), gain %.3f",
        entry_id, len(state["series"]), state["days"], state["gain"],
    )
    return True


def observe_peakiness(hub_runtime, now, day, threshold_w, production_w, dt_hours):
    """Fold one production sample into the site's peakiness observation.

    Returns ``{"forecast_peakiness_pct", "forecast_peakiness_windows"}`` — how
    much more the site ACTUALLY clipped than a 15-minute average would have
    predicted, as a percentage (100 = the block average told the whole truth).

    Accumulated per window rather than by keeping the samples: within the open
    window only the three running sums are needed, and on close they give both
    the true clip and the block-average clip that ``clip_pair`` compares.
    """
    state = hub_runtime.setdefault(
        _RT_PEAK,
        {"day": day, "win": None, "sums": None, "true_wh": 0.0, "block_wh": 0.0,
         "windows": 0, "last_pct": None},
    )

    if state["day"] != day:
        if state["block_wh"] > 0:
            state["last_pct"] = 100.0 * state["true_wh"] / state["block_wh"]
            _LOGGER.info(
                "Forecast peakiness: yesterday the site clipped %.2f kWh where"
                " 15-minute averages predicted %.2f kWh (%.0f%%) over %d"
                " window(s)",
                state["true_wh"] / 1000.0,
                state["block_wh"] / 1000.0,
                state["last_pct"],
                state["windows"],
            )
        state.update(
            day=day, true_wh=0.0, block_wh=0.0, windows=0, win=None, sums=None
        )

    key = _window_key(now)
    if state["win"] != key:
        sums = state["sums"]
        if sums and sums["hours"] > 0:
            # One synthetic sample carrying the window's mean is all clip_pair
            # needs for the block figure; the true figure is already integrated.
            true_wh, block_wh = clip_pair(
                [(sums["hours"], sums["wh"] / sums["hours"])], threshold_w
            )
            state["true_wh"] += sums["true_wh"]
            state["block_wh"] += block_wh
            state["windows"] += 1
        state["win"] = key
        state["sums"] = {"hours": 0.0, "wh": 0.0, "true_wh": 0.0}

    if production_w is not None and dt_hours > 0 and threshold_w is not None:
        sums = state["sums"]
        sums["hours"] += dt_hours
        sums["wh"] += max(0.0, float(production_w)) * dt_hours
        sums["true_wh"] += max(0.0, float(production_w) - threshold_w) * dt_hours

    pct = None
    if state["block_wh"] > 0:
        pct = round(100.0 * state["true_wh"] / state["block_wh"], 1)
    return {
        "forecast_peakiness_pct": pct,
        "forecast_peakiness_windows": state["windows"],
    }


_RT_CLIPPED = "_forecast_clipped_observer"


def observe_clipped(hub_runtime, day, forecast_w, actual_w, saturated, dt_hours):
    """Accumulate today's curtailed energy. Returns the published keys.

    The ground truth the whole clipping feature otherwise lacks: what the site
    ACTUALLY threw away, next to what the forecast said it would
    (``forecast_clipped_kwh``). Those two side by side are what tells you
    whether the reserve is doing its job.

    Resets at local midnight, so the sensor is a daily total. The previous
    day's figure is kept as an attribute rather than logged only, because the
    comparison against that morning's prediction is the interesting part.
    """
    state = hub_runtime.setdefault(
        _RT_CLIPPED, {"day": day, "wh": 0.0, "saturated_h": 0.0, "yesterday": None}
    )
    if state["day"] != day:
        if state["wh"] > 0:
            _LOGGER.info(
                "Clipped energy: yesterday the site threw away an estimated"
                " %.2f kWh over %.1f h of saturation",
                state["wh"] / 1000.0,
                state["saturated_h"],
            )
        state.update(
            day=day,
            yesterday=round(state["wh"] / 1000.0, 2),
            wh=0.0,
            saturated_h=0.0,
        )

    if dt_hours > 0:
        state["wh"] += clipped_now(forecast_w, actual_w, saturated) * dt_hours
        if saturated:
            state["saturated_h"] += dt_hours

    return {
        "forecast_clipped_actual_kwh": round(state["wh"] / 1000.0, 2),
        "forecast_clipped_actual_hours": round(state["saturated_h"], 2),
        "forecast_clipped_actual_yesterday_kwh": state["yesterday"],
    }
