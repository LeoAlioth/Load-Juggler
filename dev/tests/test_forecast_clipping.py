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
    FORECAST_EARLY_START_FACTOR,
    FORECAST_TRIM_CLAMP_W,
    FORECAST_TRIM_MAX_STEP_S,
    FORECAST_TRIM_TAU_S,
    battery_max_soc,
    clipping_forecast,
    export_trim,
    first_production_at,
    headroom_deficit_kwh,
    hours_to_shed,
    merge_forecast_series,
    recommended_charge_limit,
    reservation_is_due,
    select_clipping_window,
    unexportable_power,
    yields_to_excess,
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


# --- The next clipping window -------------------------------------------------
#
# The reservation is measured against the NEXT clip, not the rest of the
# calendar day: the remainder of today while today still clips, tomorrow's peak
# once it does not, and never further than FORECAST_LOOKAHEAD_DAYS.

# Local midnights around T0 (06:00 on the 14th), as forecast_windows builds them.
MIDNIGHT_1 = datetime(2026, 8, 15, 0, 0, tzinfo=timezone.utc)
MIDNIGHT_2 = datetime(2026, 8, 16, 0, 0, tzinfo=timezone.utc)
MIDNIGHT_3 = datetime(2026, 8, 17, 0, 0, tzinfo=timezone.utc)


def _windows(now):
    """The candidate windows forecast_windows() would build for ``now``."""
    return [(now, MIDNIGHT_1), (MIDNIGHT_1, MIDNIGHT_2)]


def _three_day_series():
    """8 kW for one hour at 10:00 on each of three consecutive days."""
    series = {}
    for day in (14, 15, 16):
        series[datetime(2026, 8, day, 10, 0, tzinfo=timezone.utc)] = 8000.0
        series[datetime(2026, 8, day, 11, 0, tzinfo=timezone.utc)] = 0.0
    return series


def test_window_is_the_rest_of_today_while_today_still_clips():
    # Byte-equivalence with the single-window behaviour: same index, and the
    # very same integration clipping_forecast would have produced on its own.
    now = datetime(2026, 8, 14, 8, 0, tzinfo=timezone.utc)
    series = _three_day_series()
    index, fc = select_clipping_window(series, THRESHOLD, _windows(now))
    assert index == 0
    assert fc == clipping_forecast(series, THRESHOLD, now, MIDNIGHT_1)
    assert fc.clipped_kwh == 2.0


def test_window_becomes_tomorrow_once_todays_clip_has_integrated_away():
    # 18:00: today's peak is hours past, so today's window holds nothing. The
    # next appointment is tomorrow's 10:00 peak.
    now = datetime(2026, 8, 14, 18, 0, tzinfo=timezone.utc)
    series = _three_day_series()
    index, fc = select_clipping_window(series, THRESHOLD, _windows(now))
    assert index == 1
    assert fc.clipped_kwh == 2.0
    assert fc.peak_at == datetime(2026, 8, 15, 10, 0, tzinfo=timezone.utc)


def test_lookahead_stops_one_day_out():
    # Nothing today, nothing tomorrow, a clip the day after: not consulted, so
    # the recommendation rests at the destination (no clip at all).
    now = datetime(2026, 8, 14, 18, 0, tzinfo=timezone.utc)
    series = {
        datetime(2026, 8, 16, 10, 0, tzinfo=timezone.utc): 8000.0,
        datetime(2026, 8, 16, 11, 0, tzinfo=timezone.utc): 0.0,
    }
    index, fc = select_clipping_window(series, THRESHOLD, _windows(now))
    assert index == 0
    assert fc.clipped_kwh == 0.0
    assert fc.absorbable_kwh == 0.0
    # And the cap really is the reason: hand the same search a third window and
    # the day-after clip is found.
    index, fc = select_clipping_window(
        series, THRESHOLD, _windows(now) + [(MIDNIGHT_2, MIDNIGHT_3)]
    )
    assert index == 2
    assert fc.clipped_kwh == 2.0


def test_a_float_dust_tail_does_not_hold_the_window_on_today():
    # 1 mWh of clip left today — below the epsilon, and below what the kWh
    # sensors can even show — must not stop the search reaching tomorrow.
    now = datetime(2026, 8, 14, 18, 0, tzinfo=timezone.utc)
    series = _three_day_series()
    series[datetime(2026, 8, 14, 19, 0, tzinfo=timezone.utc)] = THRESHOLD + 0.001
    series[datetime(2026, 8, 14, 20, 0, tzinfo=timezone.utc)] = 0.0
    index, fc = select_clipping_window(series, THRESHOLD, _windows(now))
    assert index == 1
    assert fc.peak_at == datetime(2026, 8, 15, 10, 0, tzinfo=timezone.utc)


def test_window_selection_passes_the_caps_through():
    now = datetime(2026, 8, 14, 18, 0, tzinfo=timezone.utc)
    series = _three_day_series()
    index, fc = select_clipping_window(
        series, THRESHOLD, _windows(now), charge_cap_w=500, power_cap_w=7000
    )
    assert index == 1
    assert fc.clipped_kwh == 1.0  # 7 kW cap → 1 kW of excess for one hour
    assert fc.absorbable_kwh == 0.5  # 500 W charge cap binds


def test_window_selection_with_no_windows_is_empty():
    index, fc = select_clipping_window({}, THRESHOLD, [])
    assert index == 0
    assert fc.clipped_kwh == 0.0


# --- When production starts ---------------------------------------------------
#
# The deadline the overnight floor drop is scheduled against: the moment
# forecast production overtakes base consumption, which is when the battery
# would stop discharging — NOT first light.

BASE_W = 300.0

# A dawn: pre-dawn dribble below the house draw, then the crossing at 08:00.
DAWN = {
    datetime(2026, 8, 15, 6, 0, tzinfo=timezone.utc): 50.0,
    datetime(2026, 8, 15, 7, 0, tzinfo=timezone.utc): 200.0,
    datetime(2026, 8, 15, 8, 0, tzinfo=timezone.utc): 400.0,
    datetime(2026, 8, 15, 9, 0, tzinfo=timezone.utc): 3000.0,
}
NIGHT = datetime(2026, 8, 15, 0, 0, tzinfo=timezone.utc)
HORIZON = datetime(2026, 8, 16, 0, 0, tzinfo=timezone.utc)


def test_production_starts_where_it_overtakes_the_house_not_at_first_light():
    # First light is 06:00 at 50 W; the battery is still emptying then, and goes
    # on emptying through the 200 W block. 08:00 is when it stops.
    assert first_production_at(DAWN, BASE_W, NIGHT, HORIZON) == datetime(
        2026, 8, 15, 8, 0, tzinfo=timezone.utc
    )


def test_production_under_way_reports_now():
    # Mid-morning: the block containing ``start`` is already above base, so
    # there is no wait to schedule and the answer is ``start`` itself.
    now = datetime(2026, 8, 15, 9, 30, tzinfo=timezone.utc)
    assert first_production_at(DAWN, BASE_W, now, HORIZON) == now


def test_production_never_reaching_the_house_draw_is_none():
    overcast = {
        datetime(2026, 8, 15, h, 0, tzinfo=timezone.utc): 120.0
        for h in range(6, 18)
    }
    assert first_production_at(overcast, BASE_W, NIGHT, HORIZON) is None


def test_production_beyond_the_horizon_is_none():
    assert first_production_at(DAWN, BASE_W, NIGHT, NIGHT) is None
    assert first_production_at({}, BASE_W, NIGHT, HORIZON) is None


def test_production_start_respects_the_power_cap():
    # A 3 kW-capped site never sees the modelled 400 W block as 400 W... it
    # does; but a cap BELOW base hides the crossing entirely.
    assert first_production_at(DAWN, BASE_W, NIGHT, HORIZON, power_cap_w=250) is None


# --- The just-in-time floor drop ----------------------------------------------
#
# The maintainer's worked example throughout: destination 95 %, tomorrow's clip
# 2 kWh, a 20 kWh pack, 300 W base consumption, production from 08:30. The
# reserve is 85 %, so a full pack has 2 kWh to shed at 300 W = 6 h 40 min, and
# the early-start factor asks for 8 h — putting the drop at 00:30.

PRODUCTION_AT = datetime(2026, 8, 15, 8, 30, tzinfo=timezone.utc)
RESERVED = 85.0
CAPACITY = 20.0


def _due(now, soc=95.0, was_due=False, reserved=RESERVED, base=BASE_W):
    return reservation_is_due(
        now, PRODUCTION_AT, soc, reserved, CAPACITY, base, was_due
    )


def test_the_shed_estimate_is_energy_over_base_consumption():
    assert hours_to_shed(95.0, 85.0, 20.0, 300.0) == 2.0 / 0.3
    assert round(hours_to_shed(95.0, 85.0, 20.0, 300.0), 4) == 6.6667
    # At or below the reserve there is nothing to shed.
    assert hours_to_shed(85.0, 85.0, 20.0, 300.0) == 0.0
    assert hours_to_shed(60.0, 85.0, 20.0, 300.0) == 0.0
    # And no estimate at all without the terms to make one from.
    assert hours_to_shed(None, 85.0, 20.0, 300.0) is None
    assert hours_to_shed(95.0, 85.0, 0.0, 300.0) is None
    assert hours_to_shed(95.0, 85.0, 20.0, 0.0) is None


def test_the_worked_example_holds_the_evening_and_drops_at_half_past_midnight():
    # 20:00, 18:00 the evening before, 00:00: still held.
    for hour in (18, 20, 22):
        now = datetime(2026, 8, 14, hour, 0, tzinfo=timezone.utc)
        assert _due(now) == (False, False), f"dropped early at {hour}:00"
    assert _due(datetime(2026, 8, 15, 0, 0, tzinfo=timezone.utc)) == (False, False)
    # 00:29 is 8 h 1 min out — one minute too soon.
    assert _due(datetime(2026, 8, 15, 0, 29, tzinfo=timezone.utc)) == (False, False)
    # 00:30 is exactly 8 h = 6 h 40 min × 1.2. The drop lands here.
    assert _due(datetime(2026, 8, 15, 0, 30, tzinfo=timezone.utc)) == (True, True)


def test_the_early_start_factor_is_what_moves_the_drop_before_the_arithmetic():
    # Without the factor the shed needs 6 h 40 min, putting the drop at 01:50.
    plain_hours = hours_to_shed(95.0, RESERVED, CAPACITY, BASE_W)
    assert PRODUCTION_AT - timedelta(hours=plain_hours) == datetime(
        2026, 8, 15, 1, 50, tzinfo=timezone.utc
    )
    # The factor buys 1 h 20 min of slack, and buys it EARLIER — 00:30, not
    # later. Arriving full costs clipped kWh; arriving early costs nothing.
    factored = PRODUCTION_AT - timedelta(
        hours=plain_hours * FORECAST_EARLY_START_FACTOR
    )
    assert factored == datetime(2026, 8, 15, 0, 30, tzinfo=timezone.utc)
    assert factored < PRODUCTION_AT - timedelta(hours=plain_hours)


def test_a_night_already_too_short_drops_at_once():
    # Today's clip zeroes at 04:00 with a full pack and production at 08:30:
    # 4.5 h left against 8 h needed. Nothing to schedule — drop now.
    assert _due(datetime(2026, 8, 15, 4, 0, tzinfo=timezone.utc)) == (True, True)


def test_the_drop_stays_dropped_when_the_pack_empties_faster_than_base():
    # Dropped at 00:30. An hour later the house has taken twice base, so the
    # UNLATCHED arithmetic would say "not yet" — 7.5 h left against 1.2 × 5.33 h
    # = 6.4 h — and the advice would climb back to the destination in the dark.
    later = datetime(2026, 8, 15, 1, 30, tzinfo=timezone.utc)
    assert _due(later, soc=93.4) == (False, False)
    assert _due(later, soc=93.4, was_due=True) == (True, True)


def test_the_shed_completing_early_keeps_the_reservation():
    # The pack reaches the reserve at 04:00, hours before dawn: the floor holds
    # it there and the house moves to the grid — the recommendation must not
    # drift back up.
    early = datetime(2026, 8, 15, 4, 0, tzinfo=timezone.utc)
    assert _due(early, soc=85.0, was_due=True) == (True, True)
    assert _due(early, soc=80.0, was_due=True) == (True, True)


def test_a_dropped_reservation_survives_an_soc_sensor_dropout():
    night = datetime(2026, 8, 15, 3, 0, tzinfo=timezone.utc)
    assert _due(night, soc=None, was_due=True) == (True, True)


def test_an_unknown_soc_holds_at_the_destination():
    # No SOC and nothing latched: hold. Evicting a battery on a number we do not
    # have is the one mistake the next cycle cannot undo.
    night = datetime(2026, 8, 15, 3, 0, tzinfo=timezone.utc)
    assert _due(night, soc=None) == (False, False)


def test_nothing_to_shed_is_due_immediately():
    # The pack is already below the reserve, so the drop costs nothing — and
    # holding at the destination would invite the inverter to charge up to it.
    evening = datetime(2026, 8, 14, 20, 0, tzinfo=timezone.utc)
    assert _due(evening, soc=70.0) == (True, True)


def test_an_unusable_estimate_drops_immediately():
    # No base consumption to divide by: fall back to the plain behaviour of
    # applying the reservation as soon as it is known.
    evening = datetime(2026, 8, 14, 20, 0, tzinfo=timezone.utc)
    assert _due(evening, base=0.0) == (True, True)
    assert reservation_is_due(
        evening, PRODUCTION_AT, 95.0, RESERVED, 0.0, BASE_W
    ) == (True, True)


def test_production_under_way_is_due_and_latches_nothing():
    # Daylight: no night to be part-way through, so the reservation applies and
    # the latch is left clean for the coming dusk.
    noon = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)
    assert reservation_is_due(noon, noon, 95.0, RESERVED, CAPACITY, BASE_W) == (
        True,
        False,
    )
    # Same for a forecast that offers no crossing to schedule against.
    assert reservation_is_due(
        noon, None, 95.0, RESERVED, CAPACITY, BASE_W, was_due=True
    ) == (True, False)


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


# --- The destination anchor: the reserve is carved below the user's ceiling ----
#
# The battery's *destination* is where it would have ended the day anyway (the
# per-inverter "normal SOC ceiling source" entity). Carving the reserve out of
# 100 % on a site whose ceiling sits at 95 % reserves that top 5 % twice: the
# battery is allowed up to 100 − reserve, hits its owner's 95 % first, and meets
# the peak with 5 % of room instead of the reserve.

def test_max_soc_carves_the_reserve_below_the_destination():
    # The maintainer's site: 20 kWh pack heading for 95 %, 2 kWh clippable →
    # hold at 85, absorb the clip through the peak, arrive at 95.
    assert battery_max_soc(2.0, 20.0, soc_floor=30.0, soc_target=95.0) == 85.0


def test_max_soc_with_no_destination_is_the_old_formula_exactly():
    """A site that configures no ceiling source must not move at all.

    Pinned term-for-term against the pre-destination expression
    ``min(100, max(floor, 100 − absorbable/capacity × 100))``, both by omitting
    the argument and by passing the 100 % default explicitly.
    """
    for absorbable, capacity, floor in [
        (0.0, 10.0, 30.0),
        (1.0, 10.0, 30.0),
        (4.0, 10.0, 30.0),
        (9.5, 10.0, 30.0),
        (20.0, 10.0, 30.0),
        (2.0, 20.0, 50.0),
        (0.3, 13.5, 10.0),
    ]:
        needed = min(max(0.0, absorbable), capacity)
        old = min(100.0, max(floor, 100.0 - needed / capacity * 100.0))
        assert battery_max_soc(absorbable, capacity, soc_floor=floor) == old
        assert (
            battery_max_soc(absorbable, capacity, soc_floor=floor, soc_target=100.0)
            == old
        )


def test_max_soc_with_nothing_to_absorb_is_the_destination():
    # Not 100: with nothing to clip the advice is exactly where the owner was
    # sending the battery anyway, so the fan-out's min() writes their number.
    assert battery_max_soc(0.0, 20.0, soc_floor=30.0, soc_target=95.0) == 95.0


def test_max_soc_floor_wins_over_the_destination_reserve():
    # 8 kWh of a 10 kWh pack below a 90 % destination is −10 %: the floor holds.
    assert battery_max_soc(8.0, 10.0, soc_floor=30.0, soc_target=90.0) == 30.0
    # And the floor is what binds, not the destination arithmetic: one kWh less
    # still lands on the floor, but 5 kWh clears it.
    assert battery_max_soc(7.0, 10.0, soc_floor=30.0, soc_target=90.0) == 30.0
    assert battery_max_soc(5.0, 10.0, soc_floor=30.0, soc_target=90.0) == 40.0


def test_max_soc_destination_below_the_floor_still_respects_the_floor():
    # A destination under the floor cannot lower the advice below it. The
    # recommendation then sits ABOVE the destination, which the ceiling
    # fan-out's min() resolves in the user's favour (see engine/hub_result.py).
    assert battery_max_soc(1.0, 10.0, soc_floor=50.0, soc_target=40.0) == 50.0


def test_max_soc_destination_heals_toward_the_destination_not_a_hundred():
    # Fixed series, advancing now: as the remaining clip burns down the ceiling
    # rises to the DESTINATION and stops there — the band above it is the
    # site's buffer against a forecast under-read, never advice.
    series = _series([8000, 10000, 8000, 0])
    previous = -1.0
    for hours in range(0, 6):
        fc = _fc(series, now=T0 + timedelta(hours=hours),
                 until=T0 + timedelta(hours=18))
        ceiling = battery_max_soc(
            fc.absorbable_kwh, 10.0, soc_floor=30.0, soc_target=95.0
        )
        assert ceiling >= previous
        assert ceiling <= 95.0
        previous = ceiling
    assert previous == 95.0


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
    # BELOW the destination (at_destination False, the default) — above it the
    # standing ceiling holds whatever the forecast says.
    assert recommended_charge_limit(
        0.0, 90.0, 100.0, 5000.0, 0.0, THRESHOLD, 2.0, at_destination=False
    ) == (5000.0, False)


def test_cap_released_immediately_when_nothing_left_to_clip():
    # Even while engaged: below the destination, with nothing left to protect,
    # the latch drops at once.
    assert recommended_charge_limit(
        0.0, 100.0, 100.0, 5000.0, 0.0, THRESHOLD, 2.0, True, at_destination=False
    ) == (5000.0, False)


def test_cap_released_below_the_band():
    # SOC 50 against a 60 % ceiling with 2 % hysteresis: headroom not at risk.
    assert recommended_charge_limit(
        4.0, 50.0, 60.0, 5000.0, 10000.0, THRESHOLD, 2.0
    ) == (5000.0, False)


def test_cap_restricts_to_unexportable_power_at_the_ceiling():
    # At the ceiling, charge only with power that could not have been exported.
    assert recommended_charge_limit(
        4.0, 60.0, 60.0, 5000.0, 8000.0, THRESHOLD, 2.0
    ) == (2000.0, True)


def test_cap_zero_at_ceiling_with_exportable_production():
    # Production below the threshold is exportable — charging would spend the
    # reserved headroom, so 0 is the correct setpoint here.
    assert recommended_charge_limit(
        4.0, 60.0, 60.0, 5000.0, 4000.0, THRESHOLD, 2.0
    ) == (0.0, True)


def test_cap_never_exceeds_full_rate():
    assert recommended_charge_limit(
        4.0, 60.0, 60.0, 5000.0, 20000.0, THRESHOLD, 2.0
    ) == (5000.0, True)


def test_cap_unknown_soc_protects_the_headroom():
    # No SOC to judge by: keep protecting, and report the latch as engaged so
    # the next cycle (SOC back) starts from "limiting" rather than re-engaging.
    assert recommended_charge_limit(
        4.0, None, 60.0, 5000.0, 8000.0, THRESHOLD, 2.0
    ) == (2000.0, True)


# --- The charge-cap SOC latch: two thresholds, not one boundary ---------------


def test_cap_latch_holds_through_an_integer_soc_flap():
    """Regression: the live flap of 2026-08-23 08:44–10:48 UTC.

    An integer SOC sat on the single old boundary (ceiling 100, hysteresis 2 →
    98) while partly-cloudy sun ticked it 97↔98. Each tick flipped the gate,
    and each flip was a Modbus/EEPROM write to the Deye register: ~12
    engage/release cycles in two hours. The feedback is structural — the cap
    suppresses the very charging that raised SOC over the boundary — so only a
    real band can break it.

    Solar alternates 2400/550 W, both below the 6000 W threshold, so while
    engaged the cap is 0 W (the value that was being written and un-written).
    """
    limiting = False
    limit, limiting = recommended_charge_limit(
        4.0, 98.0, 100.0, 10000.0, 2400.0, THRESHOLD, 2.0, limiting
    )
    assert (limit, limiting) == (0.0, True), "SOC 98 must engage the cap"

    # Two hours of the observed alternation. Not one release, and the setpoint
    # never returns to full rate — nothing to pace, nothing to write.
    for i, (soc, solar) in enumerate([(97.0, 550.0), (98.0, 2400.0)] * 12):
        limit, limiting = recommended_charge_limit(
            4.0, soc, 100.0, 10000.0, solar, THRESHOLD, 2.0, limiting
        )
        assert limiting is True, f"cycle {i}: cap released at SOC {soc}"
        assert limit == 0.0, f"cycle {i}: cap jumped to {limit} W at SOC {soc}"


def test_cap_latch_releases_a_full_band_below_the_engage_threshold():
    # Engaged at ceiling 100 with hysteresis 2: engage boundary 98, release
    # below 96. 96 still holds; 95 lets go.
    limit, limiting = recommended_charge_limit(
        4.0, 96.0, 100.0, 10000.0, 550.0, THRESHOLD, 2.0, True
    )
    assert (limit, limiting) == (0.0, True)

    limit, limiting = recommended_charge_limit(
        4.0, 95.0, 100.0, 10000.0, 550.0, THRESHOLD, 2.0, True
    )
    assert (limit, limiting) == (10000.0, False)


def test_cap_latch_does_not_re_engage_below_the_engage_threshold():
    # Released at 95, the gate stays open until SOC is back at 98 — an integer
    # tick at the release threshold cannot flip it either.
    limit, limiting = recommended_charge_limit(
        4.0, 96.0, 100.0, 10000.0, 550.0, THRESHOLD, 2.0, False
    )
    assert (limit, limiting) == (10000.0, False)
    limit, limiting = recommended_charge_limit(
        4.0, 97.0, 100.0, 10000.0, 550.0, THRESHOLD, 2.0, False
    )
    assert (limit, limiting) == (10000.0, False)
    limit, limiting = recommended_charge_limit(
        4.0, 98.0, 100.0, 10000.0, 550.0, THRESHOLD, 2.0, False
    )
    assert (limit, limiting) == (0.0, True)


def test_cap_latch_follows_a_moving_ceiling():
    # A forecast refresh lowers the ceiling to 90 while the cap is engaged at
    # SOC 96: the same rule against the new ceiling keeps it engaged (96 >= 86).
    # Raising the ceiling to 100 with SOC at 95 releases it (95 < 96).
    assert recommended_charge_limit(
        4.0, 96.0, 90.0, 10000.0, 550.0, THRESHOLD, 2.0, True
    ) == (0.0, True)
    assert recommended_charge_limit(
        4.0, 95.0, 100.0, 10000.0, 550.0, THRESHOLD, 2.0, True
    ) == (10000.0, False)


# --- The advice anchor: a hard-limiting inverter must not mask the signal ----
#
# The maintainer's live site: a Deye hybrid that HARD-enforces the export limit
# by curtailing its own PV. Site power balance is
#     production = house + battery + export
# so with export clamped at the limit, measured production can never exceed
#     export_limit + house + battery_allowance.
# Anchored at the true clipping threshold (limit + base) the advice is then
# battery_allowance + (house − base) — its own previous output — and freezes.
# Anchored one Excess trigger margin lower it self-creeps out instead.

_LIMIT = 8700.0        # the site's export limit, W
_MARGIN = 500.0        # Excess trigger margin (DEFAULT_EXCESS_TRIGGER_MARGIN)
_BASE = 300.0          # base consumption
_HOUSE = 300.0         # actual house draw right now
_FULL_RATE = 10000.0   # battery charge rating
_POTENTIAL = 15000.0   # what the array could make if nothing curtailed it

_ANCHOR = _LIMIT - _MARGIN + _BASE   # 8500 — the instantaneous advice anchor
_TRUE_THRESHOLD = _LIMIT + _BASE     # 9000 — what the forecast integral uses


def _hard_limited_production(allowance):
    """What the site MEASURES while the inverter enforces the export limit."""
    return min(_POTENTIAL, _LIMIT + _HOUSE + allowance)


def _next_allowance(allowance, threshold):
    """One closed-loop cycle: measure, advise, apply as the next allowance.

    SOC pinned at the ceiling and 4 kWh still to clip, so the latch is engaged
    throughout — this test is about the anchor, not the gate.
    """
    limit, limiting = recommended_charge_limit(
        4.0,
        100.0,
        100.0,
        _FULL_RATE,
        _hard_limited_production(allowance),
        threshold,
        2.0,
        True,
    )
    assert limiting is True
    return limit


def test_masked_site_replay_at_the_true_threshold_is_a_fixed_point():
    """The bug, as arithmetic: anchored at the true threshold the loop is stuck.

    Every cycle returns exactly what it was handed, so the allowance sits at
    its floor for ever while the inverter curtails 5 kW of real production.
    """
    allowance = 1000.0
    for _ in range(20):
        allowance = _next_allowance(allowance, _TRUE_THRESHOLD)
        assert allowance == 1000.0
    # And the site is genuinely clipping the whole time: 10 kW measured of
    # 15 kW available.
    assert _hard_limited_production(allowance) == 10000.0


def test_masked_site_replay_self_creeps_off_the_hard_limit():
    """The fix, as arithmetic: the shifted anchor escapes the masking.

    Closed-loop replay of the masked site. While export is pinned at the limit
    each cycle hands back allowance + margin, so the allowance climbs by
    exactly one margin per step until measured production reaches the array's
    true potential — then one more step lands on the equilibrium and stays.
    """
    allowance = 1000.0
    trajectory = [allowance]
    for _ in range(11):
        allowance = _next_allowance(allowance, _ANCHOR)
        trajectory.append(allowance)

    # Ten pinned cycles of exactly +500 W, then the equilibrium value.
    assert trajectory == [
        1000.0, 1500.0, 2000.0, 2500.0, 3000.0, 3500.0,
        4000.0, 4500.0, 5000.0, 5500.0, 6000.0, 6500.0,
    ]
    # Monotone throughout — the allowance never falls back into the masked state.
    assert all(b >= a for a, b in zip(trajectory, trajectory[1:]))

    # Export unpinned: measured production is the array's true potential, so
    # the inverter is no longer curtailing anything.
    assert _hard_limited_production(allowance) == _POTENTIAL

    # Fixed point, and it is the equilibrium: the battery absorbs
    # potential − house − (limit − margin) and export rides a margin under the
    # hard limit instead of on it.
    for _ in range(10):
        allowance = _next_allowance(allowance, _ANCHOR)
        assert allowance == 6500.0
    assert allowance == _POTENTIAL - _HOUSE - (_LIMIT - _MARGIN)
    export = _POTENTIAL - _HOUSE - allowance
    assert export == _LIMIT - _MARGIN == 8200.0


def test_unpinned_equilibrium_puts_export_a_margin_under_the_limit():
    """Regime 1 as a direct assertion, for any house draw.

    Unpinned, the advice is potential − anchor, the battery takes it, and what
    is left leaves through the meter: export = (limit − margin) + (base −
    house). At base draw that is exactly a margin below the limit — never on
    it, which is the whole point.
    """
    potential = 14000.0
    allowance, _ = recommended_charge_limit(
        4.0, 100.0, 100.0, _FULL_RATE, potential, _ANCHOR, 2.0, True
    )
    assert allowance == potential - _ANCHOR
    for house in (0.0, _BASE, 1200.0):
        export = potential - house - allowance
        assert export == (_LIMIT - _MARGIN) + (_BASE - house)
        # Never at the hard limit, so nothing is curtailed and the measured
        # production stays honest: the margin is the whole safety distance,
        # and only a house drawing less than base could eat into it.
        assert export < _LIMIT


# --- The integral trim: base consumption stops being a permanent error --------
#
# The anchored advice is feedforward, and its standing error is that
# ``base_consumption`` stands in for the house: the export equilibrium inherits
# ``(base − house)``, so a base 200 W low rides 200 W under (limit − margin) for
# ever and delays every Excess load. The trim integrates RECONSTRUCTED export
# against that setpoint, slowly and within a hard clamp.

_SETPOINT = _LIMIT - _MARGIN   # 8200 — the export the advice is aiming for


def test_trim_holds_still_when_export_sits_on_the_setpoint():
    assert export_trim(0.0, _SETPOINT, _SETPOINT, 10.0) == 0.0
    assert export_trim(-200.0, _SETPOINT, _SETPOINT, 10.0) == -200.0


def test_trim_follows_the_error_downward_and_upward():
    # Export 200 W under the setpoint means the battery is taking too much, so
    # the trim goes NEGATIVE and the advice comes down. Over the setpoint, up.
    assert export_trim(0.0, _SETPOINT - 200.0, _SETPOINT, 60.0) < 0.0
    assert export_trim(0.0, _SETPOINT + 200.0, _SETPOINT, 60.0) > 0.0


def test_trim_moves_slowly_enough_to_ignore_a_kettle():
    # One 10 s cycle of a 2 kW error: 2000 × 10/600 = 33 W. Six of them — a
    # kettle for a minute — is 200 W, which the charge control's write deadband
    # (5 % of the register's normal) absorbs; see the register assertion in
    # test_a_kettle_cannot_move_the_written_register below.
    step = export_trim(0.0, _SETPOINT - 2000.0, _SETPOINT, 10.0)
    assert abs(step + 2000.0 * 10.0 / FORECAST_TRIM_TAU_S) < 1e-9
    trim = 0.0
    for _ in range(6):
        trim = export_trim(trim, _SETPOINT - 2000.0, _SETPOINT, 10.0)
    assert abs(trim + 200.0) < 1e-6


def test_trim_is_hard_clamped_in_both_directions():
    # Windup is impossible by construction: hours of a 5 kW error stop here.
    trim = 0.0
    for _ in range(200):
        trim = export_trim(trim, 0.0, _SETPOINT, 60.0)
    assert trim == -FORECAST_TRIM_CLAMP_W
    for _ in range(200):
        trim = export_trim(trim, 30000.0, _SETPOINT, 60.0)
    assert trim == FORECAST_TRIM_CLAMP_W


def test_trim_caps_the_interval_a_single_step_may_integrate():
    # A stalled and resumed coordinator hands over an hour of elapsed time; only
    # FORECAST_TRIM_MAX_STEP_S of it is integrated.
    long_gap = export_trim(0.0, _SETPOINT - 600.0, _SETPOINT, 3600.0)
    capped = export_trim(0.0, _SETPOINT - 600.0, _SETPOINT, FORECAST_TRIM_MAX_STEP_S)
    assert long_gap == capped
    assert export_trim(0.0, 0.0, _SETPOINT, 0.0) == 0.0


def test_trim_only_touches_the_engaged_advice():
    # Engaged: the trim shifts the setpoint. Released: full rate is full rate,
    # whatever the trim happens to hold.
    engaged, limiting = recommended_charge_limit(
        4.0, 100.0, 100.0, _FULL_RATE, 14000.0, _ANCHOR, 2.0, True, trim_w=-200.0
    )
    assert (engaged, limiting) == (14000.0 - _ANCHOR - 200.0, True)
    released = recommended_charge_limit(
        4.0, 50.0, 100.0, _FULL_RATE, 14000.0, _ANCHOR, 2.0, False, trim_w=-200.0
    )
    assert released == (_FULL_RATE, False)
    # And it can never push the setpoint below zero or above the rating.
    assert recommended_charge_limit(
        4.0, 100.0, 100.0, _FULL_RATE, 8600.0, _ANCHOR, 2.0, True, trim_w=-400.0
    ) == (0.0, True)
    assert recommended_charge_limit(
        4.0, 100.0, 100.0, _FULL_RATE, 30000.0, _ANCHOR, 2.0, True, trim_w=400.0
    ) == (_FULL_RATE, True)


def _tracking_export(trim, house):
    """One unpinned tracking cycle: advise, let the battery take it, measure.

    Production is the array's potential (export well under the hard limit, so
    nothing is curtailed and the reading is honest), the battery absorbs the
    advice, and what is left leaves through the meter.
    """
    advice, limiting = recommended_charge_limit(
        4.0, 100.0, 100.0, _FULL_RATE, _POTENTIAL, _ANCHOR, 2.0, True, trim_w=trim
    )
    assert limiting is True
    return advice, _POTENTIAL - house - advice


def test_a_misconfigured_base_is_a_permanent_export_offset_without_the_trim():
    """The bug the trim exists for: base 300 against a 500 W house.

    Feedforward alone, the equilibrium is exactly (limit − margin) + (base −
    house) — 200 W under the setpoint, for ever, delaying every Excess load.
    """
    _, export = _tracking_export(0.0, house=500.0)
    assert export == _SETPOINT - 200.0


def test_the_trim_converges_the_export_onto_the_setpoint():
    """The same site with the trim: the offset is corrected, on the clock.

    Ten-second cycles, house 500 W against a 300 W base. One time constant
    closes ~63 % of the 200 W error, two ~86 %, and by 40 minutes it is gone.
    The trim itself converges to (base − house) = −200 W, which is precisely the
    feedforward's standing error — the point of the whole mechanism.
    """
    trim = 0.0
    exports = {}
    for cycle in range(1, 241):  # 40 minutes of 10 s cycles
        _, export = _tracking_export(trim, house=500.0)
        trim = export_trim(trim, export, _SETPOINT, 10.0)
        exports[cycle * 10] = export

    assert abs(exports[600] - (_SETPOINT - 200.0 * 0.37)) < 3.0    # 1 τ: ~63 %
    assert abs(exports[1200] - (_SETPOINT - 200.0 * 0.135)) < 3.0  # 2 τ: ~86 %
    assert abs(exports[2400] - _SETPOINT) < 5.0                    # gone
    assert abs(trim - (-200.0)) < 5.0
    # Monotone approach — an integral this slow cannot overshoot the setpoint.
    ordered = [exports[t] for t in sorted(exports)]
    assert all(b >= a for a, b in zip(ordered, ordered[1:]))
    assert max(ordered) <= _SETPOINT


def test_a_cloud_drops_the_advice_to_the_floor_whatever_the_trim_holds():
    """Production collapse is the feedforward's own response, not the trim's.

    Even holding its full positive clamp, the trim cannot keep the battery
    charging through a cloud: the advice is the overshoot plus at most a few
    hundred watts, so a collapse takes it to the floor within one cycle.
    """
    sunny, _ = recommended_charge_limit(
        4.0, 100.0, 100.0, _FULL_RATE, _POTENTIAL, _ANCHOR, 2.0, True,
        trim_w=FORECAST_TRIM_CLAMP_W,
    )
    assert sunny == _POTENTIAL - _ANCHOR + FORECAST_TRIM_CLAMP_W
    for production in (2000.0, 500.0, 0.0):
        clouded, limiting = recommended_charge_limit(
            4.0, 100.0, 100.0, _FULL_RATE, production, _ANCHOR, 2.0, True,
            trim_w=FORECAST_TRIM_CLAMP_W,
        )
        assert limiting is True
        assert clouded <= FORECAST_TRIM_CLAMP_W
    # And the clamp is a hard bound on how far the trim could ever hold it up,
    # however long the cloud lasts — see the freeze rule in
    # engine/hub_result._advance_export_trim, which stops integrating there.
    assert export_trim(FORECAST_TRIM_CLAMP_W, 0.0, _SETPOINT, 3600.0) < 0.0


def test_masked_site_replay_still_self_creeps_with_the_trim_active():
    """The 91aa5ed property survives the trim: the masked site still escapes.

    Closed-loop replay of the hard-limiting inverter, now with the trim running
    on the reconstructed export the site would read. While export is pinned AT
    the limit the trim is being pushed UP (export is a margin ABOVE the
    setpoint), so it can only help the creep; once export falls off the limit
    the sign reverses and the trim decays back toward zero, because here the
    house draws exactly the configured base and there is no standing error to
    correct. The equilibrium is the same one as without the trim.
    """
    allowance = 1000.0
    trim = 0.0
    trajectory = [allowance]
    for _ in range(11):
        production = _hard_limited_production(allowance)
        export = production - _HOUSE - allowance
        allowance, limiting = recommended_charge_limit(
            4.0, 100.0, 100.0, _FULL_RATE, production, _ANCHOR, 2.0, True,
            trim_w=trim,
        )
        assert limiting is True
        trim = export_trim(trim, export, _SETPOINT, 10.0)
        trajectory.append(allowance)

    # Monotone, and off the hard limit no slower than before: the pinned cycles
    # still gain at least one margin each.
    assert all(b >= a for a, b in zip(trajectory, trajectory[1:]))
    assert trajectory[-1] >= 6500.0
    assert _hard_limited_production(trajectory[-1]) == _POTENTIAL

    # Unpinned, it settles on the same equilibrium the untrimmed loop found.
    for _ in range(400):
        production = _hard_limited_production(allowance)
        export = production - _HOUSE - allowance
        allowance, _ = recommended_charge_limit(
            4.0, 100.0, 100.0, _FULL_RATE, production, _ANCHOR, 2.0, True,
            trim_w=trim,
        )
        trim = export_trim(trim, export, _SETPOINT, 10.0)
    assert abs(allowance - 6500.0) < 5.0
    assert abs(trim) < 5.0


# --- Above the destination: the battery is the absorber of last resort --------

def test_yield_engages_at_the_destination_and_releases_a_band_below_it():
    # Not a percent early: below the destination the battery is served first.
    assert yields_to_excess(94.0, 95.0, 2.0, False) is False
    assert yields_to_excess(95.0, 95.0, 2.0, False) is True
    # Engaged, it holds through an integer tick and releases a full band below.
    assert yields_to_excess(94.0, 95.0, 2.0, True) is True
    assert yields_to_excess(93.0, 95.0, 2.0, True) is True
    assert yields_to_excess(92.0, 95.0, 2.0, True) is False


def test_yield_latch_holds_through_an_integer_soc_flap_at_the_destination():
    # The crossing decides a step of kilowatts in the advice, so an SOC register
    # ticking 94↔95 must not flip it — the same failure the charge gate's latch
    # was built for, at a different boundary.
    yielding = yields_to_excess(95.0, 95.0, 2.0, False)
    assert yielding is True
    for soc in [94.0, 95.0] * 6:
        yielding = yields_to_excess(soc, 95.0, 2.0, yielding)
        assert yielding is True, f"yield released at SOC {soc}"


def test_yield_is_off_without_an_soc_or_a_destination():
    assert yields_to_excess(None, 95.0, 2.0, True) is False
    assert yields_to_excess(99.0, None, 2.0, True) is False


def test_an_engaged_excess_load_displaces_the_battery_above_the_destination():
    # Overshoot 5500 W with a 3 kW Excess EVSE running: the battery takes the
    # 2500 W the car cannot, watt for watt.
    advice, limiting = recommended_charge_limit(
        4.0, 96.0, 90.0, _FULL_RATE, _POTENTIAL, _ANCHOR, 2.0, True,
        excess_draw_w=3000.0, at_destination=True,
    )
    assert (advice, limiting) == (_POTENTIAL - _ANCHOR - 3000.0, True)
    # A load drawing more than the overshoot floors the advice rather than
    # going negative.
    assert recommended_charge_limit(
        4.0, 96.0, 90.0, _FULL_RATE, _POTENTIAL, _ANCHOR, 2.0, True,
        excess_draw_w=9000.0, at_destination=True,
    ) == (0.0, True)


def test_with_nothing_engaged_the_battery_takes_the_whole_overshoot():
    # Above the destination and no Excess load able to absorb: unchanged from
    # before this rule — the battery keeps buffering toward 100 %.
    assert recommended_charge_limit(
        4.0, 96.0, 90.0, _FULL_RATE, _POTENTIAL, _ANCHOR, 2.0, True,
        excess_draw_w=0.0, at_destination=True,
    ) == (_POTENTIAL - _ANCHOR, True)


def test_below_the_destination_the_battery_is_served_first():
    """The caller passes no draw at all below the destination (yielding False),
    so the advice is the plain overshoot even with a car on the surplus."""
    assert yields_to_excess(88.0, 95.0, 2.0, False) is False
    assert recommended_charge_limit(
        4.0, 88.0, 90.0, _FULL_RATE, _POTENTIAL, _ANCHOR, 2.0, True,
        excess_draw_w=0.0, at_destination=False,
    ) == (_POTENTIAL - _ANCHOR, True)


# --- The destination as a STANDING ceiling, clip or no clip -------------------
#
# The live bug of 2026-08-25: ``absorbable_kwh <= 0`` was tested FIRST, so on a
# site with a 95 % destination and nothing forecast to clip the battery crossed
# 95 at 08:55 UTC and ran to 98 at the BMS's own 80 A with the advice at full
# rate. The destination gate now applies regardless of the clip.

_DEST = 95.0            # the site's normal SOC ceiling — where the pack heads
_HYST = 2.0             # FORECAST_SOC_HYSTERESIS
_BMS_RATE = 4096.0      # 80 A × 51.2 V — what the pack takes when unmanaged


def test_the_destination_holds_with_nothing_forecast_to_clip():
    """The bug's own case: absorbable 0, production under the anchor, SOC at the
    destination. Engaged, and the advice is the floor — 0 W of overshoot."""
    assert recommended_charge_limit(
        0.0, _DEST, _DEST, _BMS_RATE, 3000.0, _ANCHOR, _HYST, False,
        at_destination=True,
    ) == (0.0, True)


def test_the_hold_admits_the_overshoot_a_better_day_actually_makes():
    """The very case the buffer exists for: the forecast under-read the day.

    Nothing was reserved (absorbable 0), the pack is parked at 95 — and
    production beats the anchor anyway. Those watts cannot be exported, so the
    battery climbs above the destination on the overshoot alone.
    """
    assert recommended_charge_limit(
        0.0, _DEST, _DEST, _BMS_RATE, _ANCHOR + 1500.0, _ANCHOR, _HYST, True,
        at_destination=True,
    ) == (1500.0, True)
    # And it is still only the overshoot: never the full rate the bug handed it.
    assert recommended_charge_limit(
        0.0, _DEST, _DEST, _BMS_RATE, _ANCHOR + 99000.0, _ANCHOR, _HYST, True,
        at_destination=True,
    ) == (_BMS_RATE, True)


def test_an_excess_load_displaces_the_parked_battery_with_no_clip_either():
    # The engaged formula is the SAME one, so the watt-for-watt displacement
    # holds on a day that reserves nothing.
    assert recommended_charge_limit(
        0.0, 96.0, _DEST, _BMS_RATE, _ANCHOR + 2500.0, _ANCHOR, _HYST, True,
        excess_draw_w=2300.0, at_destination=True,
    ) == (200.0, True)


def test_below_the_destination_nothing_to_clip_is_still_full_rate():
    """The cloudy-day protection, preserved exactly where it is correct: under
    the ceiling with no reserve to keep, the pack refills at full rate."""
    for soc in (0.0, 50.0, _DEST - _HYST, _DEST - 0.5):
        assert recommended_charge_limit(
            0.0, soc, _DEST, _BMS_RATE, 3000.0, _ANCHOR, _HYST, False,
            at_destination=False,
        ) == (_BMS_RATE, False), f"held at SOC {soc}"


def test_the_live_event_replay_holds_at_the_destination_and_releases_below_it():
    """Destination 95, a 20 kWh pack, nothing forecast to clip, production under
    the export limit — the maintainer's morning, replayed.

    Full rate up to the crossing, the floor from 95 on with the latch engaged,
    and a release only a full hysteresis band below the destination — where full
    rate is right again, because that is under the ceiling.
    """
    yielding = False
    limiting = False
    seen = []
    for soc in (93.0, 94.0, 95.0, 96.0, 97.0):
        yielding = yields_to_excess(soc, _DEST, _HYST, yielding)
        limit, limiting = recommended_charge_limit(
            0.0, soc, _DEST, _BMS_RATE, 3000.0, _ANCHOR, _HYST, limiting,
            excess_draw_w=0.0, at_destination=yielding,
        )
        seen.append((soc, limit, limiting))

    assert seen == [
        (93.0, _BMS_RATE, False),   # below the destination: refill
        (94.0, _BMS_RATE, False),
        (95.0, 0.0, True),          # the crossing — this is what ran to 98 %
        (96.0, 0.0, True),
        (97.0, 0.0, True),
    ]

    # The evening: the house takes the pack back down. A tick inside the band
    # still holds (the latch is the whole point), a full band below releases.
    for soc, expected in ((94.0, 0.0), (93.0, 0.0), (92.0, _BMS_RATE)):
        yielding = yields_to_excess(soc, _DEST, _HYST, yielding)
        limit, limiting = recommended_charge_limit(
            0.0, soc, _DEST, _BMS_RATE, 3000.0, _ANCHOR, _HYST, limiting,
            at_destination=yielding,
        )
        assert limit == expected, f"SOC {soc} gave {limit} W"
    assert limiting is False


def test_a_clip_appearing_while_parked_hands_over_to_the_reservation():
    """Two engagement sources, one latch state, no step in the advice.

    Parked at the destination with nothing to clip, then a forecast refresh
    reserves 2 kWh of a 20 kWh pack — the ceiling drops to 85. The hold was
    already engaged, so the reservation simply takes over: same formula, same
    latch, and the release the pack then falls to is the reservation's own
    (85 − 2 × 2 = 81), not the destination's.
    """
    held = recommended_charge_limit(
        0.0, _DEST, _DEST, _BMS_RATE, 3000.0, _ANCHOR, _HYST, False,
        at_destination=True,
    )
    assert held == (0.0, True)

    # The clip appears while the pack sits at 95 — still at the destination, so
    # nothing moves.
    limit, limiting = recommended_charge_limit(
        2.0, _DEST, 85.0, _BMS_RATE, 3000.0, _ANCHOR, _HYST, held[1],
        at_destination=True,
    )
    assert (limit, limiting) == (0.0, True)

    # The pack is discharged toward the reserve: the destination hold lets go at
    # 93, and the reservation holds it there instead of releasing to full rate.
    yielding = yields_to_excess(92.0, _DEST, _HYST, True)
    assert yielding is False
    limit, limiting = recommended_charge_limit(
        2.0, 92.0, 85.0, _BMS_RATE, 3000.0, _ANCHOR, _HYST, limiting,
        at_destination=yielding,
    )
    assert (limit, limiting) == (0.0, True), "the handover stepped to full rate"

    # It releases where the reservation says, a full band below its own ceiling.
    assert recommended_charge_limit(
        2.0, 80.0, 85.0, _BMS_RATE, 3000.0, _ANCHOR, _HYST, limiting,
        at_destination=False,
    ) == (_BMS_RATE, False)


def test_an_unknown_soc_keeps_the_reservations_ordering():
    """No reading is no destination crossing to detect — ``yields_to_excess`` is
    False without an SOC — so the unknown case sits where it always sat.

    Nothing to clip: full rate, because a dead SOC sensor must not strand the
    pack on the floor for the rest of the day. A reservation at risk: protected,
    exactly as before.
    """
    assert yields_to_excess(None, _DEST, _HYST, True) is False
    assert recommended_charge_limit(
        0.0, None, _DEST, _BMS_RATE, 3000.0, _ANCHOR, _HYST, True,
        at_destination=False,
    ) == (_BMS_RATE, False)
    assert recommended_charge_limit(
        4.0, None, 85.0, _BMS_RATE, _ANCHOR + 1000.0, _ANCHOR, _HYST, False,
        at_destination=False,
    ) == (1000.0, True)


def test_a_site_with_no_ceiling_source_is_unchanged():
    """Destination 100 (no ceiling source configured anywhere): the gate can
    only engage at SOC 100, so nothing below it moves at all.

    Byte-equivalence against the old early return across the SOC range, with
    nothing to clip — the one case the reorder could have disturbed.
    """
    for soc in range(0, 100):
        yielding = yields_to_excess(float(soc), 100.0, _HYST, False)
        assert yielding is False, f"SOC {soc} yielded against a 100 % ceiling"
        assert recommended_charge_limit(
            0.0, float(soc), 100.0, _BMS_RATE, 3000.0, _ANCHOR, _HYST, False,
            at_destination=yielding,
        ) == (_BMS_RATE, False), f"SOC {soc} was held"
    # At 100 the pack IS at its destination, and holding a full battery on the
    # floor is what "standing ceiling" means — it cannot charge either way.
    assert yields_to_excess(100.0, 100.0, _HYST, False) is True
    assert recommended_charge_limit(
        0.0, 100.0, 100.0, _BMS_RATE, 3000.0, _ANCHOR, _HYST, False,
        at_destination=True,
    ) == (0.0, True)


def test_unexportable_power_clamps_at_zero():
    assert unexportable_power(4000.0, THRESHOLD) == 0.0
    assert unexportable_power(8000.0, THRESHOLD) == 2000.0
    assert unexportable_power(None, THRESHOLD) == 0.0
