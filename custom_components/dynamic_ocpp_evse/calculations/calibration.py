"""Forecast calibration — the pure arithmetic behind the two observers.

Two DIFFERENT forecast errors, measured separately because neither correction
fixes the other:

* **Level bias** — the forecast's daily energy is systematically high or low for
  an array: a wrong declared kWp, soiling, a horizon the model does not know,
  panel degradation. Stationary, so it is learnable as one slow gain per
  inverter. Measured as an energy-weighted ``actual ÷ forecast`` ratio.
* **Peakiness** — clipping is a convex, one-sided function of power, so by
  Jensen's inequality the clip of a block AVERAGE is never more than the average
  of the clip. A 15-minute series therefore understates clipping whenever power
  varies inside a block, and the forecast's mean can be exactly right while the
  clip is still too small. Measured by replaying real production samples
  against the block-average figure the forecast integral would have produced.

Both are OBSERVERS first: they publish what they would have corrected and change
nothing, so a season of evidence decides whether either is worth applying.

Pure functions — unit-testable.
"""

import logging

_LOGGER = logging.getLogger(__name__)

# Bounds on the learned gain. A stationary array-calibration error outside ±25%
# is a misconfigured kWp or a dead sensor, not something to absorb quietly.
GAIN_CLAMP_LOW = 0.75
GAIN_CLAMP_HIGH = 1.25
# Weight of one day in the running gain. Slow on purpose: the quantity is
# stationary, so there is nothing to chase, and a single freak day must not move
# the reserve.
GAIN_DAY_WEIGHT = 0.1
# A day contributes only if its comparable forecast energy reaches this. Below
# it the ratio is dominated by dawn/dusk noise and by whatever fraction of the
# day survived the constrained-interval exclusion.
GAIN_MIN_DAY_WH = 2000.0
# Blocks below this forecast power are skipped entirely: near sunrise and sunset
# the ratio's denominator approaches zero, and a 20 W block carries no
# information about an array's calibration.
GAIN_MIN_BLOCK_W = 50.0


def block_power_at(series, when):
    """The forecast power covering ``when``, or None.

    ``series`` maps block-start timestamps to average watts, at whatever
    resolution the forecast publishes (Open-Meteo Solar Forecast: 15 minutes).
    The block containing ``when`` is the latest one starting at or before it,
    and only while ``when`` actually falls inside that block's width — past the
    end of the series there is no forecast, which is different from a forecast
    of zero.

    Pure function — unit-testable.
    """
    if not series:
        return None
    blocks = sorted(series.items())
    prev_width = None
    for i, (start, watts) in enumerate(blocks):
        if i + 1 < len(blocks):
            width = (blocks[i + 1][0] - start).total_seconds() / 3600.0
        else:
            width = prev_width
        if width and width > 0:
            prev_width = width
        if start > when:
            break
        if width and width > 0:
            end_gap = (when - start).total_seconds() / 3600.0
            if end_gap < width:
                return max(0.0, float(watts))
    return None


def note_gain_sample(state, forecast_w, actual_w, dt_hours, constrained):
    """Fold one cycle into a day's gain accumulators. Returns the new state.

    ``state`` is ``{"forecast_wh", "actual_wh", "skipped_wh"}``; missing keys
    start at zero, so an empty dict is a valid fresh day.

    CONSTRAINED INTERVALS ARE EXCLUDED, not whole days. While the site is
    curtailing, measured production is suppressed by the very thing being
    forecast, so counting those intervals would teach the learner that the
    forecast reads high exactly when accuracy matters most. Dropping the whole
    day instead was the obvious alternative and is worse: on an export-limited
    site most of the *sunny* days curtail, which would leave the gain learning
    only from overcast days — where forecast error is largest and least
    stationary. Excluding by interval keeps a clipping day's morning and
    evening, which are honest measurements.

    What is excluded is still counted, in ``skipped_wh``, so the published
    observation can say how much of the day it had to throw away.

    Pure function — unit-testable.
    """
    forecast_wh = float(state.get("forecast_wh", 0.0))
    actual_wh = float(state.get("actual_wh", 0.0))
    skipped_wh = float(state.get("skipped_wh", 0.0))

    if forecast_w is None or actual_w is None or dt_hours <= 0:
        return {
            "forecast_wh": forecast_wh,
            "actual_wh": actual_wh,
            "skipped_wh": skipped_wh,
        }
    contribution = max(0.0, float(forecast_w)) * dt_hours
    if constrained or forecast_w < GAIN_MIN_BLOCK_W:
        skipped_wh += contribution
    else:
        forecast_wh += contribution
        actual_wh += max(0.0, float(actual_w)) * dt_hours
    return {
        "forecast_wh": forecast_wh,
        "actual_wh": actual_wh,
        "skipped_wh": skipped_wh,
    }


def day_ratio(state, min_wh=GAIN_MIN_DAY_WH):
    """A day's energy-weighted ``actual ÷ forecast``, or None if uninformative.

    ENERGY-weighted — one ratio of two sums, never a mean of per-block ratios.
    A block ratio's denominator approaches zero at both ends of the day, so
    averaging them lets the least informative minutes dominate the answer.

    None when the day's comparable forecast energy is below ``min_wh``: a
    washout, or a day whose unconstrained intervals were too few to say
    anything.

    Pure function — unit-testable.
    """
    forecast_wh = float((state or {}).get("forecast_wh", 0.0))
    actual_wh = float((state or {}).get("actual_wh", 0.0))
    if forecast_wh < min_wh or forecast_wh <= 0:
        return None
    return actual_wh / forecast_wh


def update_gain(
    gain,
    ratio,
    weight=GAIN_DAY_WEIGHT,
    low=GAIN_CLAMP_LOW,
    high=GAIN_CLAMP_HIGH,
):
    """Fold one day's ratio into the running gain, clamped.

    A plain exponential average, and deliberately a slow one. The clamp is on
    the RESULT rather than on the incoming ratio, so a run of extreme days
    pushes the gain to the bound and holds it there instead of being averaged
    into something that looks moderate — the bound is then visible in the
    published value, which is the point of an observer.

    ``ratio`` of None leaves the gain untouched (an uninformative day).

    Pure function — unit-testable.
    """
    if ratio is None:
        return gain
    moved = float(gain) + weight * (float(ratio) - float(gain))
    return min(high, max(low, moved))


def clip_pair(samples, threshold_w):
    """``(true_wh, block_wh)`` for one window of measured production.

    The peakiness measurement, and it needs no cloud model at all: replay the
    real samples through the clip integral, then through the same integral fed
    only the window's average — which is exactly what the forecast series gives
    the engine. Their difference IS the Jensen gap for this window, measured on
    this array.

    ``samples`` is ``[(dt_hours, watts), …]`` covering the window. Returns both
    figures in watt-hours so a caller can accumulate them across a day and
    publish one honest ratio.

    Pure function — unit-testable.
    """
    total_hours = sum(dt for dt, _ in samples if dt > 0)
    if total_hours <= 0:
        return 0.0, 0.0
    true_wh = sum(
        max(0.0, float(watts) - threshold_w) * dt for dt, watts in samples if dt > 0
    )
    mean_w = sum(float(watts) * dt for dt, watts in samples if dt > 0) / total_hours
    block_wh = max(0.0, mean_w - threshold_w) * total_hours
    return true_wh, block_wh
