"""Unit tests for the producer-freshness predicate — entities/freshness.py.

Machine-authored tests — not yet human-reviewed.

Load Juggler's sensors are readers: the value they show was produced by their
hub's site cycle, not by themselves. So "is this sensor available?" is really
"did the producer publish recently?", and this module is that question reduced
to arithmetic — no Home Assistant, no entity, no hass.data.

What the tests pin:
  * the window is max(30 s, 3 x cycle) — the floor protects a fast site from
    flapping, the multiplier lets a slow one miss a tick;
  * never-updated (None) is stale, which is what makes a sensor unavailable
    before the first cycle instead of publishing a 0 that reads as real;
  * a garbage cycle length degrades to the 30 s floor rather than to "always
    stale" — one bad option must not blank out an entire site;
  * a future timestamp counts as fresh, so a clock step cannot black out every
    sensor on the site.

Pure Python, no Home Assistant dependencies. Runnable two ways:
  python3 dev/tests/test_freshness.py     (standalone, no pytest needed)
  pytest dev/tests/test_freshness.py      (Docker / CI tier)
"""

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Module loading — freshness.py has no package-relative imports at all, so it
# loads straight from its path without the stub-package hierarchy the rest of
# the pure tier needs. Under pytest the real module is preferred when the
# component package has already been imported.
# ---------------------------------------------------------------------------
_FQN = "custom_components.dynamic_ocpp_evse.entities.freshness"
if _FQN in sys.modules:
    freshness = sys.modules[_FQN]
else:
    _PATH = (
        Path(__file__).resolve().parents[2]
        / "custom_components"
        / "dynamic_ocpp_evse"
        / "entities"
        / "freshness.py"
    )
    _spec = importlib.util.spec_from_file_location("lj_freshness_pure", _PATH)
    freshness = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(freshness)

freshness_window_seconds = freshness.freshness_window_seconds
producer_age_seconds = freshness.producer_age_seconds
is_producer_fresh = freshness.is_producer_fresh
FRESHNESS_MIN_WINDOW_SECONDS = freshness.FRESHNESS_MIN_WINDOW_SECONDS
FRESHNESS_CYCLE_MULTIPLIER = freshness.FRESHNESS_CYCLE_MULTIPLIER

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


def test_unusable_cycle_lengths_fall_back_to_the_floor():
    # A misconfigured or unreadable interval must not blank out the whole site.
    for bad in (None, "", "fast", 0, -5, float("nan"), float("inf")):
        assert freshness_window_seconds(bad) == FRESHNESS_MIN_WINDOW_SECONDS, bad


# ---------------------------------------------------------------------------
# Age
# ---------------------------------------------------------------------------


def test_never_updated_has_no_age():
    assert producer_age_seconds(None, NOW) is None


def test_age_is_measured_from_now():
    assert producer_age_seconds(_ago(12), NOW) == 12.0


def test_a_future_timestamp_is_clamped_to_zero_not_negative():
    assert producer_age_seconds(NOW + timedelta(seconds=90), NOW) == 0.0


def test_a_naive_timestamp_cannot_be_aged():
    # Mixing naive and aware datetimes raises; the answer is "cannot tell",
    # which the predicate treats as stale rather than crashing a sensor.
    assert producer_age_seconds(datetime(2026, 8, 18, 12, 0, 0), NOW) is None


def test_a_non_datetime_cannot_be_aged():
    assert producer_age_seconds("2026-08-18T12:00:00", NOW) is None
    assert producer_age_seconds(1755518400, NOW) is None


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


def test_now_defaults_to_the_wall_clock():
    # Called without `now` the predicate must still answer sensibly — the
    # entity property does pass one, but nothing in the signature requires it.
    assert is_producer_fresh(datetime.now(timezone.utc), 2) is True
    assert is_producer_fresh(datetime(2000, 1, 1, tzinfo=timezone.utc), 2) is False


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    # Deliberately pytest-free: the pure tier has to run on the developer's
    # machine, which has no pytest (dev/tests/conftest.py imports HA anyway).
    failed = []
    for _name, _fn in sorted(list(globals().items())):
        if not _name.startswith("test_") or not callable(_fn):
            continue
        try:
            _fn()
        except Exception as exc:  # noqa: BLE001 - report and continue
            failed.append((_name, exc))
            print(f"FAIL {_name}: {type(exc).__name__}: {exc}")
        else:
            print(f"PASS {_name}")
    print(f"\n{'FAILED' if failed else 'OK'} — {len(failed)} failure(s)")
    sys.exit(1 if failed else 0)
