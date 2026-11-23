"""Phases are billed: individually / sum of all phases (GitHub issue 23).

Machine-authored tests - not yet human-reviewed.

The reporter's site: a three-phase grid and solar, a single-phase charger, and
a meter that sums the phases - 1 kW exported on each phase while the car draws
3 kW on one is billed as nothing. Billed individually (the default) a
single-phase Solar or Excess load may use only its own phase's surplus; summed,
it may use the site's NET surplus. Breakers still bound each phase.
"""

from custom_components.dynamic_ocpp_evse.calculations.models import (
    LoadContext,
    PhaseValues,
    SiteContext,
)
from custom_components.dynamic_ocpp_evse.calculations.target_calculator import (
    _excess_phase_is_importing,
    calculate_all_load_targets,
)
from custom_components.dynamic_ocpp_evse.config_flow.schemas import HUB_EXPORT_KEYS
from custom_components.dynamic_ocpp_evse.const import (
    CONF_PHASE_BILLING,
    DEFAULT_PHASE_BILLING,
    PHASE_BILLING_INDIVIDUAL,
)
from custom_components.dynamic_ocpp_evse.const.common import BEHAVIOR_SOLAR_ONLY

V = 230.0


def _car(phase="A"):
    return LoadContext(
        load_id="car", entity_id="charger", min_current=6, max_current=16, phases=1,
        l1_phase=phase, operating_mode="Solar Only", mode_behavior=BEHAVIOR_SOLAR_ONLY,
        mode_priority=3, connector_status="Charging",
    )


def _site(export, import_, summed, breaker=25):
    """Per-phase export and household import in amps (post-feedback)."""
    site = SiteContext(
        voltage=V,
        main_breaker_rating=breaker,
        consumption=PhaseValues(*import_),
        export_current=PhaseValues(*export),
        phases_summed_for_billing=summed,
    )
    site.loads = [_car()]
    calculate_all_load_targets(site)
    return site, site.loads[0]


def test_billed_individually_a_single_phase_car_sees_only_its_own_phase():
    """1 kW exported on each phase: 4.3 A on phase A, under the 6 A minimum."""
    _, car = _site((4.35, 4.35, 4.35), (0, 0, 0), summed=False)
    assert car.available_current < 6


def test_summed_a_single_phase_car_takes_the_surplus_of_all_three():
    """The reporter's example: 3 kW of surplus, 13 A on one phase."""
    _, car = _site((4.35, 4.35, 4.35), (0, 0, 0), summed=True)
    assert 12.5 <= car.available_current <= 13.1


def test_summed_an_importing_phase_cancels_the_others_export():
    """A and B export 8 A each, C imports 10 A: the site's net surplus is 6 A."""
    _, car = _site((8.0, 8.0, 0.0), (0.0, 0.0, 10.0), summed=True)
    assert 5.9 <= car.available_current <= 6.1


def test_summed_the_breaker_still_bounds_the_phase():
    """30 A of net surplus, but phase A's 16 A breaker caps the car."""
    _, car = _site((10.0, 10.0, 10.0), (0, 0, 0), summed=True, breaker=16)
    assert car.available_current <= 16.0


def test_the_excess_import_guard_asks_the_site_when_phases_are_summed():
    """C imports 2 A while A and B export 3 A each: billed individually a load on
    C is buying; summed, the site is not."""
    for summed, buying in ((False, True), (True, False)):
        site = SiteContext(
            voltage=V, main_breaker_rating=25,
            consumption=PhaseValues(0.0, 0.0, 2.0),
            export_current=PhaseValues(3.0, 3.0, 0.0),
            phases_summed_for_billing=summed,
        )
        assert _excess_phase_is_importing(_car("C"), site) is buying, summed
    # summed, but the site as a whole is buying
    site = SiteContext(
        voltage=V, main_breaker_rating=25,
        consumption=PhaseValues(0.0, 0.0, 8.0),
        export_current=PhaseValues(3.0, 3.0, 0.0),
        phases_summed_for_billing=True,
    )
    assert _excess_phase_is_importing(_car("C"), site) is True


def test_the_option_is_on_the_export_page_and_defaults_to_individually():
    assert CONF_PHASE_BILLING in HUB_EXPORT_KEYS
    assert DEFAULT_PHASE_BILLING == PHASE_BILLING_INDIVIDUAL
