"""Variable Load arithmetic (JUG-30) - pure, no Home Assistant.

The site allocation sizes a variable load like any modulating load: one current
per leg, on the legs it is configured on. What is left here is the device's
side of it: its bounds in amps from the setpoint unit, the setpoint written for
an allowance, the published allowance figures, and the phase advice.
"""

import math

SINGLE = "single"
MULTI = "multi"


def bounds_amps(unit, low, high, voltage, legs):
    """``(min, max)`` per-leg amps of bounds given in the setpoint unit:
    amps per leg as they are, watts spread over the legs at the phase voltage."""
    if unit == "W":
        per = voltage * max(legs, 1)
        return (low / per, high / per) if per > 0 else (0.0, 0.0)
    return float(low), float(high)


def setpoint(amps, unit, voltage, legs, bias=0.0, entity_min=None, entity_max=None, step=None):
    """The value written for a per-leg allowance of ``amps``.

    In the setpoint unit (amps per leg, or watts over the legs), plus ``bias``
    (the device delivers less than it is told) while anything is allowed,
    floored to the entity's ``step`` so it never asks for more than allowed,
    and kept inside the entity's own range - an allowance of 0 on an entity
    that cannot take 0 becomes its minimum (Hold, a stop)."""
    value = amps if unit != "W" else amps * voltage * max(legs, 1)
    if value > 0:
        value += bias
    if step:
        value = math.floor(round(value / step, 6)) * step
    if entity_max is not None:
        value = min(value, entity_max)
    if entity_min is not None:
        value = max(value, entity_min)
    return round(value, 3)


def sized_legs(readings, remembered, disconnected, drawing_a=1.0):
    """The leg numbers (1-3) a device is sized on, from its per-leg readings
    (``None`` for a leg with no sensor): the legs drawing over ``drawing_a``.
    While none draws, the legs ``remembered`` from when it last drew, so a
    single-phase device that stopped restarts as one - until it is
    ``disconnected``, when a new session starts on every configured leg ([]).
    An empty result means every configured leg."""
    drawing = [n for n, a in enumerate(readings, 1) if a is not None and a > drawing_a]
    if drawing:
        return drawing
    return [] if disconnected else list(remembered or [])


def outputs(amps, legs, sized, voltage, max_amps):
    """The allowance figures, from the per-leg allowance ``amps``.

    ``legs``: the site phases of the configured legs, L1 first; ``sized``: the
    leg numbers (1-3) the allocation sized it on - the legs in use (empty:
    every configured leg). A sized leg carries ``amps``, a configured leg it is
    not sized on 0 (nothing was allocated there), a leg not configured None.
    The lowest is over the sized legs, the power over them too (what it may
    draw as it is wired now), the percentage of its per-leg maximum."""
    sized = list(sized or range(1, len(legs) + 1))
    per_leg = {
        n: (None if n > len(legs) else round(amps, 1) if n in sized else 0.0)
        for n in (1, 2, 3)
    }
    return {
        "allowed_l1": per_leg[1],
        "allowed_l2": per_leg[2],
        "allowed_l3": per_leg[3],
        "allowed_lowest": min(per_leg[n] for n in sized),
        "allowed_power": round(amps * voltage * len(sized)),
        "allowed_percent": round(100 * amps / max_amps) if max_amps > 0 else 0,
    }


def phase_advice(power_w, memory, now, multi_above, single_below, min_interval_s):
    """``(advice, reason, memory)``: whether the device should run on one leg or
    on all of them, from its allowed power. Advice only - nothing is written.

    Multi above ``multi_above`` W, single below ``single_below`` W, and between
    the two the last advice holds (the hysteresis). A change waits until
    ``min_interval_s`` has passed since the last one. ``memory`` is the
    ``(advice, since)`` returned last time (None at first: the first advice is
    taken as it stands, single anywhere below ``multi_above``)."""
    if power_w > multi_above:
        wanted, why = MULTI, f"allowed {power_w:.0f} W is above {multi_above:.0f} W"
    elif power_w < single_below:
        wanted, why = SINGLE, f"allowed {power_w:.0f} W is below {single_below:.0f} W"
    else:
        wanted, why = None, f"allowed {power_w:.0f} W is between the thresholds"
    if memory is None:
        advice = wanted or SINGLE
        return advice, why, (advice, now)
    advice, since = memory
    if wanted is None or wanted == advice:
        return advice, why, memory
    left = min_interval_s - (now - since)
    if left > 0:
        return advice, f"{why}; held {math.ceil(left / 60)} min more (minimum time between changes)", memory
    return wanted, why, (wanted, now)
