"""Unit tests for the producer-freshness predicate - entities/freshness.py.

Machine-authored tests - not yet human-reviewed.

Load Juggler's sensors are readers: the value they show was produced by their
hub's site cycle, not by themselves. So "is this sensor available?" is really
"did the producer publish recently?", and this module is that question reduced
to arithmetic - no Home Assistant, no entity, no hass.data.

What the tests pin:
  * the window is max(30 s, 3 x cycle) - the floor protects a fast site from
    flapping, the multiplier lets a slow one miss a tick;
  * never-updated (None) is stale, which is what makes a sensor unavailable
    before the first cycle instead of publishing a 0 that reads as real;
  * a future timestamp counts as fresh, so a clock step cannot black out every
    sensor on the site.
"""

from datetime import datetime, timedelta, timezone

from custom_components.dynamic_ocpp_evse.entities.freshness import (
    FRESHNESS_CYCLE_MULTIPLIER,
    FRESHNESS_MIN_WINDOW_SECONDS,
    freshness_window_seconds,
    is_producer_fresh,
)

NOW = datetime(2026, 8, 18, 12, 0, 0, tzinfo=timezone.utc)


def _ago(seconds):
    return NOW - timedelta(seconds=seconds)


# ---------------------------------------------------------------------------
# The window
# ---------------------------------------------------------------------------


def test_fast_cycle_gets_the_thirty_second_floor():
    # The default site cadence is 2 s: 3 x 2 = 6 s would call a producer dead
    # after one slow engine run.
    assert freshness_window_seconds(2) == FRESHNESS_MIN_WINDOW_SECONDS
    assert freshness_window_seconds(10) == FRESHNESS_MIN_WINDOW_SECONDS


def test_slow_cycle_gets_three_cycles():
    assert freshness_window_seconds(20) == 60.0
    assert freshness_window_seconds(60) == 180.0


def test_the_crossover_is_exactly_ten_seconds():
    # 3 x 10 == the floor, so 10 s is the last cadence the floor still governs.
    assert freshness_window_seconds(10) == FRESHNESS_MIN_WINDOW_SECONDS
    assert freshness_window_seconds(10.1) > FRESHNESS_MIN_WINDOW_SECONDS
    assert FRESHNESS_CYCLE_MULTIPLIER == 3


# ---------------------------------------------------------------------------
# The predicate
# ---------------------------------------------------------------------------


def test_before_the_first_cycle_nothing_is_fresh():
    # This is the behaviour the whole change exists for: a reader with no
    # producer output yet is unavailable, not 0.
    assert is_producer_fresh(None, 2, NOW) is False


def test_recent_update_is_fresh():
    assert is_producer_fresh(_ago(1), 2, NOW) is True
    assert is_producer_fresh(_ago(29), 2, NOW) is True


def test_the_window_boundary_is_inclusive():
    assert is_producer_fresh(_ago(30), 2, NOW) is True
    assert is_producer_fresh(_ago(30.001), 2, NOW) is False


def test_a_stopped_producer_goes_stale():
    assert is_producer_fresh(_ago(31), 2, NOW) is False
    assert is_producer_fresh(_ago(3600), 2, NOW) is False


def test_a_slow_site_gets_its_longer_window():
    # 60 s cadence: two missed ticks still counts as alive, four does not.
    assert is_producer_fresh(_ago(120), 60, NOW) is True
    assert is_producer_fresh(_ago(240), 60, NOW) is False


def test_a_future_timestamp_is_fresh():
    assert is_producer_fresh(NOW + timedelta(hours=1), 2, NOW) is True
