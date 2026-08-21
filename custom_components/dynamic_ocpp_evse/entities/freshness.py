"""Producer-freshness predicate for the entities fed by a hub's site cycle.

Most Load Juggler sensors publish nothing of their own: they read a value the
hub's site cycle wrote into ``hass.data``. That makes their honest availability
a question about the PRODUCER, not about themselves — a sensor whose producer
stopped running is not "0 W", it is unavailable, and the difference matters
because 0 A of grid draw reads as "the whole main breaker is free".

The window is deliberately generous: ``max(30 s, 3 x site_update_frequency)``.
Three cycles rides out a single slow or skipped tick (an engine cycle that
overran, a coordinator refresh that raised), and the 30 s floor keeps a fast
site (the default cadence is 2 s) from flapping to unavailable on any hiccup.

Pure Python: no Home Assistant, and no package-relative imports, so the pure
test tier can load this file straight from its path.
"""

import math
from datetime import datetime, timezone

# Never call a producer stale sooner than this, however fast its cycle is.
FRESHNESS_MIN_WINDOW_SECONDS = 30.0

# How many site cycles a producer may miss before its readers go unavailable.
FRESHNESS_CYCLE_MULTIPLIER = 3


def freshness_window_seconds(site_update_frequency) -> float:
    """Seconds a producer's last update stays usable, given its cycle length.

    Anything unparseable or nonsensical (None, a string, NaN, a negative
    period) degrades to the 30 s floor rather than to "always stale" — a
    misconfigured interval must not blank out every sensor on the site.
    """
    try:
        cycle = float(site_update_frequency)
    except (TypeError, ValueError):
        cycle = 0.0
    if not math.isfinite(cycle) or cycle < 0:
        cycle = 0.0
    return max(FRESHNESS_MIN_WINDOW_SECONDS, FRESHNESS_CYCLE_MULTIPLIER * cycle)


def producer_age_seconds(last_update, now=None):
    """Age of ``last_update`` in seconds, or None when it cannot be measured.

    None is the answer for "never updated" (``last_update`` is None) and for a
    timestamp that cannot be compared with ``now`` — mixing a naive datetime
    with an aware one raises, and a producer that cannot be dated is exactly
    the case this module exists to report as stale.

    A timestamp in the future (clock stepped backwards, or a producer stamping
    with a skewed clock) is clamped to age 0: it is evidence of a recent write,
    not of staleness.
    """
    if last_update is None:
        return None
    if now is None:
        now = datetime.now(timezone.utc)
    try:
        age = (now - last_update).total_seconds()
    except (TypeError, AttributeError):
        return None
    if not math.isfinite(age):
        return None
    return max(0.0, age)


def is_producer_fresh(last_update, site_update_frequency, now=None) -> bool:
    """True when ``last_update`` is recent enough to trust its readers' values."""
    age = producer_age_seconds(last_update, now)
    if age is None:
        return False
    return age <= freshness_window_seconds(site_update_frequency)
