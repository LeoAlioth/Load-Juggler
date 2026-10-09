"""The scenario harness places an asymmetric inverter's output to balance the grid.

Machine-authored tests - not yet human-reviewed.

run_tests.place_asymmetric_output is what simulate_grid_ct builds every
asymmetric inverter's per-phase grid readings from (Anže, 2026-09-24: it
"puts/pulls power on the phases in a way to always try to balance them on
the grid"). Until then the harness spread its output evenly, and
3ph-1c-standard-heavy-importing read 25.97 A on a 25 A breaker that the
hardware never would.
"""

from .run_tests import place_asymmetric_output


def _grid(demand, total, cap):
    out = place_asymmetric_output(demand, total, cap)
    assert abs(sum(out) - total) < 1e-6, out
    assert all(abs(o) <= cap + 1e-9 for o in out), out
    return [round(d - o, 2) for d, o in zip(demand, out)]


def test_balances_every_phase_when_the_caps_allow():
    # 3ph-1c-standard-heavy-importing: house 8/7/6 A + a 28.3 A three-phase
    # car, 13 A of sun + 18 A of battery, 26 A per leg.
    assert _grid([36.3, 35.3, 34.3], 31.0, 26.0) == [24.97, 24.97, 24.97]


def test_a_capped_leg_keeps_the_rest_and_the_others_share_what_is_left():
    # The same site with a 32 A single-phase car on A: A's leg stops at 26 A.
    assert _grid([40.0, 7.0, 6.0], 31.0, 26.0) == [14.0, 4.0, 4.0]


def test_pulls_from_a_light_phase_to_balance():
    assert _grid([20.0, 0.0, 0.0], 3.0, 26.0) == [5.67, 5.67, 5.67]


def test_a_total_past_every_cap_falls_back_to_the_even_spread():
    assert place_asymmetric_output([10.0, 0.0, 0.0], 90.0, 26.0) == [30.0, 30.0, 30.0]


def test_the_pushes_together_stay_within_the_rating():
    """A pull on C lets the inverter push on A and B for a small net: at 10 A
    per phase that would be 20 A out on a 15 A inverter. The pushes come down
    to 15 A and the pull with them, the net unchanged."""
    out = place_asymmetric_output([12.0, 12.0, -8.0], 12.0, 10.0, rating=15.0)
    assert abs(sum(out) - 12.0) < 1e-9
    assert abs(sum(o for o in out if o > 0) - 15.0) < 1e-9
    assert all(abs(o) <= 10.0 + 1e-9 for o in out)
    # Within the rating, the placement is the caps' alone.
    assert place_asymmetric_output([12.0, 12.0, -8.0], 12.0, 10.0) == \
        place_asymmetric_output([12.0, 12.0, -8.0], 12.0, 10.0, rating=100.0)

