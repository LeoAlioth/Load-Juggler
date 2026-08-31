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
    battery_max_soc,
    clipping_forecast,
    first_production_at,
    headroom_deficit_kwh,
    hours_to_shed,
    merge_forecast_series,
    recommended_charge_limit,
    reservation_is_due,
    select_clipping_window,
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
#
# The engaged value is memoryless DIRECT FEEDBACK:
#
#     desired = battery_charge_now + (export_now − export_setpoint)
#
# so every engaged assertion below fixes the two live figures and reads the
# arithmetic. ``_SETPOINT`` is watts AT THE METER — the export limit less the
# Excess trigger margin — and there is no production threshold and no base
# consumption anywhere in this path.

_SETPOINT = 4500.0     # export limit 5000 − trigger margin 500
_FULL = 5000.0         # a 5 kW charge rating, for the gate tests


def _at(export_over_setpoint, battery_w=0.0):
    """The (battery, export) pair that asks for ``export_over_setpoint`` watts."""
    return battery_w, _SETPOINT + export_over_setpoint


def test_cap_released_when_nothing_to_clip():
    # BELOW the destination (at_destination False, the default) — above it the
    # standing ceiling holds whatever the forecast says.
    assert recommended_charge_limit(
        0.0, 90.0, 100.0, _FULL, *_at(0.0), _SETPOINT, 2.0, at_destination=False
    ) == (_FULL, False)


def test_cap_released_immediately_when_nothing_left_to_clip():
    # Even while engaged: below the destination, with nothing left to protect,
    # the latch drops at once.
    assert recommended_charge_limit(
        0.0, 100.0, 100.0, _FULL, *_at(0.0), _SETPOINT, 2.0, True,
        at_destination=False,
    ) == (_FULL, False)


def test_cap_released_below_the_band():
    # SOC 50 against a 60 % ceiling with 2 % hysteresis: headroom not at risk.
    assert recommended_charge_limit(
        4.0, 50.0, 60.0, _FULL, *_at(4000.0), _SETPOINT, 2.0
    ) == (_FULL, False)


def test_cap_permits_the_export_error_at_the_ceiling():
    # At the ceiling, permit exactly what the meter says is going out over the
    # setpoint — with the battery taking nothing yet, that IS the whole error.
    assert recommended_charge_limit(
        4.0, 60.0, 60.0, _FULL, *_at(2000.0), _SETPOINT, 2.0
    ) == (2000.0, True)


def test_cap_permits_what_the_battery_already_takes_plus_the_error():
    """The feedback form, in one assertion: the value is not the error alone.

    A pack already absorbing 3 kW with export still 500 W over the setpoint may
    take 3.5 kW — that is the rate at which the meter lands ON the setpoint. Read
    as the error alone it would collapse to 500 W and give the surplus away.
    """
    assert recommended_charge_limit(
        4.0, 60.0, 60.0, _FULL, *_at(500.0, battery_w=3000.0), _SETPOINT, 2.0
    ) == (3500.0, True)


def test_cap_zero_when_export_is_already_on_the_setpoint():
    # Export on the setpoint with the battery taking nothing: there is nothing
    # spare to charge with, so 0 is the correct setpoint here.
    assert recommended_charge_limit(
        4.0, 60.0, 60.0, _FULL, *_at(0.0), _SETPOINT, 2.0
    ) == (0.0, True)


def test_cap_floors_at_zero_when_export_is_under_the_setpoint():
    """Import, or export short of the setpoint: the pack is asked to stop.

    A pack taking 1 kW while the meter sits 3 kW UNDER the setpoint is 2 kW past
    what the site can spare, and the arithmetic asks for a discharge. A charge
    cap cannot force one, so it floors at 0 — no freeze rule, no special case.
    """
    assert recommended_charge_limit(
        4.0, 60.0, 60.0, _FULL, *_at(-3000.0, battery_w=1000.0), _SETPOINT, 2.0
    ) == (0.0, True)


def test_a_discharging_pack_is_a_negative_term_not_a_special_case():
    # Selling 2 kW to the meter while export runs 1 kW over the setpoint: the
    # honest answer is "keep discharging", which a charge cap cannot express, so
    # the clamp lands on 0 rather than on a fabricated permit.
    assert recommended_charge_limit(
        4.0, 60.0, 60.0, _FULL, *_at(1000.0, battery_w=-2000.0), _SETPOINT, 2.0
    ) == (0.0, True)


def test_cap_never_exceeds_full_rate():
    assert recommended_charge_limit(
        4.0, 60.0, 60.0, _FULL, *_at(14000.0), _SETPOINT, 2.0
    ) == (_FULL, True)


def test_cap_unknown_soc_protects_the_headroom():
    # No SOC to judge by: keep protecting, and report the latch as engaged so
    # the next cycle (SOC back) starts from "limiting" rather than re-engaging.
    assert recommended_charge_limit(
        4.0, None, 60.0, _FULL, *_at(2000.0), _SETPOINT, 2.0
    ) == (2000.0, True)


def test_no_battery_power_sensor_degrades_to_the_error_alone():
    """The documented degradation: the caller hands in 0 for an unknown pack.

    The value is then the export error by itself, so a genuine surplus is
    admitted only as fast as the meter shows it — conservative, and never a
    fabricated permit.
    """
    assert recommended_charge_limit(
        4.0, 60.0, 60.0, _FULL, 0.0, _SETPOINT + 800.0, _SETPOINT, 2.0
    ) == (800.0, True)


# --- The charge-cap SOC latch: two thresholds, not one boundary ---------------


def test_cap_latch_holds_through_an_integer_soc_flap():
    """Regression: the live flap of 2026-08-23 08:44–10:48 UTC.

    An integer SOC sat on the single old boundary (ceiling 100, hysteresis 2 →
    98) while partly-cloudy sun ticked it 97↔98. Each tick flipped the gate,
    and each flip was a Modbus/EEPROM write to the Deye register: ~12
    engage/release cycles in two hours. The feedback is structural — the cap
    suppresses the very charging that raised SOC over the boundary — so only a
    real band can break it.

    Export alternates on and just under the setpoint, so while engaged the cap
    is 0 W (the value that was being written and un-written).
    """
    limiting = False
    limit, limiting = recommended_charge_limit(
        4.0, 98.0, 100.0, 10000.0, *_at(0.0), _SETPOINT, 2.0, limiting
    )
    assert (limit, limiting) == (0.0, True), "SOC 98 must engage the cap"

    # Two hours of the observed alternation. Not one release, and the setpoint
    # never returns to full rate — nothing to pace, nothing to write.
    for i, (soc, over) in enumerate([(97.0, -900.0), (98.0, 0.0)] * 12):
        limit, limiting = recommended_charge_limit(
            4.0, soc, 100.0, 10000.0, *_at(over), _SETPOINT, 2.0, limiting
        )
        assert limiting is True, f"cycle {i}: cap released at SOC {soc}"
        assert limit == 0.0, f"cycle {i}: cap jumped to {limit} W at SOC {soc}"


def test_cap_latch_releases_a_full_band_below_the_engage_threshold():
    # Engaged at ceiling 100 with hysteresis 2: engage boundary 98, release
    # below 96. 96 still holds; 95 lets go.
    limit, limiting = recommended_charge_limit(
        4.0, 96.0, 100.0, 10000.0, *_at(-900.0), _SETPOINT, 2.0, True
    )
    assert (limit, limiting) == (0.0, True)

    limit, limiting = recommended_charge_limit(
        4.0, 95.0, 100.0, 10000.0, *_at(-900.0), _SETPOINT, 2.0, True
    )
    assert (limit, limiting) == (10000.0, False)


def test_cap_latch_does_not_re_engage_below_the_engage_threshold():
    # Released at 95, the gate stays open until SOC is back at 98 — an integer
    # tick at the release threshold cannot flip it either.
    limit, limiting = recommended_charge_limit(
        4.0, 96.0, 100.0, 10000.0, *_at(-900.0), _SETPOINT, 2.0, False
    )
    assert (limit, limiting) == (10000.0, False)
    limit, limiting = recommended_charge_limit(
        4.0, 97.0, 100.0, 10000.0, *_at(-900.0), _SETPOINT, 2.0, False
    )
    assert (limit, limiting) == (10000.0, False)
    limit, limiting = recommended_charge_limit(
        4.0, 98.0, 100.0, 10000.0, *_at(-900.0), _SETPOINT, 2.0, False
    )
    assert (limit, limiting) == (0.0, True)


def test_cap_latch_follows_a_moving_ceiling():
    # A forecast refresh lowers the ceiling to 90 while the cap is engaged at
    # SOC 96: the same rule against the new ceiling keeps it engaged (96 >= 86).
    # Raising the ceiling to 100 with SOC at 95 releases it (95 < 96).
    assert recommended_charge_limit(
        4.0, 96.0, 90.0, 10000.0, *_at(-900.0), _SETPOINT, 2.0, True
    ) == (0.0, True)
    assert recommended_charge_limit(
        4.0, 95.0, 100.0, 10000.0, *_at(-900.0), _SETPOINT, 2.0, True
    ) == (10000.0, False)


# --- The closed loop: what the direct feedback settles on ---------------------
#
# The maintainer's live site: a Deye hybrid that HARD-enforces the export limit
# by curtailing its own PV. Site power balance is
#     production = house + battery + export
# so with export clamped at the limit, measured production can never exceed
#     export_limit + house + battery_allowance.
# The design this replaced read PRODUCTION and had to be anchored a margin below
# the true clipping threshold to escape that masking. The feedback form reads the
# METER instead, and the escape is inherent: pinned at the wall the error IS the
# margin, so the permit creeps by one margin per cycle until export falls off the
# limit. These replay both regimes against the real function.

_LIMIT = 8700.0        # the site's export limit, W
_MARGIN = 500.0        # Excess trigger margin (DEFAULT_EXCESS_TRIGGER_MARGIN)
_SP = _LIMIT - _MARGIN  # 8200 — the export setpoint, watts at the meter
_HOUSE = 300.0         # actual house draw right now
_FULL_RATE = 10000.0   # battery charge rating
_POTENTIAL = 15000.0   # what the array could make if nothing curtailed it


def _plant(allowance, house=_HOUSE, potential=_POTENTIAL, limit=_LIMIT):
    """One cycle of the site: what the battery takes, and what the meter reads.

    The inverter hard-enforces ``limit``, so it curtails its own production
    rather than exporting past the wall. Returns ``(battery_w, export_w)``.
    """
    battery = max(0.0, min(allowance, potential - house))
    export = min(limit, potential - house - battery)
    return battery, export


def _next_allowance(allowance, setpoint=_SP, house=_HOUSE,
                    potential=_POTENTIAL, limit=_LIMIT):
    """Measure, advise, and hand the advice back as the next permit.

    SOC pinned at the ceiling and 4 kWh still to clip, so the latch is engaged
    throughout — these are about the value, not the gate.
    """
    battery, export = _plant(allowance, house, potential, limit)
    limit_w, limiting = recommended_charge_limit(
        4.0, 100.0, 100.0, _FULL_RATE, battery, export, setpoint, 2.0, True,
    )
    assert limiting is True
    return limit_w


def test_the_masked_site_self_creeps_off_the_hard_limit():
    """Pinned at the wall, the permit climbs by exactly one margin per cycle.

    The property the shifted anchor used to buy, now a consequence of the
    setpoint sitting a margin under the limit: export pinned at the limit means
    ``error == margin``, so each cycle returns ``battery + margin``.
    """
    allowance = 1000.0
    trajectory = [allowance]
    for _ in range(12):
        allowance = _next_allowance(allowance)
        trajectory.append(allowance)

    # Pinned cycles of exactly +500 W, then the equilibrium.
    assert trajectory[:12] == [
        1000.0, 1500.0, 2000.0, 2500.0, 3000.0, 3500.0,
        4000.0, 4500.0, 5000.0, 5500.0, 6000.0, 6500.0,
    ]
    # Monotone throughout — the permit never falls back into the masked state.
    assert all(b >= a for a, b in zip(trajectory, trajectory[1:]))

    # Export is unpinned once the permit reaches the equilibrium: the array's
    # true potential is measured and nothing is curtailed any more.
    battery, export = _plant(allowance)
    assert battery == _POTENTIAL - _HOUSE - _SP == 6500.0
    assert export == _SP < _LIMIT

    # And it is a fixed point: export ON the setpoint means zero error, so the
    # permit is whatever the battery already takes.
    for _ in range(10):
        allowance = _next_allowance(allowance)
        assert allowance == 6500.0


def test_the_equilibrium_is_the_setpoint_for_any_house_draw():
    """No base consumption anywhere: the loop lands on the setpoint regardless.

    This is the whole reason the integral trim is gone. The old feedforward
    value carried ``(base − house)`` into the equilibrium, so a base 200 W low
    parked export 200 W under the trigger for ever and a trim had to walk it
    back. Here the meter is the input, so every house draw — including the ones
    a base consumption setting would have got wrong by kilowatts — settles on
    the setpoint itself, and in ONE cycle.
    """
    for house in (0.0, 300.0, 1200.0, 4000.0):
        allowance = 0.0
        # From a cold start the site is pinned at the wall, so the climb is the
        # self-creep of one margin per cycle; 40 cycles is well past every case.
        for _ in range(40):
            allowance = _next_allowance(allowance, house=house)
        battery, export = _plant(allowance, house=house)
        assert abs(export - _SP) < 1e-9, f"house {house} settled at {export} W"
        assert battery == _POTENTIAL - house - _SP


def test_a_house_step_is_corrected_on_the_next_cycle():
    """H1, as arithmetic: the setpoint accuracy this controller is chosen for.

    The house jumps 300 → 1300 W under a settled loop. The meter shows the
    kilowatt at once, the next value gives it back, and export is inside 50 W of
    the setpoint one cycle later — with nothing carried and no time constant to
    wait out. (The register's own pacing then decides whether that reduction is
    worth a write; see ``control/inverter.py``, where a step this short-lived is
    exactly what the downward window eats.)

    The recovery is deliberately NOT symmetric. Giving the kilowatt back pins
    export at the wall again, and from there the permit climbs one margin per
    cycle — protection is instant, permission is paced.
    """
    settled = _POTENTIAL - _HOUSE - _SP
    battery, export = _plant(settled, house=1300.0)
    assert export == _SP - 1000.0        # the step, straight onto the meter

    allowance = _next_allowance(settled, house=1300.0)
    battery, export = _plant(allowance, house=1300.0)
    assert abs(export - _SP) <= 50.0
    assert allowance == settled - 1000.0  # exactly the kilowatt, once

    # The kettle goes off: export pins at the limit and the permit creeps back.
    trajectory = [allowance]
    for _ in range(3):
        allowance = _next_allowance(allowance, house=_HOUSE)
        trajectory.append(allowance)
    assert trajectory == [5500.0, 6000.0, 6500.0, 6500.0]
    assert allowance == settled


def test_a_cloud_needs_no_freeze_rule():
    """Production collapses: the value follows the meter down and back up.

    The integral trim needed a conditional-integration rule for this (the
    actuator cannot act, so do not integrate). Memoryless, the cloud is simply
    two different cycles, and nothing survives it to be re-earned afterwards.
    """
    settled = _POTENTIAL - _HOUSE - _SP
    clouded = _next_allowance(settled, potential=800.0)
    assert clouded == 0.0                       # nothing to place, nothing spare

    # The sun returns and the very next cycle is the sunny answer again, with no
    # stale correction on top of it.
    assert _next_allowance(clouded) == _MARGIN  # pinned again, so +one margin
    for _ in range(20):
        settled = _next_allowance(settled)
    assert settled == _POTENTIAL - _HOUSE - _SP


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
    """Surplus 6500 W with a 3 kW Excess EVSE running: the battery takes the
    3500 W the car cannot, watt for watt.

    The draw is subtracted rather than left to the feedback because the
    reconstruction credits it back into the export figure on purpose — without
    this term the battery and the car would both be permitted the same watts.
    """
    battery, export = 0.0, _SP + 6500.0
    advice, limiting = recommended_charge_limit(
        4.0, 96.0, 90.0, _FULL_RATE, battery, export, _SP, 2.0, True,
        excess_draw_w=3000.0, at_destination=True,
    )
    assert (advice, limiting) == (3500.0, True)
    # A load drawing more than the surplus floors the advice rather than going
    # negative.
    assert recommended_charge_limit(
        4.0, 96.0, 90.0, _FULL_RATE, battery, export, _SP, 2.0, True,
        excess_draw_w=9000.0, at_destination=True,
    ) == (0.0, True)


def test_with_nothing_engaged_the_battery_takes_the_whole_surplus():
    # Above the destination and no Excess load able to absorb: unchanged from
    # before this rule — the battery keeps buffering toward 100 %.
    assert recommended_charge_limit(
        4.0, 96.0, 90.0, _FULL_RATE, 0.0, _SP + 6500.0, _SP, 2.0, True,
        excess_draw_w=0.0, at_destination=True,
    ) == (6500.0, True)


def test_below_the_destination_the_battery_is_served_first():
    """The caller passes no draw at all below the destination (yielding False),
    so the advice is the plain feedback value even with a car on the surplus."""
    assert yields_to_excess(88.0, 95.0, 2.0, False) is False
    assert recommended_charge_limit(
        4.0, 88.0, 90.0, _FULL_RATE, 0.0, _SP + 6500.0, _SP, 2.0, True,
        excess_draw_w=0.0, at_destination=False,
    ) == (6500.0, True)


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
    """The bug's own case: absorbable 0, export under the setpoint, SOC at the
    destination. Engaged, and the advice is the floor — nothing to spare."""
    assert recommended_charge_limit(
        0.0, _DEST, _DEST, _BMS_RATE, 0.0, _SP - 5000.0, _SP, _HYST, False,
        at_destination=True,
    ) == (0.0, True)


def test_the_hold_admits_the_surplus_a_better_day_actually_makes():
    """The very case the buffer exists for: the forecast under-read the day.

    Nothing was reserved (absorbable 0), the pack is parked at 95 — and the site
    is exporting 1500 W more than it may. Those watts cannot leave, so the
    battery climbs above the destination on that surplus alone.
    """
    assert recommended_charge_limit(
        0.0, _DEST, _DEST, _BMS_RATE, 0.0, _SP + 1500.0, _SP, _HYST, True,
        at_destination=True,
    ) == (1500.0, True)
    # And it is still only the surplus: never the full rate the bug handed it.
    assert recommended_charge_limit(
        0.0, _DEST, _DEST, _BMS_RATE, 0.0, _SP + 99000.0, _SP, _HYST, True,
        at_destination=True,
    ) == (_BMS_RATE, True)


def test_an_excess_load_displaces_the_parked_battery_with_no_clip_either():
    # The engaged formula is the SAME one, so the watt-for-watt displacement
    # holds on a day that reserves nothing.
    assert recommended_charge_limit(
        0.0, 96.0, _DEST, _BMS_RATE, 0.0, _SP + 2500.0, _SP, _HYST, True,
        excess_draw_w=2300.0, at_destination=True,
    ) == (200.0, True)


def test_below_the_destination_nothing_to_clip_is_still_full_rate():
    """The cloudy-day protection, preserved exactly where it is correct: under
    the ceiling with no reserve to keep, the pack refills at full rate."""
    for soc in (0.0, 50.0, _DEST - _HYST, _DEST - 0.5):
        assert recommended_charge_limit(
            0.0, soc, _DEST, _BMS_RATE, 0.0, _SP - 5000.0, _SP, _HYST, False,
            at_destination=False,
        ) == (_BMS_RATE, False), f"held at SOC {soc}"


def test_the_live_event_replay_holds_at_the_destination_and_releases_below_it():
    """Destination 95, a 20 kWh pack, nothing forecast to clip, export under the
    setpoint — the maintainer's morning, replayed.

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
            0.0, soc, _DEST, _BMS_RATE, 0.0, _SP - 5000.0, _SP, _HYST, limiting,
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
            0.0, soc, _DEST, _BMS_RATE, 0.0, _SP - 5000.0, _SP, _HYST, limiting,
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
        0.0, _DEST, _DEST, _BMS_RATE, 0.0, _SP - 5000.0, _SP, _HYST, False,
        at_destination=True,
    )
    assert held == (0.0, True)

    # The clip appears while the pack sits at 95 — still at the destination, so
    # nothing moves.
    limit, limiting = recommended_charge_limit(
        2.0, _DEST, 85.0, _BMS_RATE, 0.0, _SP - 5000.0, _SP, _HYST, held[1],
        at_destination=True,
    )
    assert (limit, limiting) == (0.0, True)

    # The pack is discharged toward the reserve: the destination hold lets go at
    # 93, and the reservation holds it there instead of releasing to full rate.
    yielding = yields_to_excess(92.0, _DEST, _HYST, True)
    assert yielding is False
    limit, limiting = recommended_charge_limit(
        2.0, 92.0, 85.0, _BMS_RATE, 0.0, _SP - 5000.0, _SP, _HYST, limiting,
        at_destination=yielding,
    )
    assert (limit, limiting) == (0.0, True), "the handover stepped to full rate"

    # It releases where the reservation says, a full band below its own ceiling.
    assert recommended_charge_limit(
        2.0, 80.0, 85.0, _BMS_RATE, 0.0, _SP - 5000.0, _SP, _HYST, limiting,
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
        0.0, None, _DEST, _BMS_RATE, 0.0, _SP - 5000.0, _SP, _HYST, True,
        at_destination=False,
    ) == (_BMS_RATE, False)
    assert recommended_charge_limit(
        4.0, None, 85.0, _BMS_RATE, 0.0, _SP + 1000.0, _SP, _HYST, False,
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
            0.0, float(soc), 100.0, _BMS_RATE, 0.0, _SP - 5000.0, _SP, _HYST,
            False, at_destination=yielding,
        ) == (_BMS_RATE, False), f"SOC {soc} was held"
    # At 100 the pack IS at its destination, and holding a full battery on the
    # floor is what "standing ceiling" means — it cannot charge either way.
    assert yields_to_excess(100.0, 100.0, _HYST, False) is True
    assert recommended_charge_limit(
        0.0, 100.0, 100.0, _BMS_RATE, 0.0, _SP - 5000.0, _SP, _HYST, False,
        at_destination=True,
    ) == (0.0, True)
