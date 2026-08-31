"""The battery SOC hysteresis latch — engine/hub_calculation._apply_soc_hysteresis.

The latch widens a threshold once it has been crossed so the SOC-gated modes
cannot chatter around it. Its answer has to agree with the gates that consume
it, and on the MIN floor it did not: the latch read "still above" at exactly the
floor while ``_source_limit`` read "not above" and shed the load, so the band
never armed. Observed live 2026-08-29 on an off-grid site: a 3.6 kW plug in
Solar Priority cycled 58 times in ten hours in a 1% SOC window at the floor,
until the car plugged into it locked its onboard charger out.

The contract, from the function's own docstring: discharge stops AT the
configured floor and resumes only a full hysteresis above it.
"""

from custom_components.dynamic_ocpp_evse.engine.hub_calculation import (
    _apply_soc_hysteresis,
)

HYST = 3.0
FLOOR = 10.0
TARGET = 80.0


def _apply(runtime, soc):
    """One cycle of the latch; returns the thresholds the calculator sees."""
    target, floor, _above_target, above_min = _apply_soc_hysteresis(
        runtime, soc, HYST, TARGET, FLOOR
    )
    return floor, above_min


def _gate_is_on(soc, floor):
    """The SOC half of BEHAVIOR_BINARY_ABOVE_MIN, verbatim."""
    return soc > floor


# --- The floor -------------------------------------------------------------


def test_sitting_exactly_on_the_floor_arms_the_band():
    """The case that chattered: at the floor the gate sheds, so the latch has
    to agree and widen — otherwise the load switches off at the floor, the
    battery recovers one percent, and it switches straight back on."""
    runtime = {"_soc_above_min": True}
    floor, above_min = _apply(runtime, FLOOR)

    assert above_min is False
    assert floor == FLOOR + HYST
    assert _gate_is_on(FLOOR, floor) is False


def test_the_band_holds_the_load_off_through_the_recovery():
    """One percent back is not enough — that was the whole 1% oscillation."""
    runtime = {"_soc_above_min": True}
    _apply(runtime, FLOOR)

    for soc in (FLOOR + 1, FLOOR + 2):
        floor, above_min = _apply(runtime, soc)
        assert above_min is False
        assert _gate_is_on(soc, floor) is False


def test_a_full_hysteresis_above_the_floor_resumes():
    """Floor 10, hysteresis 3 → stop at 10, resume at 13, as documented."""
    runtime = {"_soc_above_min": True}
    _apply(runtime, FLOOR)

    floor, above_min = _apply(runtime, FLOOR + HYST)
    assert above_min is True
    assert floor == FLOOR
    assert _gate_is_on(FLOOR + HYST, floor) is True


def test_above_the_floor_nothing_is_widened():
    """The band is not a permanent offset — it applies only while shed."""
    runtime = {"_soc_above_min": True}
    floor, above_min = _apply(runtime, FLOOR + 5)
    assert above_min is True
    assert floor == FLOOR


def test_one_full_cycle_spans_the_whole_band():
    """End to end: the load runs from resume down to the floor and stops, and
    the next start needs the full band again — no 1% window anywhere in it."""
    runtime = {"_soc_above_min": True}
    on_at = []
    # Draining from well above the floor down through it.
    for soc in (14.0, 13.0, 12.0, 11.0, 10.0):
        floor, _ = _apply(runtime, soc)
        on_at.append(_gate_is_on(soc, floor))
    assert on_at == [True, True, True, True, False]

    # Recovering: nothing until a full hysteresis above the floor.
    back_on = []
    for soc in (11.0, 12.0, 13.0):
        floor, _ = _apply(runtime, soc)
        back_on.append(_gate_is_on(soc, floor))
    assert back_on == [False, False, True]


# --- The target is unaffected ----------------------------------------------


def test_the_target_band_still_latches_at_the_threshold():
    """The target side never had this defect and must not acquire one: there
    the latch DOES widen its threshold while above, so reaching the target
    lowers the bar to target − hysteresis instead of leaving a boundary the
    gate disagrees with."""
    runtime = {"_soc_above_target": False}
    target, _floor, above_target, _above_min = _apply_soc_hysteresis(
        runtime, TARGET, HYST, TARGET, FLOOR
    )
    assert above_target is True
    assert target == TARGET - HYST


def test_hysteresis_disabled_leaves_both_thresholds_raw():
    runtime = {}
    target, floor, above_target, above_min = _apply_soc_hysteresis(
        runtime, FLOOR, 0, TARGET, FLOOR
    )
    assert (target, floor) == (TARGET, FLOOR)
    assert above_target is False and above_min is False


# --- Which modes may ride out a collapsed permit ---------------------------
#
# Solar Priority was excluded from the grace window outright, because the hold
# could not tell a brief inverter saturation from a minimum-SOC shed and the
# floor is protective. For a binary load the two ARE distinguishable here: the
# floor the engine gated on is published alongside the live SOC.

from custom_components.dynamic_ocpp_evse.entities.load import (  # noqa: E402
    grace_modes,
    soc_floor_reached,
)

SOLAR_PRIORITY = "Solar Priority"
SOLAR_ONLY = "Solar Only"
EXCESS = "Excess"


def test_a_binary_load_above_the_floor_may_ride_out_a_dip():
    modes = grace_modes(True, {"battery_soc": 40.0, "battery_soc_min": 10.0})
    assert SOLAR_PRIORITY in modes


def test_a_binary_load_at_the_floor_sheds_immediately():
    """The protective case: no ride-through, exactly as before this change."""
    modes = grace_modes(True, {"battery_soc": 10.0, "battery_soc_min": 10.0})
    assert SOLAR_PRIORITY not in modes
    assert modes == (SOLAR_ONLY, EXCESS)


def test_the_widened_floor_is_what_counts():
    """While shed, the published floor IS floor + hysteresis, so the whole
    recovery is protected rather than just the moment of the crossing."""
    modes = grace_modes(True, {"battery_soc": 11.0, "battery_soc_min": 13.0})
    assert SOLAR_PRIORITY not in modes


def test_an_unreadable_soc_never_buys_a_ride_through():
    assert soc_floor_reached({"battery_soc": None, "battery_soc_min": 10.0}) is True
    assert soc_floor_reached({"battery_soc": 40.0, "battery_soc_min": None}) is True
    assert soc_floor_reached({}) is True
    assert grace_modes(True, {}) == (SOLAR_ONLY, EXCESS)


def test_modulating_loads_are_untouched():
    """An EVSE in Solar Priority falls back to a grid-backed minimum rather
    than to zero, so it has no collapse to bridge and gains no new mode."""
    modes = grace_modes(False, {"battery_soc": 40.0, "battery_soc_min": 10.0})
    assert modes == (SOLAR_ONLY, EXCESS)


# --- Minimum off time ------------------------------------------------------
#
# The dwell that bounds how OFTEN a binary load may cycle. It can only ever
# withhold a permit, never grant or hold one, so it cannot keep a load running
# past a protective shed — which is why it needs no notion of cause at all.

from custom_components.dynamic_ocpp_evse.entities.load import (  # noqa: E402
    min_off_hold,
)

TEN_MIN = 600.0


def test_the_shed_itself_is_never_delayed():
    """Nothing may make a load switch off later than the engine says."""
    permit, off_since, held = min_off_hold(0.0, None, 1000.0, TEN_MIN)
    assert permit == 0.0
    assert off_since == 1000.0  # stamped, so the dwell runs from the shed
    assert held is None


def test_an_early_recovery_is_withheld():
    permit, off_since, held = min_off_hold(16.0, 1000.0, 1100.0, TEN_MIN)
    assert permit == 0.0
    assert off_since == 1000.0  # still counting from the original shed
    assert held == 100.0


def test_the_permit_returns_once_the_dwell_is_served():
    permit, off_since, held = min_off_hold(16.0, 1000.0, 1600.0, TEN_MIN)
    assert permit == 16.0
    assert off_since is None
    assert held == 600.0


def test_a_load_already_running_is_untouched():
    assert min_off_hold(16.0, None, 1000.0, TEN_MIN) == (16.0, None, None)


def test_zero_disables_it():
    """0 restores the previous behaviour exactly — permit honoured at once."""
    permit, off_since, _held = min_off_hold(16.0, 1000.0, 1000.5, 0)
    assert permit == 16.0
    assert off_since is None


def test_the_dwell_runs_from_the_shed_not_from_the_last_attempt():
    """The chatter case: repeated recoveries inside the window must not each
    restart the clock, or the load would never come back at all."""
    off_since = None
    _p, off_since, _h = min_off_hold(0.0, off_since, 0.0, TEN_MIN)
    for now in (60.0, 120.0, 300.0):
        permit, off_since, _h = min_off_hold(16.0, off_since, now, TEN_MIN)
        assert permit == 0.0
    permit, off_since, held = min_off_hold(16.0, off_since, 600.0, TEN_MIN)
    assert permit == 16.0 and held == 600.0


def test_the_live_case_would_have_collapsed_to_one_cycle():
    """The 2026-08-29 trace: a permit that recovered roughly every 45 s. Over a
    quarter of an hour that is ten switch-ons; with a ten-minute dwell it is
    one — long enough for the car to finish negotiating."""
    off_since = None
    switch_ons = 0
    raw_ons = 0
    for tick in range(0, 901, 45):
        permit_in = 0.0 if (tick // 45) % 2 else 16.0
        was_off = off_since is not None
        permit, off_since, _h = min_off_hold(permit_in, off_since, float(tick), TEN_MIN)
        if permit_in > 0 and tick > 0:
            raw_ons += 1
        if permit > 0 and was_off:
            switch_ons += 1
    assert raw_ons == 10
    assert switch_ons == 1
