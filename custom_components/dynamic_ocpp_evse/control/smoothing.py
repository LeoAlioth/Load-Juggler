import logging
from ..const import (
    DEAD_BAND,
    ema_alpha_for,
    RAMP_APPROACH_MAX,
    RAMP_APPROACH_RATE,
    RAMP_UP_RATE,
    RAMP_DOWN_RATE,
    CONF_SITE_UPDATE_FREQUENCY,
    DEFAULT_SITE_UPDATE_FREQUENCY,
)
from ..helpers import get_entry_value

_LOGGER = logging.getLogger(__name__)


def apply_smoothing(
    sensor, raw_allocated: float, mode_changed: bool, hub_entry
) -> float:
    """Apply EMA smoothing → Schmitt trigger → rate limiting pipeline.

    Every stage runs on the same TIME basis: the site interval sets both the
    EMA weight and the ramp step, so the pipeline delivers the same seconds of
    smoothing whether the site polls every second or every minute. Weighting
    the EMA per CYCLE instead coupled the two, and the coupling was invisible:
    at the 60 s interval a slow inverter needs, the permit filter carried a
    200 s time constant, so a load crawled for minutes toward a surplus it had
    already been granted.

    Returns the final rate-limited current to send to the load.
    """
    site_freq = get_entry_value(
        hub_entry, CONF_SITE_UPDATE_FREQUENCY, DEFAULT_SITE_UPDATE_FREQUENCY
    )

    if sensor._schmitt_current is None and sensor._ema_current is not None:
        sensor._schmitt_current = sensor._rate_limited_current
        sensor._schmitt_state = "rising"

    if mode_changed or sensor._ema_current is None:
        sensor._ema_current = raw_allocated
        sensor._schmitt_current = raw_allocated
        sensor._schmitt_state = "rising"
        sensor._rate_limited_current = raw_allocated
        if mode_changed:
            _LOGGER.debug(
                "Mode changed for %s - smoothing reset (allocated=%.1fA)",
                sensor._attr_name,
                raw_allocated,
            )
    elif sensor._rate_limited_current == 0:
        # Intentional fast-start (design decision, 2026-08-17): resuming from 0
        # seeds the whole pipeline at the raw permit instead of ramping up from
        # the minimum. The permit was computed inside every site constraint, so
        # the step is safe by construction, and crawling up would waste surplus.
        # The ramp exists to damp oscillation, not to protect anything; the
        # compliance checker's "ramping" skip tolerates the step. Every
        # modulating load type goes through this identically - the power
        # station used to resume at its minimum instead, which only delayed
        # its absorption of a surplus it had already been granted.
        sensor._ema_current = raw_allocated
        sensor._schmitt_current = raw_allocated
        sensor._schmitt_state = "rising"
        sensor._rate_limited_current = raw_allocated
    else:
        # EMA_TAU_S of smoothing at whatever cadence this site runs at. The
        # conversion is the readers' own, shared rather than restated: the
        # input filter and this one are tuned as a pair, and a second copy of
        # the formula would let them drift apart silently.
        alpha = ema_alpha_for(site_freq)
        sensor._ema_current = round(
            alpha * raw_allocated + (1 - alpha) * sensor._ema_current, 2
        )

        ema = sensor._ema_current
        prev = sensor._schmitt_current
        if sensor._schmitt_state == "rising":
            if ema >= prev:
                sensor._schmitt_current = ema
            elif prev - ema >= DEAD_BAND:
                sensor._schmitt_state = "falling"
                sensor._schmitt_current = ema
                _LOGGER.debug(
                    "Schmitt RISING→FALLING (large) for %s at %.2fA (prev=%.2fA)",
                    sensor._attr_name,
                    ema,
                    prev,
                )
            else:
                sensor._schmitt_state = "falling"
                _LOGGER.debug(
                    "Schmitt RISING→FALLING (small) for %s at %.2fA (prev=%.2fA)",
                    sensor._attr_name,
                    ema,
                    prev,
                )
        else:
            if ema < prev - DEAD_BAND:
                sensor._schmitt_current = ema
            elif ema > prev + DEAD_BAND:
                sensor._schmitt_state = "rising"
                sensor._schmitt_current = ema
                _LOGGER.debug(
                    "Schmitt FALLING→RISING for %s at %.2fA (prev=%.2fA)",
                    sensor._attr_name,
                    ema,
                    prev,
                )

        target = sensor._schmitt_current
        delta = target - sensor._rate_limited_current

        # The allowed step is the LARGER of a fixed floor and a fraction of the
        # error still to close, so this is never slower than the old constant
        # slew and is much faster while far from target. Shrinking with the
        # error is what makes it self-damping: it approaches asymptotically
        # rather than driving through at a constant rate.
        approach = min(RAMP_APPROACH_MAX, RAMP_APPROACH_RATE * site_freq)
        proportional = abs(delta) * approach
        max_up = max(RAMP_UP_RATE * site_freq, proportional)
        max_down = max(RAMP_DOWN_RATE * site_freq, proportional)

        if delta > max_up:
            target = sensor._rate_limited_current + max_up
            _LOGGER.debug(
                "Ramp UP for %s: %.1fA → %.1fA (schmitt=%.1fA, max +%.2fA/cycle)",
                sensor._attr_name,
                sensor._rate_limited_current,
                target,
                sensor._schmitt_current,
                max_up,
            )
        elif delta < -max_down:
            target = sensor._rate_limited_current - max_down
            _LOGGER.debug(
                "Ramp DOWN for %s: %.1fA → %.1fA (schmitt=%.1fA, max -%.2fA/cycle)",
                sensor._attr_name,
                sensor._rate_limited_current,
                target,
                sensor._schmitt_current,
                max_down,
            )
        sensor._rate_limited_current = round(target, 1)

    return sensor._rate_limited_current
