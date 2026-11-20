"""Phase switching: the rule and the single-phase trial (pure).

Machine-authored tests - not yet human-reviewed.

Design: docs/plans/2026-11-01/phase-switching.md. The site is the third site's
on 2026-11-01: a 20 A breaker, an import cap of 6.9 kW (its "smoothed average
power to the end of the block" sensor at the time) and a ~3 kW house, most of
it on phase B. Three phases at the 6 A minimum need 4.1 kW, there were 3.9 kW,
and Charger Nova was paused; one phase on A would have run at 16 A.
"""

import math
import pytest

from custom_components.dynamic_ocpp_evse.calculations.models import (
    LoadContext,
    PhaseValues,
    SiteContext,
)
from custom_components.dynamic_ocpp_evse.calculations.phase_switch import (
    ONE,
    THREE,
    decide,
    set_phases,
    three_phase_gain_w,
    single_phase_permit,
    spared_by_one_phase,
    paused_by_three_phases,
)
from custom_components.dynamic_ocpp_evse.calculations.target_calculator import (
    calculate_all_load_targets,
)
from custom_components.dynamic_ocpp_evse.const import (
    DISTRIBUTION_MODE_SEQUENTIAL_OPTIMIZED,
    DISTRIBUTION_MODE_SHARED,
)


def _site(cap_w=6900.0, house=(2.5, 10.0, 0.5)):
    nova = LoadContext(
        load_id="nova", entity_id="charger_nova", min_current=6, max_current=16,
        phases=3, connector_status="Charging", phase_switchable=True,
    )
    return SiteContext(
        voltage=230,
        main_breaker_rating=20,
        consumption=PhaseValues(*house),
        export_current=PhaseValues(0.0, 0.0, 0.0),
        max_grid_import_power=cap_w,
        distribution_mode=DISTRIBUTION_MODE_SEQUENTIAL_OPTIMIZED,
        loads=[nova],
    ), nova


def test_three_phases_pause_and_one_phase_runs_under_the_import_cap():
    site, nova = _site()
    calculate_all_load_targets(site)
    assert nova.available_current < 6
    assert single_phase_permit(site, "nova") >= 6
    # The trial leaves the real site alone.
    assert nova.phases == 3 and nova.active_phases_mask == "ABC"


def test_the_calculation_run_twice_gives_the_same_permits():
    site, nova = _site(cap_w=12000)
    calculate_all_load_targets(site)
    first = (nova.allocated_current, nova.available_current)
    calculate_all_load_targets(site)
    assert (nova.allocated_current, nova.available_current) == first


def test_on_one_phase_the_potential_is_what_three_legs_could_take():
    site, nova = _site(cap_w=12000)
    set_phases(nova, ONE)
    calculate_all_load_targets(site)
    assert nova.active_phases_mask == "A"
    assert nova.potential_current >= 6 + 1   # room for three again


def _decide(on=THREE, since=0.0, now=1000.0, **kw):
    """On one phase the permit defaults to 16 A (one phase at its maximum),
    and ``gain_w`` is worked out from it and ``potential`` at 230 V."""
    args = dict(connected=True, held=False, permit=16.0 if on == ONE else 0.0, min_current=6.0,
                single_permit=lambda: 0.0, potential=0.0, margin_w=1000.0,
                above_s=math.inf, hold_s=300, interval_s=600)
    args.update(kw)
    args.setdefault("gain_w", three_phase_gain_w(args["potential"], args["permit"], 230.0))
    return decide(on, since, now, **args)


def test_three_phases_stay_while_they_reach_the_minimum():
    called = []
    phases, since, _ = _decide(permit=8.0, single_permit=lambda: called.append(1) or 16.0)
    assert (phases, since) == (THREE, 0.0)
    assert not called   # the trial costs a whole calculation - only when needed


def test_one_phase_at_once_when_three_would_pause():
    phases, since, reason = _decide(permit=5.6, single_permit=lambda: 16.0)
    assert (phases, since) == (ONE, 1000.0)
    assert "would pause" in reason


def test_three_kept_when_one_phase_does_not_reach_the_minimum_either():
    assert _decide(permit=2.0, single_permit=lambda: 4.0)[0] == THREE


def test_back_to_three_only_after_the_interval_and_for_the_margin():
    # one phase at 16 A (3.68 kW); three at 6.5 A give 4.49 kW, 805 W more: under 1 kW, stays
    phases, _, reason = _decide(on=ONE, since=0.0, now=2000.0, potential=6.5)
    assert phases == ONE and "under the 1000 W margin" in reason
    # three at 7.0 A give 1150 W more, but within 10 minutes of the last change: stays, and says why
    phases, _, reason = _decide(on=ONE, since=900.0, now=1000.0, potential=7.0)
    assert phases == ONE and "last change 2 min ago" in reason
    # enough and the interval is over
    assert _decide(on=ONE, since=0.0, now=601.0, potential=7.0)[:2] == (THREE, 601.0)


def test_the_margin_is_power_against_what_one_phase_gets_now():
    """The third site, 2026-11-11: one phase at 20 A (4.6 kW) against three at
    7.6 A (5.24 kW) is 644 W more - under 1 kW, so it stays on one; one phase
    at 16 A against the same three is 1.56 kW more, so it goes back."""
    assert _decide(on=ONE, potential=7.6, permit=20.0)[0] == ONE
    assert _decide(on=ONE, potential=7.6, permit=16.0)[0] == THREE
    # the margin is the charger's option
    assert _decide(on=ONE, potential=7.6, permit=20.0, margin_w=500.0)[0] == THREE
    # never back to three with a leg under the minimum, whatever the power
    assert _decide(on=ONE, potential=5.9, permit=6.0, margin_w=0.0)[0] == ONE


def test_back_to_three_only_once_the_margin_held_five_minutes():
    # interval long over, enough power, but held only for 2 minutes: stays, says when
    phases, _, reason = _decide(on=ONE, since=0.0, now=5000.0, potential=8.0, above_s=120.0)
    assert phases == ONE and "back to three in 3 min" in reason
    assert _decide(on=ONE, since=0.0, now=5000.0, potential=8.0, above_s=300.0)[:2] == (THREE, 5000.0)


def test_not_charging_resumes_on_three_when_three_reach_the_minimum():
    # paused on one phase, three would get 6.5 A: back to three without the hold
    phases, since, reason = _decide(on=ONE, since=0.0, now=1000.0, potential=6.5, above_s=0.0, resumable=True)
    assert (phases, since) == (THREE, 1000.0) and "resumes on three" in reason
    # three would not reach the minimum: stays on one (no flip-flop while paused for lack of room)
    assert _decide(on=ONE, since=0.0, potential=5.0, resumable=True)[0] == ONE
    # charging: the hold still applies
    assert _decide(on=ONE, since=0.0, now=1000.0, potential=9.0, above_s=0.0, resumable=False)[0] == ONE


def test_the_chargers_own_stop_for_a_switch_does_not_switch_it_back():
    """The third site's go-e, 2026-11-05 18:00: put on one phase at 18:00:01,
    it stopped to switch (SuspendedEVSE, so "not charging") and three phases
    would have got 7.4 A - it was switched back at 18:00:05. Within the
    interval nothing changes, in either direction."""
    phases, since, reason = _decide(on=ONE, since=996.0, now=1000.0, potential=7.4, resumable=True)
    assert (phases, since) == (ONE, 996.0) and "phase count kept" in reason
    # nor straight back to one after a return to three (18:01:33, 90 s later)
    assert _decide(on=THREE, since=910.0, now=1000.0, permit=0.0, single_permit=lambda: 16.0)[:2] == (THREE, 910.0)
    # once the interval is over both directions work again
    assert _decide(on=ONE, since=399.0, now=1000.0, potential=7.4, resumable=True)[0] == THREE
    assert _decide(on=THREE, since=399.0, now=1000.0, permit=0.0, single_permit=lambda: 16.0)[0] == ONE


def test_a_charger_that_is_not_charging_does_not_wait_out_the_interval():
    """The third site, 2026-11-20: Nova, paused on three phases beside Stara on
    one, would have got 9 A on one phase under Shared but waited 15 minutes.
    ``unspaced`` (not charging, no switch of ours under way - the caller's
    call) lifts the interval in both directions and for the forced mode."""
    kept = _decide(on=THREE, since=910.0, permit=0.0, single_permit=lambda: 9.0)
    assert kept[0] == THREE and "phase count kept" in kept[2]
    phases, since, _ = _decide(on=THREE, since=910.0, permit=0.0, single_permit=lambda: 9.0, unspaced=True)
    assert (phases, since) == (ONE, 1000.0)
    assert _decide(on=ONE, since=910.0, potential=7.4, resumable=True, unspaced=True)[0] == THREE
    assert _decide(on=THREE, since=910.0, forced_one=True, unspaced=True)[0] == ONE
    # three still fit: nothing to change
    assert _decide(on=THREE, since=910.0, permit=8.0, unspaced=True)[:2] == (THREE, 910.0)


def _two_chargers(cap_w, stara_phases=THREE, nova_phases=ONE, drawing=True):
    """The third site on 2026-11-20: Stara (6-20 A) and Nova (6-16 A), both
    switchable with their L1 on the same site phase, an idle house, Shared."""
    stara = LoadContext(
        load_id="stara", entity_id="charger", min_current=6, max_current=20,
        phases=3, connector_status="Charging", phase_switchable=True, priority=1,
    )
    nova = LoadContext(
        load_id="nova", entity_id="charger_nova", min_current=6, max_current=16,
        phases=3, connector_status="SuspendedEVSE", phase_switchable=True, priority=2,
    )
    set_phases(stara, stara_phases)
    set_phases(nova, nova_phases)
    if drawing:
        stara.l1_current = 6.0
        if stara_phases == THREE:
            stara.l2_current = stara.l3_current = 6.0
    site = SiteContext(
        voltage=230, main_breaker_rating=20,
        consumption=PhaseValues(0.0, 0.0, 0.0), export_current=PhaseValues(0.0, 0.0, 0.0),
        max_grid_import_power=cap_w, distribution_mode=DISTRIBUTION_MODE_SHARED,
        loads=[stara, nova],
    )
    calculate_all_load_targets(site)
    return site, stara, nova


def test_three_phases_at_the_minimum_leave_nothing_for_the_second_charger():
    """18:48: 4.4 kW of import allowance, Stara on three phases at 6 A takes
    4.1 kW of it and Nova, on one phase, is paused. On one phase both run."""
    site, stara, nova = _two_chargers(4400)
    assert stara.available_current >= 6 and nova.available_current < 6
    single, spared = spared_by_one_phase(site, "stara")
    assert single >= 6 and spared == "charger_nova"
    # the trial leaves the real site alone
    assert stara.active_phases_mask == "ABC" and nova.available_current < 6


def test_no_trial_when_nothing_waits():
    site, stara, nova = _two_chargers(4400)
    nova.connector_status = "Available"
    assert spared_by_one_phase(site, "stara") == (0.0, None)


def test_going_back_to_three_phases_would_pause_the_second_charger():
    """18:46: both on one phase at about 9.5 A; Stara back on three would
    leave Nova without its minimum."""
    site, stara, nova = _two_chargers(4400, stara_phases=ONE)
    assert stara.available_current >= 6 and nova.available_current >= 6
    assert paused_by_three_phases(site, "stara") == "charger_nova"
    # with room for both, three phases pause nobody
    site, stara, nova = _two_chargers(9000, stara_phases=ONE)
    assert paused_by_three_phases(site, "stara") is None


def test_a_sharing_site_narrows_to_one_phase_for_a_waiting_load():
    spares = lambda: (9.5, "charger_nova")
    phases, since, reason = _decide(on=THREE, permit=6.2, spares=spares, sharing=True)
    assert (phases, since) == (ONE, 1000.0) and "charger_nova" in reason
    # nothing waits, or one phase would not carry this charger: three stay
    assert _decide(on=THREE, permit=6.2, spares=lambda: (0.0, None), sharing=True)[0] == THREE
    assert _decide(on=THREE, permit=6.2, spares=lambda: (4.0, "charger_nova"), sharing=True)[0] == THREE
    # within the interval nothing changes
    assert _decide(on=THREE, since=910.0, permit=6.2, spares=spares, sharing=True)[0] == THREE


def test_under_a_priority_mode_it_narrows_only_when_three_phases_gain_little():
    # three at 6.2 A = 4.3 kW against one at 16 A = 3.7 kW: +0.6 kW, under the margin
    assert _decide(on=THREE, permit=6.2, spares=lambda: (16.0, "charger_nova"))[0] == ONE
    # three at 10 A = 6.9 kW against 3.7 kW: its rank keeps it on three
    assert _decide(on=THREE, permit=10.0, spares=lambda: (16.0, "charger_nova"))[0] == THREE


def test_a_sharing_site_stays_on_one_phase_while_three_would_pause_another():
    blocked = lambda: "charger_nova"
    phases, since, reason = _decide(on=ONE, since=5.0, permit=9.9, potential=14.3,
                                    three_pauses=blocked, sharing=True)
    assert (phases, since) == (ONE, 5.0) and "would pause charger_nova" in reason
    assert _decide(on=ONE, since=5.0, permit=9.9, potential=14.3, three_pauses=lambda: None, sharing=True)[0] == THREE
    # not charging: no return to three either
    assert _decide(on=ONE, since=5.0, potential=7.4, resumable=True, three_pauses=blocked, sharing=True)[0] == ONE
    # a priority mode: the gain decides, as before
    assert _decide(on=ONE, since=5.0, permit=9.9, potential=14.3, three_pauses=blocked)[0] == THREE


def test_a_running_load_keeps_its_minimum_through_a_small_shortfall():
    """18:48: the import allowance wobbled around Stara's 18 A of minimum
    (three phases x 6 A) and its permit flipped between 6 A and nothing."""
    # 4100 W: 5.94 A a leg, 0.06 A short - a running charger keeps its place
    _, stara, _ = _two_chargers(4100)
    assert stara.available_current >= 6
    # one that draws nothing does not
    _, stara, _ = _two_chargers(4100, drawing=False)
    assert stara.available_current < 6
    # more than half an amp short: paused
    _, stara, _ = _two_chargers(3700)
    assert stara.available_current < 6


def test_a_breaker_is_never_overbooked_for_a_running_load():
    site, stara, nova = _two_chargers(20000)
    site.consumption = PhaseValues(14.3, 0.0, 0.0)   # 5.7 A left on A, under the minimum
    calculate_all_load_targets(site)
    assert stara.available_current < 6


def test_held_keeps_the_phase_count():
    assert _decide(on=ONE, since=5.0, held=True, potential=16.0)[:2] == (ONE, 5.0)
    assert _decide(on=THREE, held=True, permit=0.0, single_permit=lambda: 16.0)[0] == THREE


def test_no_car_goes_back_to_three_without_holding_up_the_next_session():
    assert _decide(on=ONE, since=5.0, connected=False)[:2] == (THREE, 5.0)
    assert _decide(on=THREE, since=5.0, connected=False)[:2] == (THREE, 5.0)
    # unplugged within the interval: still back to three (nothing draws)
    assert _decide(on=ONE, since=990.0, connected=False)[0] == THREE



def test_forced_one_phase_stays_on_one_and_waits_out_the_interval():
    # the select on One phase: three would reach 16 A, still one
    assert _decide(on=ONE, since=5.0, permit=16.0, potential=16.0, forced_one=True)[:2] == (ONE, 5.0)
    # from three with a car: within the interval it waits and says so, then one
    phases, _, reason = _decide(on=THREE, since=900.0, now=1000.0, permit=16.0, forced_one=True)
    assert phases == THREE and "set to one phase" in reason
    assert _decide(on=THREE, since=0.0, now=1000.0, permit=16.0, forced_one=True)[:2] == (ONE, 1000.0)
    # no car: one at once, so the next session starts on one
    assert _decide(on=THREE, since=990.0, now=1000.0, connected=False, forced_one=True)[0] == ONE
    assert _decide(on=ONE, since=5.0, connected=False, forced_one=True)[0] == ONE

@pytest.mark.parametrize("phases, mask, potential", [(ONE, "A", "ABC"), (THREE, "ABC", "")])
def test_set_phases(phases, mask, potential):
    _, nova = _site()
    set_phases(nova, phases)
    assert (nova.phases, nova.active_phases_mask, nova.potential_phases_mask) == (phases, mask, potential)
