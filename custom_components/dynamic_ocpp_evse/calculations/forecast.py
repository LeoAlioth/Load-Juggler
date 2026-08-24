"""PV clipping forecast — how much production the site cannot place.

Sites with more PV than they may export (e.g. 15 kWp behind a 5 kW export
limit) curtail the midday peak if the battery fills up in the morning. The
morning energy was never at risk — it could have been exported — so the site
should keep enough battery headroom for the forecast peak instead.

This module answers, from a solar production forecast: *how many kWh will be
produced above what the site can export or consume, and therefore how full may
the battery be right now?*

The threshold is ``T = grid export limit + base consumption`` — power the site
can place without curtailment. (That is the ENERGY threshold, used by the
integral here. ``recommended_charge_limit``'s instantaneous advice is anchored
a margin lower; its docstring explains why a hard-limiting inverter makes that
necessary.) The forecast is a mapping of block-start
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


def battery_max_soc(
    absorbable_kwh, capacity_kwh, soc_floor, soc_ceiling=100.0, soc_target=100.0
):
    """Recommended battery SOC ceiling that keeps room for the forecast clip.

    The battery must be able to take ``absorbable_kwh``, so the ceiling is the
    SOC that leaves exactly that much headroom below the battery's
    DESTINATION — ``soc_target``, where this pack was going to end the day
    anyway — clamped to ``[soc_floor, soc_ceiling]``. With nothing to absorb
    the answer is the destination itself: fill as full as its owner asked.

    The anchor is the destination rather than a flat 100 % because the reserve
    only has to exist *while the clip happens*. A site whose ceiling normally
    sits at 95 % has 5 % of pack it never fills; carving the reserve out of
    100 % reserves that 5 % twice — the battery is allowed up to
    ``100 − reserve``, hits its owner's 95 % first, and meets the peak with 5 %
    of room instead of the reserve. Anchored at 95 % it holds at
    ``95 − reserve``, absorbs the clip through the peak and arrives at 95 %:
    the same place, by the intended route. Worked example — 20 kWh pack,
    destination 95 %, 2 kWh clippable → hold at 85 %.

    ``soc_target`` defaults to 100 %, where an unmanaged battery is heading, so
    a site that configures no ceiling source gets exactly the old formula
    ``100 − absorbable/capacity × 100``. It is deliberately independent of
    ``soc_ceiling``, which clamps the OUTPUT: the band between the destination
    and 100 % is the site's safety buffer against a forecast under-read, and
    this advice never reaches up into it.

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
    max_soc = soc_target - needed / capacity_kwh * 100.0
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


# --- The engaged advice's integral trim ---------------------------------------
#
# The production anchor is FEEDFORWARD: cloud-correct and kettle-immune, because
# it never looks at the meter. Its one weakness is that ``base_consumption``
# stands in for the house, so the export equilibrium inherits ``(base − house)``
# as a standing offset — a site whose base is 200 W low rides 200 W under
# ``limit − margin`` for ever, delaying every Excess load. The fix is a slow,
# bounded INTEGRAL trim steered by reconstructed export, which makes base an
# initial guess instead of a permanent error.
#
# Both numbers below are constants rather than settings on purpose: they are
# control-loop tuning, not site policy, and a user who could set them could
# make the register chatter.
#
# The time constant is what keeps the trim off the register. A kettle, a passing
# cloud or a car starting to charge moves reconstructed export by kilowatts for
# a minute or two; at this rate that is worth a couple of hundred watts at most
# — inside the charge control's write deadband — while a STANDING error of the
# same size is most of the way corrected within twenty minutes (63 % at one
# time constant, 86 % at two).
FORECAST_TRIM_TAU_S = 600.0  # s — first-order time constant (10 minutes)
# And the clamp is what makes windup impossible BY CONSTRUCTION: whatever the
# meter says and for however long, the trim can never move the advice by more
# than this, so no accumulated history can survive a cloud or hijack the
# feedforward's response to one. A few hundred watts is enough to absorb a
# realistically misconfigured base consumption and small next to any battery's
# charge rating.
FORECAST_TRIM_CLAMP_W = 400.0
# Longest cycle gap that may be integrated in one step, so a stalled or
# resumed coordinator cannot make a single step a large one.
FORECAST_TRIM_MAX_STEP_S = 60.0


def export_trim(trim_w, reconstructed_export_w, setpoint_w, elapsed_s):
    """One step of the engaged advice's integral trim, in watts.

    Steered by RECONSTRUCTED export — the draws-credited-back figure the Excess
    verdict decides on — against ``setpoint_w`` (``export limit − Excess trigger
    margin``, the export the anchored advice is aiming for). Reconstruction is
    what makes this safe to close a loop on: our own engaged loads' draw is
    credited back, so a car charging on the surplus is not read as an export
    shortfall and cannot steer the battery's limit.

    Sign convention follows the plant: export sitting BELOW the setpoint means
    the battery is taking too much, so the trim goes negative and the advice
    comes down. At the equilibrium the trim converges to ``base − house``,
    exactly cancelling the feedforward's standing offset.

    Two bounds, both structural rather than tuned: ``elapsed_s`` is capped so no
    single step is large, and the result is hard-clamped to
    ±``FORECAST_TRIM_CLAMP_W``. Anti-windup does not depend on either — the
    caller integrates only while the advice is actually free to move (see
    ``engine/hub_result._advance_export_trim``) — but the clamp means even a
    caller that got that wrong could not wind up.

    Pure function — unit-testable.
    """
    error = (reconstructed_export_w or 0.0) - setpoint_w
    window = max(0.0, min(elapsed_s or 0.0, FORECAST_TRIM_MAX_STEP_S))
    stepped = (trim_w or 0.0) + error * window / FORECAST_TRIM_TAU_S
    return max(-FORECAST_TRIM_CLAMP_W, min(FORECAST_TRIM_CLAMP_W, stepped))


def yields_to_excess(battery_soc, soc_target, hysteresis_pct, was_yielding=False):
    """Whether the battery is the absorber of LAST RESORT this cycle.

    Below its destination the battery is served first: it is on its way to where
    its owner sends it, and the clipping reserve exists precisely so it gets
    there. Above the destination it has already arrived — everything further is
    the overshoot buffer — so the surplus should go to the Excess loads that
    exist to soak it up, and the battery takes only what they cannot
    (``excess_draw_w`` in ``recommended_charge_limit``).

    A LATCH, for the same reason the charge gate is one: this decides a step
    change of kilowatts in the advice, and an integer SOC register sitting
    exactly on the destination would otherwise flip it back and forth. Engage at
    ``battery_soc >= soc_target``, release only below
    ``soc_target − hysteresis_pct``. The band is deliberately on the release
    side: yielding starts exactly at the destination and never a percent early,
    which is what keeps "below the destination the battery comes first" true.

    False whenever either number is unknown — no SOC to judge by, or no
    destination configured (a battery heading for 100 % is never above it).

    Pure function — unit-testable.
    """
    if battery_soc is None or soc_target is None:
        return False
    if was_yielding:
        return battery_soc >= soc_target - hysteresis_pct
    return battery_soc >= soc_target


def recommended_charge_limit(
    absorbable_kwh,
    battery_soc,
    max_soc,
    battery_max_charge_power,
    solar_now_w,
    threshold_w,
    hysteresis_pct,
    was_limiting=False,
    trim_w=0.0,
    excess_draw_w=0.0,
):
    """Battery charge-rate cap that protects the reserved headroom.

    ``max(0, solar_now − T)`` alone would be catastrophic as unconditional
    advice — on a cloudy day it reads 0 W all day and the house ends the
    evening with no reserve — so the cap engages only while the headroom is
    actually at risk:

    - nothing left to clip today (``absorbable_kwh <= 0``) → full rate, and the
      latch drops immediately: there is nothing left to protect
    - SOC below the SOC band (see below) → full rate
    - otherwise → charge only with power that could not have been exported

    ``threshold_w`` here is the instantaneous ADVICE ANCHOR, and the caller
    deliberately hands in a value one Excess trigger margin BELOW the true
    clipping threshold the forecast integral uses (``export limit + base
    consumption``). The two are different numbers on purpose:

    An inverter that hard-enforces the export limit curtails its own PV to
    hold it, so measured production can never exceed ``export_limit + house +
    battery_allowance``. Anchored at the true threshold, this function then
    returns ``battery_allowance + (house − base)`` — its own previous output.
    The allowance freezes near its floor while real kilowatts are curtailed,
    and the masking hides the very overshoot signal the advice is computed
    from. Anchored a margin lower, the same arithmetic SELF-RECOVERS with no
    probe state at all: while export is pinned at the limit each cycle returns
    ``allowance + margin + (house − base)``, so the allowance creeps up by
    about one margin per cycle until export falls off the limit; from there
    the battery absorbs ``production − house − (limit − margin)`` and export
    settles a margin under the limit, where production is measured honestly
    and this plain formula tracks the sun.

    The SOC test is a two-threshold LATCH, not one boundary, which is why the
    caller must hand the previous state back in as ``was_limiting``:

    - disarmed → engage at ``battery_soc >= max_soc − hysteresis_pct``
    - engaged  → release only below ``max_soc − 2 × hysteresis_pct``

    A single threshold in both directions was a genuine bug: the cap suppresses
    the very charging that pushed SOC over the boundary, so an integer SOC
    sitting on it flapped engage/release every couple of minutes and each flip
    became a Modbus/EEPROM register write. With the band a full
    ``hysteresis_pct`` wide, an integer ±1 tick at either threshold cannot flip
    the gate. ``max_soc`` moving (a forecast refresh) needs no special case —
    the same rule is applied against the new ceiling.

    Two engaged-only adjustments ride on top of the anchored overshoot, and
    neither exists while the gate is released (full rate is full rate):

    * ``trim_w`` — the slow, bounded integral trim that zeroes the standing
      ``(base − house)`` offset out of the equilibrium. See ``export_trim``; the
      caller owns its state and its anti-windup, this just adds it.
    * ``excess_draw_w`` — what the site's engaged Excess loads are already
      drawing, subtracted only when the caller says the battery is above its
      destination and therefore the absorber of last resort (see
      ``yields_to_excess``). An Excess EVSE then displaces battery charging
      watt for watt; with nothing engaged, or nothing able to absorb, the
      battery goes on taking the whole overshoot toward 100 % exactly as
      before. 0 below the destination, where the battery is served first.

    Returns ``(limit_w, limiting)``. The limit is always a legitimate setpoint
    ("restricted" is exactly ``limit_w < battery_max_charge_power``); the flag
    is the latch state to carry into the next cycle.

    Pure function — unit-testable.
    """
    full_rate = max(0.0, battery_max_charge_power or 0.0)

    def engaged_limit():
        """The overshoot, trimmed, less what the Excess loads already took."""
        advice = (
            unexportable_power(solar_now_w, threshold_w)
            + (trim_w or 0.0)
            - max(0.0, excess_draw_w or 0.0)
        )
        return min(full_rate, max(0.0, advice))

    if absorbable_kwh <= 0:
        return full_rate, False
    if battery_soc is None:
        # No SOC to judge headroom by: protect it (the pre-latch behaviour).
        return engaged_limit(), True
    if was_limiting:
        limiting = battery_soc >= max_soc - 2 * hysteresis_pct
    else:
        limiting = battery_soc >= max_soc - hysteresis_pct
    if not limiting:
        return full_rate, False
    return engaged_limit(), True
