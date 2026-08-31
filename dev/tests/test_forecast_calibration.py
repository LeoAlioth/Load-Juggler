"""Forecast calibration arithmetic — calculations/calibration.py.

Two observers, two different errors. The tests that matter are the ones pinning
WHY each measurement is shaped the way it is: energy-weighting rather than a
mean of ratios, constrained intervals excluded rather than whole days, and the
peakiness gap measured from real samples rather than modelled.

Docker / CI tier:
  pytest dev/tests/test_forecast_calibration.py
"""

from datetime import datetime, timedelta, timezone

from custom_components.dynamic_ocpp_evse.calculations.calibration import (
    GAIN_CLAMP_HIGH,
    GAIN_CLAMP_LOW,
    block_power_at,
    clip_pair,
    day_ratio,
    note_gain_sample,
    update_gain,
)

T0 = datetime(2026, 8, 31, 6, 0, tzinfo=timezone.utc)
QUARTER = timedelta(minutes=15)


def _series(*watts):
    return {T0 + i * QUARTER: w for i, w in enumerate(watts)}


# --- Finding the forecast block for a moment -------------------------------


def test_the_block_containing_the_moment_is_returned():
    s = _series(1000.0, 2000.0, 3000.0)
    assert block_power_at(s, T0) == 1000.0
    assert block_power_at(s, T0 + timedelta(minutes=14)) == 1000.0
    assert block_power_at(s, T0 + timedelta(minutes=15)) == 2000.0


def test_before_the_series_starts_is_unknown():
    s = _series(1000.0)
    assert block_power_at(s, T0 - timedelta(minutes=1)) is None


def test_past_the_end_is_unknown_not_zero():
    """A forecast that has run out is different from a forecast of nothing —
    counting it as 0 W would feed the learner a free perfect-overestimate."""
    s = _series(1000.0, 2000.0)
    assert block_power_at(s, T0 + timedelta(minutes=30)) is None


def test_an_empty_series_is_unknown():
    assert block_power_at({}, T0) is None
    assert block_power_at(None, T0) is None


# --- Accumulating a day ----------------------------------------------------


def test_an_empty_dict_is_a_valid_fresh_day():
    st = note_gain_sample({}, 2000.0, 1800.0, 0.25, False)
    assert st["forecast_wh"] == 500.0
    assert st["actual_wh"] == 450.0
    assert st["skipped_wh"] == 0.0


def test_constrained_intervals_are_excluded_from_both_sums():
    """While curtailing, production is suppressed by the very thing being
    forecast — counting it would teach the learner the forecast reads high
    exactly when it matters."""
    st = note_gain_sample({}, 4000.0, 2500.0, 0.25, True)
    assert st["forecast_wh"] == 0.0
    assert st["actual_wh"] == 0.0
    # Still counted, so the observation can report what it threw away.
    assert st["skipped_wh"] == 1000.0


def test_a_clipping_day_still_contributes_its_unconstrained_hours():
    """The reason exclusion is per INTERVAL and not per day: on an
    export-limited site most sunny days curtail at midday, and dropping them
    whole would leave the gain learning only from overcast days."""
    st = {}
    st = note_gain_sample(st, 1000.0, 1100.0, 1.0, False)   # morning, honest
    st = note_gain_sample(st, 9000.0, 6000.0, 2.0, True)    # midday, curtailed
    st = note_gain_sample(st, 800.0, 880.0, 1.0, False)     # evening, honest
    assert st["forecast_wh"] == 1800.0
    assert st["actual_wh"] == 1980.0
    assert st["skipped_wh"] == 18000.0
    assert round(day_ratio(st, min_wh=1000.0), 4) == 1.1


def test_dim_blocks_are_skipped():
    """Dawn and dusk carry no information about an array's calibration and
    would let a near-zero denominator dominate."""
    st = note_gain_sample({}, 20.0, 500.0, 0.25, False)
    assert st["forecast_wh"] == 0.0
    assert st["skipped_wh"] == 5.0


def test_missing_readings_contribute_nothing():
    for forecast, actual in ((None, 100.0), (100.0, None)):
        st = note_gain_sample({}, forecast, actual, 0.25, False)
        assert st == {"forecast_wh": 0.0, "actual_wh": 0.0, "skipped_wh": 0.0}


# --- The day's ratio -------------------------------------------------------


def test_the_ratio_is_energy_weighted_not_a_mean_of_ratios():
    """One big honest hour must outweigh a handful of tiny lopsided blocks. A
    mean of per-block ratios would answer ~1.75 here; the energy-weighted
    answer is 1.02."""
    st = {}
    st = note_gain_sample(st, 4000.0, 4000.0, 1.0, False)   # ratio 1.00, 4 kWh
    st = note_gain_sample(st, 100.0, 250.0, 0.25, False)    # ratio 2.50, 25 Wh
    ratio = day_ratio(st, min_wh=1000.0)
    assert round(ratio, 4) == round(4062.5 / 4025.0, 4)
    assert 1.0 < ratio < 1.02


def test_an_uninformative_day_yields_no_ratio():
    st = note_gain_sample({}, 1000.0, 900.0, 0.25, False)   # 250 Wh only
    assert day_ratio(st) is None


def test_no_data_yields_no_ratio():
    assert day_ratio({}) is None
    assert day_ratio(None) is None


# --- Folding a day into the gain ------------------------------------------


def test_a_day_moves_the_gain_one_tenth_of_the_way():
    assert round(update_gain(1.0, 1.10, weight=0.1), 4) == 1.01


def test_an_uninformative_day_leaves_the_gain_alone():
    assert update_gain(1.07, None) == 1.07


def test_the_gain_is_clamped_at_both_ends():
    assert update_gain(GAIN_CLAMP_HIGH, 5.0) == GAIN_CLAMP_HIGH
    assert update_gain(GAIN_CLAMP_LOW, 0.1) == GAIN_CLAMP_LOW


def test_a_run_of_extreme_days_parks_at_the_bound_and_stays_visible():
    """The clamp is on the RESULT, so a persistently wrong array pins the gain
    at 1.25 where the published value shows it, rather than averaging into
    something that looks moderate."""
    gain = 1.0
    for _ in range(200):
        gain = update_gain(gain, 3.0)
    assert gain == GAIN_CLAMP_HIGH


def test_converges_on_a_steady_bias():
    gain = 1.0
    for _ in range(100):
        gain = update_gain(gain, 1.08)
    assert round(gain, 3) == 1.08


# --- Peakiness: the Jensen gap, measured ----------------------------------


def test_a_steady_window_has_no_gap():
    samples = [(0.05, 5000.0)] * 5
    true_wh, block_wh = clip_pair(samples, 4000.0)
    assert round(true_wh, 6) == round(block_wh, 6)


def test_a_broken_window_clips_where_the_average_says_nothing():
    """The whole effect in one case: the average sits exactly on the limit, so
    the block-average integral reports no clipping at all, while the real trace
    spends half its time 2 kW above it."""
    samples = [(0.125, 7000.0), (0.125, 3000.0)]
    true_wh, block_wh = clip_pair(samples, 5000.0)
    assert block_wh == 0.0
    assert round(true_wh, 4) == 250.0


def test_the_gap_is_one_directional():
    """Troughs never cancel peaks — power below the limit is not negative
    clipping — so the measured truth can only ever exceed the block figure."""
    for peak, trough in ((9000.0, 1000.0), (6000.0, 4000.0), (5001.0, 4999.0)):
        true_wh, block_wh = clip_pair(
            [(0.125, peak), (0.125, trough)], 5000.0
        )
        assert true_wh >= block_wh


def test_a_window_entirely_above_the_limit_has_no_gap():
    """Where the whole window clips the function is linear, so averaging costs
    nothing — which is why the gap concentrates at the limit."""
    samples = [(0.125, 9000.0), (0.125, 7000.0)]
    true_wh, block_wh = clip_pair(samples, 5000.0)
    assert round(true_wh, 6) == round(block_wh, 6)


def test_an_empty_window_measures_nothing():
    assert clip_pair([], 5000.0) == (0.0, 0.0)
    assert clip_pair([(0.0, 9000.0)], 5000.0) == (0.0, 0.0)
