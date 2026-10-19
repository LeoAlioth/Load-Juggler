"""JUG-24: the SG Ready state machine (calculations/sg_ready.py) - pure, no Home Assistant."""
import pytest

from custom_components.dynamic_ocpp_evse.calculations import sg_ready as sg
from custom_components.dynamic_ocpp_evse.calculations.models import PhaseValues, SiteContext
from custom_components.dynamic_ocpp_evse.calculations.target_calculator import grid_headroom_w

CFG = sg.SgConfig(breaker_hold_s=0.0)  # the debounce has its own tests below
MARGIN = 500.0
MIN = CFG.min_state_s
DAY = sg.DAY_S


def inp(**kw):
    return sg.SgInputs(**kw)


def run(mem, inputs, t):
    return sg.step(mem, inputs, CFG, t)


def memory(state=sg.NORMAL, since=0.0):
    return sg.SgMemory(state=state, entered_at=since)


# --- the state table, one row per case -------------------------------------

def test_row1_overload_blocks():
    mem = memory()
    d = run(mem, inp(breaker_over_w=500), MIN + 1)
    assert d.state == sg.BLOCK and d.blocks_24h == 1


def test_row2_actual_excess_forces():
    assert run(memory(), inp(actual_excess=True), MIN + 1).state == sg.FORCE


def test_row3_early_start_recommends_then_excess_forces():
    mem = memory()
    d = run(mem, inp(early_start=True), MIN + 1)
    assert (d.state, d.reason) == (sg.RECOMMENDED, "predicted excess")
    d = run(mem, inp(early_start=True, actual_excess=True), MIN + 1 + MIN)
    assert d.state == sg.FORCE


def test_row4_battery_above_target_recommends():
    d = run(memory(), inp(battery_above_target=True), MIN + 1)
    assert (d.state, d.reason) == (sg.RECOMMENDED, "battery above target")


def test_row5_otherwise_normal():
    assert run(memory(sg.RECOMMENDED), inp(), MIN + 1).state == sg.NORMAL


def test_first_match_wins():
    inputs = inp(breaker_over_w=1, actual_excess=True, early_start=True, battery_above_target=True)
    assert run(memory(), inputs, MIN + 1).state == sg.BLOCK
    inputs = inp(actual_excess=True, early_start=True, battery_above_target=True)
    assert run(memory(), inputs, MIN + 1).state == sg.FORCE


def test_battery_free_goes_normal_then_force_nothing_between():
    mem = memory()
    t = MIN + 1
    assert run(mem, inp(), t).state == sg.NORMAL  # battery_above_target is never set
    assert run(mem, inp(actual_excess=True), t + 1).state == sg.FORCE


# --- the 10-minute rule ----------------------------------------------------

def test_state_held_for_the_minimum_time():
    mem = memory(sg.FORCE, since=100.0)
    d = run(mem, inp(), 100.0 + MIN - 1)
    assert d.state == sg.FORCE and d.min_left_s == pytest.approx(1)
    assert run(mem, inp(), 100.0 + MIN).state == sg.NORMAL


def test_a_breaker_overload_does_not_wait_for_the_minimum_of_the_state_it_interrupts():
    mem = memory(sg.RECOMMENDED, since=0.0)
    assert run(mem, inp(breaker_over_w=900), 5.0).state == sg.BLOCK


def test_a_max_import_overload_does_wait_for_it():
    cfg = sg.SgConfig(import_hold_s=0.0)
    mem = memory(sg.RECOMMENDED, since=0.0)
    assert sg.step(mem, inp(import_over_w=900), cfg, MIN - 1).state == sg.RECOMMENDED
    assert sg.step(mem, inp(import_over_w=900), cfg, MIN).state == sg.BLOCK


def test_reentry_is_never_sooner_than_the_minimum():
    mem = memory(sg.NORMAL, since=0.0)
    t = MIN
    run(mem, inp(actual_excess=True), t)               # Force from t
    run(mem, inp(), t + MIN)                           # Normal from t + MIN
    assert mem.entered_at == t + MIN
    # Force again no sooner than MIN after Normal began, i.e. >= MIN after Force ended
    assert run(mem, inp(actual_excess=True), t + MIN + MIN - 1).state == sg.NORMAL
    assert run(mem, inp(actual_excess=True), t + 2 * MIN).state == sg.FORCE


def test_start_up_state_counts_as_long_served():
    mem = sg.SgMemory()
    assert run(mem, inp(breaker_over_w=1), 5.0).state == sg.BLOCK


# --- Block limits ----------------------------------------------------------

def test_block_released_only_when_headroom_covers_the_normal_draw_plus_margin():
    mem = memory(sg.BLOCK, since=0.0)
    draw = 1500.0
    short = inp(headroom_w=draw + MARGIN - 1, normal_draw_w=draw, release_margin_w=MARGIN)
    d = run(mem, short, MIN + 1)
    assert d.state == sg.BLOCK and "headroom" in d.reason
    ok = inp(headroom_w=draw + MARGIN, normal_draw_w=draw, release_margin_w=MARGIN)
    assert run(mem, ok, MIN + 2).state == sg.NORMAL


def test_block_held_for_the_minimum_even_with_room():
    mem = memory(sg.BLOCK, since=0.0)
    assert run(mem, inp(headroom_w=9999, normal_draw_w=1000), MIN - 1).state == sg.BLOCK


def test_block_without_a_grid_limit_is_released_at_once():
    mem = memory(sg.BLOCK, since=0.0)
    assert run(mem, inp(headroom_w=None, normal_draw_w=1000), MIN).state == sg.NORMAL


def test_overload_keeps_the_block_going():
    mem = memory(sg.BLOCK, since=0.0)
    assert run(mem, inp(breaker_over_w=100, headroom_w=-100), MIN + 10).state == sg.BLOCK


def test_block_ends_at_its_maximum_time_and_is_flagged():
    mem = memory(sg.BLOCK, since=0.0)
    d = run(mem, inp(breaker_over_w=100, headroom_w=-100), CFG.max_block_s)
    assert d.state == sg.NORMAL and d.allowance_used_up


def test_three_blocks_in_a_rolling_day_then_normal_never_block():
    mem = sg.SgMemory()
    t = 0.0
    for _ in range(3):
        t += MIN
        assert run(mem, inp(breaker_over_w=1), t).state == sg.BLOCK
        t += MIN
        assert run(mem, inp(headroom_w=None), t).state == sg.NORMAL
    t += MIN
    d = run(mem, inp(breaker_over_w=1), t)
    assert d.state == sg.NORMAL and d.allowance_used_up and d.blocks_24h == 3


def test_the_day_is_rolling_not_the_calendar():
    mem = sg.SgMemory(state=sg.NORMAL, entered_at=0.0, block_starts=[1000.0, 2000.0, 3000.0])
    assert run(mem, inp(breaker_over_w=1), 3000.0 + MIN).state == sg.NORMAL  # used up
    t = 1000.0 + DAY  # the oldest block has left the window
    assert sg.blocks_in_day(mem, t) == 2
    assert run(mem, inp(breaker_over_w=1), t).state == sg.BLOCK
    assert mem.block_starts[-1] == t


def test_used_up_breaker_over_waits_for_the_current_states_minimum_then_normal():
    mem = sg.SgMemory(state=sg.FORCE, entered_at=0.0, block_starts=[0.0, 1.0, 2.0])
    assert run(mem, inp(breaker_over_w=1), MIN - 1).state == sg.FORCE
    assert run(mem, inp(breaker_over_w=1), MIN).state == sg.NORMAL


# --- relay failure, restart ------------------------------------------------

@pytest.mark.parametrize("state", [sg.BLOCK, sg.RECOMMENDED, sg.FORCE])
def test_relay_not_available_goes_normal_at_once(state):
    mem = memory(state, since=1000.0)
    d = run(mem, inp(relays_ok=False, breaker_over_w=5), 1001.0)
    assert d.state == sg.NORMAL and d.reason == "relay not available"


def test_a_cut_short_block_still_counts_toward_the_allowance():
    mem = sg.SgMemory(state=sg.NORMAL, entered_at=0.0)
    run(mem, inp(breaker_over_w=1), MIN)
    run(mem, inp(relays_ok=False), MIN + 5)
    assert sg.blocks_in_day(mem, MIN + 6) == 1


def test_fresh_memory_is_normal():
    assert sg.SgMemory().state == sg.NORMAL


# --- relays and their order --------------------------------------------------

def test_relay_combinations():
    assert sg.RELAYS == {
        sg.BLOCK: (True, False), sg.NORMAL: (False, False),
        sg.RECOMMENDED: (False, True), sg.FORCE: (True, True),
    }


ALL = [sg.BLOCK, sg.NORMAL, sg.RECOMMENDED, sg.FORCE]


@pytest.mark.parametrize("a", ALL)
@pytest.mark.parametrize("b", ALL)
def test_relay_path_changes_one_relay_at_a_time_and_ends_on_target(a, b):
    path = sg.relay_path(a, b)
    prev = sg.RELAYS[a]
    for combo in path:
        assert sum(x != y for x, y in zip(prev, combo)) == 1
        prev = combo
    assert prev == sg.RELAYS[b]


def test_relay_path_goes_through_the_mildest_state():
    assert sg.relay_path(sg.BLOCK, sg.RECOMMENDED)[0] == sg.RELAYS[sg.NORMAL]
    assert sg.relay_path(sg.RECOMMENDED, sg.BLOCK)[0] == sg.RELAYS[sg.NORMAL]
    assert sg.relay_path(sg.NORMAL, sg.FORCE)[0] == sg.RELAYS[sg.RECOMMENDED]
    assert sg.relay_path(sg.FORCE, sg.NORMAL)[0] == sg.RELAYS[sg.RECOMMENDED]


# --- the shortage reaching the devices in priority order ----------------------

def test_shortage_reaches_the_lowest_priority_first():
    devices = [("a", 1, 1000, sg.NORMAL, True), ("b", 2, 1000, sg.NORMAL, True)]
    assert sg.overloaded_devices(500, devices) == {"b"}
    assert sg.overloaded_devices(1500, devices) == {"a", "b"}
    assert sg.overloaded_devices(0, devices) == set()


def test_a_lower_device_that_cannot_block_gives_no_relief():
    devices = [("a", 1, 1000, sg.NORMAL, True), ("b", 2, 1000, sg.NORMAL, False)]
    assert sg.overloaded_devices(500, devices) == {"a", "b"}


def test_a_device_already_blocked_gives_no_relief_twice():
    devices = [("a", 1, 1000, sg.NORMAL, True), ("b", 2, 1000, sg.BLOCK, True)]
    assert "a" in sg.overloaded_devices(100, devices)


# --- the headroom the engine feeds -------------------------------------------

def _site(cons=(10.0, 0.0, 0.0), rating=25, **kw):
    return SiteContext(
        voltage=230, main_breaker_rating=rating,
        consumption=PhaseValues(*cons), export_current=PhaseValues(0.0, 0.0, 0.0), **kw,
    )


def test_headroom_is_signed_and_per_phase():
    assert grid_headroom_w(_site(), "A") == pytest.approx(15 * 230)
    assert grid_headroom_w(_site(cons=(30.0, 0.0, 0.0)), "A") == pytest.approx(-5 * 230)


def test_headroom_of_a_multi_phase_device_is_its_tightest_phase_times_n():
    site = _site(cons=(10.0, 20.0, 0.0))
    assert grid_headroom_w(site, "AB") == pytest.approx(5 * 2 * 230)


def test_headroom_respects_the_max_import_power():
    site = _site(cons=(10.0, 0.0, 0.0), max_grid_import_power=2500)
    assert grid_headroom_w(site, "A") == pytest.approx(2500 - 10 * 230)


def test_off_grid_has_no_grid_limit():
    assert grid_headroom_w(_site(is_off_grid=True), "A") is None


# --- the two overloads, two timings ---------------------------------------------

def test_breaker_overload_blocks_well_inside_the_90_s_a_breaker_tolerates():
    cfg = sg.SgConfig()  # the default noise filter
    assert cfg.breaker_hold_s <= 15 < 90
    mem = memory()
    cycle, t, blocked_at = 2.0, 1000.0, None
    for i in range(45):  # 90 s of 2 s cycles
        d = sg.step(mem, inp(breaker_over_w=800), cfg, t + i * cycle)
        if d.state == sg.BLOCK:
            blocked_at = i * cycle
            break
    assert blocked_at is not None and blocked_at <= cfg.breaker_hold_s + cycle


def test_a_breaker_spike_shorter_than_the_noise_filter_is_ignored():
    cfg = sg.SgConfig()
    mem = memory()
    assert sg.step(mem, inp(breaker_over_w=800), cfg, 1000.0).state == sg.NORMAL
    assert sg.step(mem, inp(breaker_over_w=800), cfg, 1000.0 + cfg.breaker_hold_s - 1).state == sg.NORMAL
    sg.step(mem, inp(), cfg, 1000.0 + cfg.breaker_hold_s)  # back under: the filter starts over
    assert sg.step(mem, inp(breaker_over_w=800), cfg, 1000.0 + cfg.breaker_hold_s + 1).state == sg.NORMAL


def test_max_import_overload_blocks_only_after_two_continuous_minutes():
    cfg = sg.SgConfig()
    assert cfg.import_hold_s == 120
    mem = memory()
    t = 1000.0
    assert sg.step(mem, inp(import_over_w=500), cfg, t).state == sg.NORMAL
    assert sg.step(mem, inp(import_over_w=500), cfg, t + 119).state == sg.NORMAL
    assert sg.step(mem, inp(import_over_w=500), cfg, t + 120).state == sg.BLOCK


def test_max_import_overload_that_drops_under_starts_the_two_minutes_over():
    cfg = sg.SgConfig()
    mem = memory()
    t = 1000.0
    sg.step(mem, inp(import_over_w=500), cfg, t)
    sg.step(mem, inp(import_over_w=500), cfg, t + 100)
    assert sg.step(mem, inp(), cfg, t + 110).state == sg.NORMAL  # dropped under
    assert sg.step(mem, inp(import_over_w=500), cfg, t + 115).state == sg.NORMAL
    assert sg.step(mem, inp(import_over_w=500), cfg, t + 115 + 119).state == sg.NORMAL
    assert sg.step(mem, inp(import_over_w=500), cfg, t + 115 + 120).state == sg.BLOCK


def test_the_two_headrooms_are_kept_apart():
    from custom_components.dynamic_ocpp_evse.calculations.target_calculator import grid_headrooms_w
    site = _site(cons=(10.0, 0.0, 0.0), max_grid_import_power=2000)
    breaker, imported = grid_headrooms_w(site, "A")
    assert breaker == pytest.approx(15 * 230) and imported == pytest.approx(2000 - 2300)
    assert grid_headrooms_w(_site(), "A")[1] is None
