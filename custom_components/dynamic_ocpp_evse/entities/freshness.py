"""Producer-freshness predicate for the entities fed by a hub's site cycle.

Most Load Juggler sensors publish nothing of their own: they read a value the
hub's site cycle wrote into ``hass.data``. That makes their honest availability
a question about the PRODUCER, not about themselves - a sensor whose producer
stopped running is not "0 W", it is unavailable, and the difference matters
because 0 A of grid draw reads as "the whole main breaker is free".

The window is deliberately generous: ``max(30 s, 3 x site_update_frequency)``.
Three cycles rides out a single slow or skipped tick (an engine cycle that
overran, a coordinator refresh that raised), and the 30 s floor keeps a fast
site (the default cadence is 2 s) from flapping to unavailable on any hiccup.

Pure Python: no Home Assistant, and no package-relative imports, so the pure
test tier can load this file straight from its path.
"""

# Never call a producer stale sooner than this, however fast its cycle is.
FRESHNESS_MIN_WINDOW_SECONDS = 30.0

# How many site cycles a producer may miss before its readers go unavailable.
FRESHNESS_CYCLE_MULTIPLIER = 3


def freshness_window_seconds(site_update_frequency) -> float:
    """Seconds a producer's last update stays usable, given its cycle length."""
    return max(
        FRESHNESS_MIN_WINDOW_SECONDS,
        FRESHNESS_CYCLE_MULTIPLIER * site_update_frequency,
    )


def is_producer_fresh(last_update, site_update_frequency, now) -> bool:
    """True when ``last_update`` is recent enough to trust its readers' values.

    Never updated (None) is stale. A timestamp in the future (clock stepped
    backwards) is fresh: it is evidence of a recent write, not of staleness.
    Every producer stamps an aware ``datetime.now(timezone.utc)`` (the hub's
    publish_hub_data, a load's processor and its control/ senders), and the
    cycle length is the hub form's 1-60 s number.
    """
    if last_update is None:
        return False
    age = (now - last_update).total_seconds()
    return age <= freshness_window_seconds(site_update_frequency)
