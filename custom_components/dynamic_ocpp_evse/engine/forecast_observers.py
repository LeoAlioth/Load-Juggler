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
    clip_pair,
    day_ratio,
    note_gain_sample,
    update_gain,
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
    hub_runtime, entry_id, day, forecast_w, actual_w, dt_hours, constrained
):
    """Fold one cycle into this inverter's gain observation.

    Returns ``{"forecast_accuracy_pct", "forecast_gain", "forecast_gain_days"}``
    — today's running ratio as a percentage (the published state, so it moves
    through the day and settles by evening), plus the running average and how
    many days have contributed to it.

    ``day`` is the LOCAL date; the rollover happens when it changes, which is
    what makes "a day" mean the daylight period rather than a UTC boundary
    somewhere mid-afternoon.
    """
    store = hub_runtime.setdefault(_RT_GAIN, {})
    state = store.setdefault(
        entry_id,
        {"day": day, "acc": {}, "gain": 1.0, "days": 0, "last_ratio": None},
    )

    if state["day"] != day:
        ratio = day_ratio(state["acc"])
        if ratio is not None:
            state["gain"] = update_gain(state["gain"], ratio)
            state["days"] += 1
            state["last_ratio"] = ratio
            _LOGGER.info(
                "Forecast gain for %s: yesterday actual/forecast %.3f over"
                " %.1f kWh of unconstrained forecast (%.1f kWh excluded) —"
                " running gain %.3f over %d day(s)",
                entry_id,
                ratio,
                state["acc"].get("forecast_wh", 0.0) / 1000.0,
                state["acc"].get("skipped_wh", 0.0) / 1000.0,
                state["gain"],
                state["days"],
            )
        state["day"] = day
        state["acc"] = {}

    state["acc"] = note_gain_sample(
        state["acc"], forecast_w, actual_w, dt_hours, constrained
    )

    today = day_ratio(state["acc"])
    return {
        # Percent, so the sensor reads 100 when the forecast is exactly right
        # and the deviation is legible without doing arithmetic.
        "forecast_accuracy_pct": None if today is None else round(today * 100.0, 1),
        "forecast_gain": round(state["gain"], 4),
        "forecast_gain_days": state["days"],
        "forecast_gain_clamp": [GAIN_CLAMP_LOW, GAIN_CLAMP_HIGH],
    }


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
