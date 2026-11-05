"""A charger held at its limit until a new reading keeps its room (pure).

Machine-authored tests - not yet human-reviewed.

The third site on 2026-11-05 in Priority mode: Stara (priority 1) and Nova
(priority 2), both 6-16 A three-phase, the import cap the binding limit.
While Stara waited for the first reading after a raise its maximum was lowered
to the limit in force, so the distribution handed the room it was climbing into
to Nova - 6 -> 12.5 -> 6 A with every 30 s reading of Stara's. The hold is now
a trim after the distribution (LoadContext.raise_hold).
"""

from custom_components.dynamic_ocpp_evse.calculations.models import (
    LoadContext,
    PhaseValues,
    SiteContext,
)
from custom_components.dynamic_ocpp_evse.calculations.target_calculator import (
    calculate_all_load_targets,
)
from custom_components.dynamic_ocpp_evse.const import DISTRIBUTION_MODE_PRIORITY


def _site(stara_hold=None):
    stara = LoadContext(
        load_id="stara", entity_id="charger", min_current=6, max_current=16,
        phases=3, priority=1, connector_status="Charging", raise_hold=stara_hold,
    )
    nova = LoadContext(
        load_id="nova", entity_id="charger_nova", min_current=6, max_current=16,
        phases=3, priority=2, connector_status="Charging",
    )
    site = SiteContext(
        voltage=230,
        main_breaker_rating=32,   # the import cap binds, not the breaker
        consumption=PhaseValues(0.5, 0.5, 0.5),
        export_current=PhaseValues(0.0, 0.0, 0.0),
        # room for 16 + 6 A a phase over a 0.5 A house: 22.5 A x 3 x 230 V
        max_grid_import_power=22.5 * 3 * 230,
        distribution_mode=DISTRIBUTION_MODE_PRIORITY,
        loads=[stara, nova],
    )
    return site, stara, nova


def test_without_a_hold_the_first_charger_takes_the_room_and_the_second_its_minimum():
    site, stara, nova = _site()
    calculate_all_load_targets(site)
    assert stara.available_current == 16.0
    assert nova.available_current == 6.0


def test_a_held_charger_keeps_its_room_and_the_second_stays_at_its_minimum():
    site, stara, nova = _site(stara_hold=10.0)
    calculate_all_load_targets(site)
    assert stara.available_current == 10.0   # not raised past its limit in force
    assert nova.available_current == 6.0     # was 12.0: the held room went to Nova
    assert stara.max_current == 16           # the maximum itself is untouched


def test_a_hold_above_the_share_changes_nothing():
    site, stara, nova = _site(stara_hold=20.0)
    calculate_all_load_targets(site)
    assert (stara.available_current, nova.available_current) == (16.0, 6.0)
