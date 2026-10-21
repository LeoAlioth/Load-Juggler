"""JUG-30: the Variable Load arithmetic (calculations/variable_load.py) - pure, no Home Assistant.

Machine-authored tests - not yet human-reviewed.
"""
import pytest

from custom_components.dynamic_ocpp_evse.calculations import variable_load as vl

V = 230.0
MIN10 = 600.0


def test_bounds_in_amps_stay_as_they_are():
    assert vl.bounds_amps("A", 6, 16, V, 3) == (6.0, 16.0)


def test_bounds_in_watts_spread_over_the_legs():
    lo, hi = vl.bounds_amps("W", 1380, 11040, V, 3)
    assert lo == pytest.approx(2.0) and hi == pytest.approx(16.0)


def test_setpoint_amps_and_watts():
    assert vl.setpoint(10, "A", V, 3) == 10
    assert vl.setpoint(10, "W", V, 3) == 6900


def test_setpoint_bias_only_while_something_is_allowed():
    assert vl.setpoint(10, "A", V, 1, bias=0.5) == 10.5
    assert vl.setpoint(0, "A", V, 1, bias=0.5) == 0


def test_setpoint_floors_to_the_step_and_keeps_to_the_entity_range():
    assert vl.setpoint(10.79, "A", V, 1, step=1) == 10       # never more than allowed
    assert vl.setpoint(40, "A", V, 1, entity_max=32) == 32
    # Hold / a stop on an entity that cannot take 0: its minimum
    assert vl.setpoint(0, "A", V, 1, entity_min=6) == 6


def test_outputs_per_leg_lowest_power_percent():
    out = vl.outputs(10, ["A", "B", "C"], [], V, 16)
    assert (out["allowed_l1"], out["allowed_l2"], out["allowed_l3"]) == (10, 10, 10)
    assert out["allowed_lowest"] == 10
    assert out["allowed_power"] == 6900
    assert out["allowed_percent"] == 62


def test_outputs_single_leg_has_no_l2_l3():
    out = vl.outputs(8, ["B"], [], V, 16)
    assert out["allowed_l2"] is None and out["allowed_l3"] is None
    assert out["allowed_lowest"] == 8 and out["allowed_power"] == 1840




def test_advice_first_reading_stands():
    assert vl.phase_advice(5000, None, 0, 4000, 2000, MIN10)[0] == vl.MULTI
    assert vl.phase_advice(1000, None, 0, 4000, 2000, MIN10)[0] == vl.SINGLE
    assert vl.phase_advice(3000, None, 0, 4000, 2000, MIN10)[0] == vl.SINGLE  # in the band


def test_advice_holds_inside_the_band():
    _, _, mem = vl.phase_advice(5000, None, 0, 4000, 2000, MIN10)
    for power in (3999, 3000, 2001, 2000):
        advice, reason, mem = vl.phase_advice(power, mem, 10_000, 4000, 2000, MIN10)
        assert advice == vl.MULTI, (power, reason)


def test_advice_flips_only_past_a_threshold_and_after_the_minimum_time():
    _, _, mem = vl.phase_advice(5000, None, 0, 4000, 2000, MIN10)
    advice, reason, mem = vl.phase_advice(1500, mem, 300, 4000, 2000, MIN10)
    assert advice == vl.MULTI and "held 5 min" in reason
    advice, _, mem = vl.phase_advice(1500, mem, 599, 4000, 2000, MIN10)
    assert advice == vl.MULTI
    advice, reason, mem = vl.phase_advice(1500, mem, 600, 4000, 2000, MIN10)
    assert advice == vl.SINGLE and "below 2000 W" in reason
    # and back: the clock restarts at the change
    advice, _, mem = vl.phase_advice(4500, mem, 1100, 4000, 2000, MIN10)
    assert advice == vl.SINGLE
    advice, _, mem = vl.phase_advice(4500, mem, 1200, 4000, 2000, MIN10)
    assert advice == vl.MULTI


def test_advice_never_changes_more_often_than_the_minimum_time():
    """A power swinging across both thresholds every 30 s for an hour."""
    mem, changes, last = None, [], None
    for i in range(120):
        t = i * 30.0
        power = 5000 if (i // 1) % 2 == 0 else 1000
        advice, _, mem = vl.phase_advice(power, mem, t, 4000, 2000, MIN10)
        if advice != last:
            changes.append(t)
            last = advice
    gaps = [b - a for a, b in zip(changes, changes[1:])]
    assert gaps and min(gaps) >= MIN10


def test_sized_legs_follow_the_legs_drawing_and_remember_them_while_idle():
    assert vl.sized_legs([7.0, 0.2, 0.0], None, False) == [1]
    assert vl.sized_legs([7.0, 7.0, 7.0], [1], False) == [1, 2, 3]
    assert vl.sized_legs([0.0, 0.0, 0.0], [1], False) == [1]       # idle: still single
    assert vl.sized_legs([0.0, 0.0, 0.0], [1], True) == []         # unplugged: every leg
    assert vl.sized_legs([0.0, 0.0, 0.0], None, False) == []
    assert vl.sized_legs([None, 5.0, None], None, False) == [2]


def test_outputs_on_fewer_legs_than_configured():
    """Sized on L1 only: L2/L3 are allowed nothing, the power is one leg's."""
    out = vl.outputs(12, ["A", "B", "C"], [1], V, 16)
    assert (out["allowed_l1"], out["allowed_l2"], out["allowed_l3"]) == (12, 0.0, 0.0)
    assert out["allowed_lowest"] == 12
    assert out["allowed_power"] == 2760
