"""PV clipping forecast — how much production the site cannot place.

Sites with more PV than they may export (e.g. 15 kWp behind a 5 kW export
limit) curtail the midday peak if the battery fills up in the morning. The
morning energy was never at risk — it could have been exported — so the site
should keep enough battery headroom for the forecast peak instead.

This module answers, from a solar production forecast: *how many kWh will be
produced above what the site can export or consume, and therefore how full may
the battery be right now?*

The threshold is ``T = grid export limit + base consumption`` — power the site
can place without curtailment. The forecast is a mapping of block-start
timestamps to average watts for that block (the ``watts`` attribute of the
Open-Meteo Solar Forecast sensors). Each block is treated as constant power
for its duration, so the maths is a plain sum over blocks:

    clipped_kwh    = Σ max(0, p − T) × h
    absorbable_kwh = Σ min(charge_cap, max(0, p − T)) × h

Block width ``h`` comes from consecutive timestamps — never assumed to be one
hour (Open-Meteo can serve 15-minute data, and DST makes one block two hours
wide). The block containing ``now`` is prorated, and blocks at or beyond
``until`` (normally the start of the next local day) are excluded so
tomorrow's peak is not reserved for twice.

``absorbable_kwh`` — the clipped energy the battery could physically take at
its charge rate — drives the SOC recommendation. The difference to
``clipped_kwh`` is unavoidable curtailment: energy no SOC ceiling can save,
because the *charge rate* is the binding constraint.

Deliberately not modelled: charge efficiency (≤6 %, and it errs conservative),
sub-hourly cloud transients (block resolution makes the estimate a lower
bound), and multi-hump days, where reserving for the whole remaining tail can
over-reserve between the humps.

Pure functions — unit-testable. Timezone handling lives in the caller; these
take timezone-aware datetimes and never consult a clock.
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

_LOGGER = logging.getLogger(__name__)

# Assumed width of the last forecast block, which has no successor to
# subtract, when the series has no earlier block to copy the width from.
_DEFAULT_BLOCK_HOURS = 1.0


@dataclass(frozen=True)
class ClippingForecast:
    """Result of a clipping integration over the forecast window."""

    clipped_kwh: float
    absorbable_kwh: float
    window_hours: float
    peak_w: float
    peak_at: Optional[datetime]


def merge_forecast_series(series_list):
    """Sum multiple per-array forecast series into one site series.

    Clipping is a site-level nonlinearity — two 4 kW arrays against a 6 kW
    threshold clip 2 kW, not 0 — so arrays must be summed *before* clipping.

    Each series maps block-start timestamps to average watts. Blocks are
    summed by timestamp; a series missing a timestamp contributes 0 there
    (a shorter array simply produces nothing outside its own range).

    Pure function — unit-testable.
    """
    merged = {}
    for series in series_list:
        if not series:
            continue
        for ts, watts in series.items():
            merged[ts] = merged.get(ts, 0.0) + float(watts)
    return merged


def clipping_forecast(
    series,
    threshold_w,
    now,
    until,
    charge_cap_w=None,
    power_cap_w=None,
):
    """Sum the forecast energy above ``threshold_w`` between ``now`` and ``until``.

    ``series`` maps block-start timestamps to average watts (see module
    docstring for the block model). ``charge_cap_w`` folds the battery's
    charge rate into the sum, producing ``absorbable_kwh``; ``power_cap_w``
    caps the summed series at what the inverters can physically deliver — an
    AC-coupled string inverter cannot produce what Open-Meteo models from kWp,
    and without the cap an oversized array over-reserves badly.

    Pure function — unit-testable.
    """
    empty = ClippingForecast(0.0, 0.0, 0.0, 0.0, None)
    if not series or until <= now:
        return empty

    blocks = sorted(series.items())
    clipped_wh = 0.0
    absorbable_wh = 0.0
    window_hours = 0.0
    peak_w = 0.0
    peak_at = None
    prev_width = None

    for i, (start, watts) in enumerate(blocks):
        if i + 1 < len(blocks):
            width = (blocks[i + 1][0] - start).total_seconds() / 3600.0
        else:
            width = prev_width if prev_width else _DEFAULT_BLOCK_HOURS
        if width <= 0:
            continue  # duplicate or unsorted timestamp — skip defensively
        prev_width = width

        # Overlap of [start, start + width) with [now, until), in hours.
        start_h = 0.0
        block_end_offset = width
        if start < now:
            start_h = (now - start).total_seconds() / 3600.0
        if start_h >= block_end_offset:
            continue
        until_offset = (until - start).total_seconds() / 3600.0
        overlap = min(block_end_offset, until_offset) - start_h
        if overlap <= 0:
            continue

        power = max(0.0, float(watts))
        if power_cap_w is not None:
            power = min(power, power_cap_w)

        if power > peak_w:
            peak_w = power
            peak_at = start

        window_hours += overlap
        excess = power - threshold_w
        if excess > 0:
            clipped_wh += excess * overlap
            if charge_cap_w is None:
                absorbable_wh += excess * overlap
            else:
                absorbable_wh += min(charge_cap_w, excess) * overlap

    result = ClippingForecast(
        clipped_kwh=clipped_wh / 1000.0,
        absorbable_kwh=absorbable_wh / 1000.0,
        window_hours=window_hours,
        peak_w=peak_w,
        peak_at=peak_at,
    )
    _LOGGER.debug(
        "Clipping forecast: %.2f kWh above %.0fW (%.2f kWh storable at cap %s)"
        " over %.1fh to %s, peak %.0fW at %s",
        result.clipped_kwh,
        threshold_w,
        result.absorbable_kwh,
        f"{charge_cap_w:.0f}W" if charge_cap_w is not None else "none",
        result.window_hours,
        until,
        result.peak_w,
        result.peak_at,
    )
    return result


def battery_max_soc(absorbable_kwh, capacity_kwh, soc_floor, soc_ceiling=100.0):
    """Recommended battery SOC ceiling that keeps room for the forecast clip.

    The battery must be able to take ``absorbable_kwh``, so the ceiling is the
    SOC that leaves exactly that much headroom, clamped to
    ``[soc_floor, soc_ceiling]``. With nothing to absorb the answer is the
    ceiling — the battery may fill completely.

    Pure function — unit-testable.
    """
    if capacity_kwh <= 0:
        _LOGGER.warning(
            "battery_max_soc called with capacity %.1f kWh — failing open to %.0f%%",
            capacity_kwh,
            soc_ceiling,
        )
        return soc_ceiling
    needed = min(max(0.0, absorbable_kwh), capacity_kwh)
    max_soc = 100.0 - needed / capacity_kwh * 100.0
    return min(soc_ceiling, max(soc_floor, max_soc))


def headroom_deficit_kwh(absorbable_kwh, capacity_kwh, battery_soc):
    """kWh of forecast clip the battery can no longer make room for.

    Zero while the advice is achievable. Positive when the battery already
    holds more than the recommendation allows — the machine-readable "this
    advice cannot be met from here", since the integration never forces a
    discharge.

    Pure function — unit-testable.
    """
    if capacity_kwh <= 0 or battery_soc is None:
        return 0.0
    needed = min(max(0.0, absorbable_kwh), capacity_kwh)
    available = capacity_kwh * max(0.0, 100.0 - battery_soc) / 100.0
    return max(0.0, needed - available)


def unexportable_power(solar_now_w, threshold_w):
    """Watts of current production the site cannot export or consume.

    Pure function — unit-testable.
    """
    return max(0.0, (solar_now_w or 0.0) - threshold_w)


def recommended_charge_limit(
    absorbable_kwh,
    battery_soc,
    max_soc,
    battery_max_charge_power,
    solar_now_w,
    threshold_w,
    hysteresis_pct,
):
    """Battery charge-rate cap that protects the reserved headroom.

    ``max(0, solar_now − T)`` alone would be catastrophic as unconditional
    advice — on a cloudy day it reads 0 W all day and the house ends the
    evening with no reserve — so the cap is released whenever there is nothing
    to protect:

    - nothing left to clip today (``absorbable_kwh == 0``) → full rate
    - SOC comfortably below the ceiling → full rate
    - otherwise → charge only with power that could not have been exported

    The returned value is therefore always a legitimate setpoint; "restricted"
    is exactly ``value < battery_max_charge_power``.

    Pure function — unit-testable.
    """
    full_rate = max(0.0, battery_max_charge_power or 0.0)
    if absorbable_kwh <= 0:
        return full_rate
    if battery_soc is not None and battery_soc < max_soc - hysteresis_pct:
        return full_rate
    return min(full_rate, unexportable_power(solar_now_w, threshold_w))
