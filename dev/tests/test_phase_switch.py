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
    single_phase_permit,
)
from custom_components.dynamic_ocpp_evse.calculations.target_calculator import (
    calculate_all_load_targets,
)
from custom_components.dynamic_ocpp_evse.const import DISTRIBUTION_MODE_SEQUENTIAL_OPTIMIZED


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
    args = dict(connected=True, held=False, permit=0.0, min_current=6.0,
                single_permit=lambda: 0.0, potential=0.0, margin=2.0,
                above_s=math.inf, hold_s=300, interval_s=600)
    args.update(kw)
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


def test_back_to_three_only_after_the_interval_and_above_the_margin():
    # under minimum + margin (6 + 2 A): stays
    assert _decide(on=ONE, since=0.0, now=2000.0, potential=7.5)[0] == ONE
    # enough, but within 10 minutes of the last change: stays, and says why
    phases, _, reason = _decide(on=ONE, since=900.0, now=1000.0, potential=9.0)
    assert phases == ONE and "last change 2 min ago" in reason
    # enough and the interval is over
    assert _decide(on=ONE, since=0.0, now=601.0, potential=9.0)[:2] == (THREE, 601.0)


def test_back_to_three_only_once_the_margin_held_five_minutes():
    # interval long over, but above the margin only for 2 minutes: stays, says when
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


def test_held_keeps_the_phase_count():
    assert _decide(on=ONE, since=5.0, held=True, potential=16.0)[:2] == (ONE, 5.0)
    assert _decide(on=THREE, held=True, permit=0.0, single_permit=lambda: 16.0)[0] == THREE


def test_no_car_goes_back_to_three_without_holding_up_the_next_session():
    assert _decide(on=ONE, since=5.0, connected=False)[:2] == (THREE, 5.0)
    assert _decide(on=THREE, since=5.0, connected=False)[:2] == (THREE, 5.0)
    # unplugged within the interval: still back to three (nothing draws)
    assert _decide(on=ONE, since=990.0, connected=False)[0] == THREE


@pytest.mark.parametrize("phases, mask, potential", [(ONE, "A", "ABC"), (THREE, "ABC", "")])
def test_set_phases(phases, mask, potential):
    _, nova = _site()
    set_phases(nova, phases)
    assert (nova.phases, nova.active_phases_mask, nova.potential_phases_mask) == (phases, mask, potential)
