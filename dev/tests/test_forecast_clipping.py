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
    ) == (5000.0, False)


def test_cap_released_immediately_when_nothing_left_to_clip():
    # Even while engaged: with nothing left to protect the latch drops at once.
    assert recommended_charge_limit(
        0.0, 100.0, 100.0, 5000.0, 0.0, THRESHOLD, 2.0, True
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


def test_unexportable_power_clamps_at_zero():
    assert unexportable_power(4000.0, THRESHOLD) == 0.0
    assert unexportable_power(8000.0, THRESHOLD) == 2000.0
    assert unexportable_power(None, THRESHOLD) == 0.0
