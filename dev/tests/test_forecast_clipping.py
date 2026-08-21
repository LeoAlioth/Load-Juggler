"""Tests for the PV clipping forecast — calculations.forecast.

Machine-authored tests — not yet human-reviewed.

The forecast is a mapping of block-start timestamps to average watts; each
block is constant power for its duration, so the maths is a plain sum:

    clipped_kwh    = Σ max(0, p − T) × h
    absorbable_kwh = Σ min(charge_cap, max(0, p − T)) × h

with ``T = export limit + base consumption``. Block width comes from
consecutive timestamps, the block containing ``now`` is prorated, and blocks
at or beyond ``until`` (the start of the next local day) are excluded so
tomorrow's peak is not reserved for twice. Arrays are summed *before*
clipping — the nonlinearity is site-level.
"""

from datetime import datetime, timedelta, timezone

from custom_components.dynamic_ocpp_evse.calculations import (
    battery_max_soc,
    clipping_forecast,
    headroom_deficit_kwh,
    merge_forecast_series,
    recommended_charge_limit,
    unexportable_power,
)

T0 = datetime(2026, 8, 14, 6, 0, tzinfo=timezone.utc)
THRESHOLD = 6000.0  # 5 kW export limit + 1 kW base consumption


def _series(watts_by_hour, start=T0, step_minutes=60):
    """Build a forecast series from a list of block powers."""
    step = timedelta(minutes=step_minutes)
    return {start + i * step: w for i, w in enumerate(watts_by_hour)}


def _fc(series, now=None, until=None, **kwargs):
    now = now or min(series)
    until = until or (max(series) + timedelta(hours=1))
    return clipping_forecast(series, THRESHOLD, now, until, **kwargs)


# --- The block sum -----------------------------------------------------------

def test_single_block_above_threshold():
    fc = _fc(_series([8000, 0]))
    assert fc.clipped_kwh == 2.0


def test_blocks_below_threshold_contribute_nothing():
    fc = _fc(_series([4000, 5999, 0]))
    assert fc.clipped_kwh == 0.0
    assert fc.absorbable_kwh == 0.0


def test_full_day_shape():
    # 6 kW threshold against a midday hump: 2 + 4 + 4 + 2 = 12 kWh clipped.
    fc = _fc(_series([2000, 5000, 8000, 10000, 10000, 8000, 5000, 2000]))
    assert fc.clipped_kwh == 12.0


def test_block_width_from_timestamps_not_assumed_hourly():
    # 15-minute blocks: 8 kW for one block clips 2 kW × 0.25 h = 0.5 kWh.
    fc = _fc(_series([8000, 0], step_minutes=15))
    assert fc.clipped_kwh == 0.5


def test_dst_double_width_block():
    # A 2-hour gap between timestamps means a 2-hour block.
    series = {T0: 8000.0, T0 + timedelta(hours=2): 0.0}
    fc = _fc(series)
    assert fc.clipped_kwh == 4.0


def test_last_block_inherits_previous_width():
    # Two 30-minute blocks; the last has no successor and copies the width.
    series = {T0: 0.0, T0 + timedelta(minutes=30): 8000.0}
    fc = _fc(series, until=T0 + timedelta(hours=2))
    assert fc.clipped_kwh == 1.0


# --- The window: now and until -----------------------------------------------

def test_in_progress_block_is_prorated():
    # now is 30 min into an 8 kW block — only half of it remains.
    fc = _fc(_series([8000, 0]), now=T0 + timedelta(minutes=30))
    assert fc.clipped_kwh == 1.0


def test_tomorrows_peak_excluded_by_until():
    # Same peak today and tomorrow; the horizon keeps only today's.
    series = _series([8000, 0])
    series.update(_series([8000, 0], start=T0 + timedelta(hours=24)))
    fc = _fc(series, until=T0 + timedelta(hours=18))
    assert fc.clipped_kwh == 2.0


def test_block_straddling_until_is_prorated():
    fc = _fc(_series([8000, 8000]), until=T0 + timedelta(hours=1, minutes=30))
    assert fc.clipped_kwh == 3.0


def test_window_entirely_past_forecast_is_empty():
    fc = _fc(_series([8000, 0]), now=T0 + timedelta(hours=6),
             until=T0 + timedelta(hours=12))
    assert fc.clipped_kwh == 0.0
    assert fc.peak_at is None


def test_empty_series_is_empty():
    fc = clipping_forecast({}, THRESHOLD, T0, T0 + timedelta(hours=12))
    assert fc.clipped_kwh == 0.0
    assert fc.absorbable_kwh == 0.0
    assert fc.window_hours == 0.0


# --- Charge-rate and inverter caps -------------------------------------------

def test_charge_cap_binds_absorbable_below_clipped():
    # 4 kW of excess against a 3 kW charge rate: 1 kWh can never be stored.
    fc = _fc(_series([10000, 0]), charge_cap_w=3000)
    assert fc.clipped_kwh == 4.0
    assert fc.absorbable_kwh == 3.0


def test_charge_cap_idle_when_excess_below_it():
    fc = _fc(_series([8000, 0]), charge_cap_w=5000)
    assert fc.absorbable_kwh == fc.clipped_kwh == 2.0


def test_power_cap_limits_summed_series():
    # Open-Meteo models 12 kW from kWp, but the inverters top out at 9 kW.
    fc = _fc(_series([12000, 0]), power_cap_w=9000)
    assert fc.clipped_kwh == 3.0
    assert fc.peak_w == 9000


# --- Multi-array merge: sum before clipping ----------------------------------

def test_two_arrays_summed_before_clipping():
    # Two 4 kW arrays against T = 6 kW clip 2 kW — clipping each alone clips 0.
    merged = merge_forecast_series([_series([4000, 0]), _series([4000, 0])])
    fc = _fc(merged)
    assert fc.clipped_kwh == 2.0


def test_shorter_array_contributes_zero_outside_its_range():
    # The short array must not extend its last sample over the long one's tail.
    long = _series([4000, 4000, 4000, 0])
    short = _series([4000])
    merged = merge_forecast_series([long, short])
    fc = _fc(merged)
    assert fc.clipped_kwh == 2.0  # only the first hour reaches 8 kW


def test_merge_of_nothing_is_empty():
    assert merge_forecast_series([]) == {}
    assert merge_forecast_series([{}, {}]) == {}


# --- The SOC recommendation ---------------------------------------------------

def test_max_soc_leaves_exact_headroom():
    # 4 kWh to absorb into a 10 kWh pack → ceiling 60 %.
    assert battery_max_soc(4.0, 10.0, soc_floor=30.0) == 60.0


def test_max_soc_is_ceiling_with_nothing_to_absorb():
    assert battery_max_soc(0.0, 10.0, soc_floor=30.0) == 100.0


def test_max_soc_clamped_to_floor():
    # 20 kWh to absorb into a 10 kWh pack cannot push below the floor.
    assert battery_max_soc(20.0, 10.0, soc_floor=30.0) == 30.0


def test_max_soc_zero_capacity_fails_open():
    assert battery_max_soc(4.0, 0.0, soc_floor=30.0) == 100.0


def test_max_soc_rises_as_now_advances():
    # Fixed series, advancing now: the remaining clip shrinks, so the ceiling
    # only ever rises, reaching 100 % once the clipping window has passed.
    series = _series([8000, 10000, 8000, 0])
    previous = -1.0
    for hours in range(0, 6):
        fc = _fc(series, now=T0 + timedelta(hours=hours),
                 until=T0 + timedelta(hours=18))
        ceiling = battery_max_soc(fc.absorbable_kwh, 10.0, soc_floor=30.0)
        assert ceiling >= previous
        previous = ceiling
    assert previous == 100.0


# --- The deficit: honesty when the advice cannot be met -----------------------

def test_deficit_zero_while_achievable():
    # 4 kWh needed, battery at 50 % of 10 kWh has 5 kWh of room.
    assert headroom_deficit_kwh(4.0, 10.0, 50.0) == 0.0


def test_deficit_positive_when_soc_exceeds_ceiling():
    # 4 kWh needed, battery at 80 % has only 2 kWh of room.
    assert headroom_deficit_kwh(4.0, 10.0, 80.0) == 2.0


def test_deficit_capped_by_capacity():
    # Needing 20 kWh of a 10 kWh pack at 0 % SOC: the pack's full 10 kWh is
    # available, and more was never achievable.
    assert headroom_deficit_kwh(20.0, 10.0, 0.0) == 0.0


def test_deficit_unknown_soc_is_zero():
    assert headroom_deficit_kwh(4.0, 10.0, None) == 0.0


# --- The charge-rate cap and its release rules ---------------------------------

def test_cap_released_when_nothing_to_clip():
    assert recommended_charge_limit(
        0.0, 90.0, 100.0, 5000.0, 0.0, THRESHOLD, 2.0
    ) == 5000.0


def test_cap_released_below_the_band():
    # SOC 50 against a 60 % ceiling with 2 % hysteresis: headroom not at risk.
    assert recommended_charge_limit(
        4.0, 50.0, 60.0, 5000.0, 10000.0, THRESHOLD, 2.0
    ) == 5000.0


def test_cap_restricts_to_unexportable_power_at_the_ceiling():
    # At the ceiling, charge only with power that could not have been exported.
    assert recommended_charge_limit(
        4.0, 60.0, 60.0, 5000.0, 8000.0, THRESHOLD, 2.0
    ) == 2000.0


def test_cap_zero_at_ceiling_with_exportable_production():
    # Production below the threshold is exportable — charging would spend the
    # reserved headroom, so 0 is the correct setpoint here.
    assert recommended_charge_limit(
        4.0, 60.0, 60.0, 5000.0, 4000.0, THRESHOLD, 2.0
    ) == 0.0


def test_cap_never_exceeds_full_rate():
    assert recommended_charge_limit(
        4.0, 60.0, 60.0, 5000.0, 20000.0, THRESHOLD, 2.0
    ) == 5000.0


def test_unexportable_power_clamps_at_zero():
    assert unexportable_power(4000.0, THRESHOLD) == 0.0
    assert unexportable_power(8000.0, THRESHOLD) == 2000.0
    assert unexportable_power(None, THRESHOLD) == 0.0
