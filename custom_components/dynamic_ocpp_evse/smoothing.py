import logging
from .const import (
    EMA_ALPHA,
    DEAD_BAND,
    RAMP_UP_RATE,
    RAMP_DOWN_RATE,
    CONF_SITE_UPDATE_FREQUENCY,
    DEFAULT_SITE_UPDATE_FREQUENCY,
)
from .helpers import get_entry_value

_LOGGER = logging.getLogger(__name__)


def apply_smoothing(
    sensor, raw_allocated: float, mode_changed: bool, hub_entry
) -> float:
    """Apply EMA smoothing → Schmitt trigger → rate limiting pipeline.

    Returns the final rate-limited current to send to the charger.
    """
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
                "Mode changed for %s — smoothing reset (allocated=%.1fA)",
                sensor._attr_name,
                raw_allocated,
            )
    elif sensor._rate_limited_current == 0:
        sensor._ema_current = raw_allocated
        sensor._schmitt_current = raw_allocated
        sensor._schmitt_state = "rising"
        sensor._rate_limited_current = raw_allocated
    else:
        sensor._ema_current = round(
            EMA_ALPHA * raw_allocated + (1 - EMA_ALPHA) * sensor._ema_current, 2
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

        site_freq = get_entry_value(
            hub_entry, CONF_SITE_UPDATE_FREQUENCY, DEFAULT_SITE_UPDATE_FREQUENCY
        )
        max_up = RAMP_UP_RATE * site_freq
        max_down = RAMP_DOWN_RATE * site_freq
        target = sensor._schmitt_current
        delta = target - sensor._rate_limited_current
        if delta > max_up:
            target = sensor._rate_limited_current + max_up
            _LOGGER.debug(
                "Ramp UP for %s: %.1fA → %.1fA (schmitt=%.1fA, max +%.1fA/cycle)",
                sensor._attr_name,
                sensor._rate_limited_current,
                target,
                sensor._schmitt_current,
                max_up,
            )
        elif delta < -max_down:
            target = sensor._rate_limited_current - max_down
            _LOGGER.debug(
                "Ramp DOWN for %s: %.1fA → %.1fA (schmitt=%.1fA, max -%.1fA/cycle)",
                sensor._attr_name,
                sensor._rate_limited_current,
                target,
                sensor._schmitt_current,
                max_down,
            )
        sensor._rate_limited_current = round(target, 1)

    return sensor._rate_limited_current
