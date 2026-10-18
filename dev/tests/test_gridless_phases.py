"""A phase an inverter feeds but no grid CT reads (JUG-12), on a site with CTs elsewhere.

Machine-authored tests - not yet human-reviewed.
"""

from types import SimpleNamespace

from custom_components.dynamic_ocpp_evse.calculations import PhaseValues
from custom_components.dynamic_ocpp_evse.calculations.models import SiteContext
from custom_components.dynamic_ocpp_evse.calculations.target_calculator import _calculate_grid_limit
from custom_components.dynamic_ocpp_evse.calculations.utils import grid_without_managed_draws
from custom_components.dynamic_ocpp_evse.engine import hub_calculation
from custom_components.dynamic_ocpp_evse.const import (
    CONF_INVERTER_OUTPUT_PHASE_A_ENTITY_ID,
    CONF_INVERTER_OUTPUT_PHASE_B_ENTITY_ID,
    CONF_INVERTER_OUTPUT_PHASE_C_ENTITY_ID,
)

V = 230.0


def _site(gridless):
    # A 3-phase inverter, a grid CT on A only: B and C read a synthetic 0 A.
    return SiteContext(
        voltage=V,
        main_breaker_rating=25,
        consumption=PhaseValues(2.0, 0.0, 0.0),
        export_current=PhaseValues(0.0, 0.0, 0.0),
        gridless_phases=gridless,
    )


def test_a_phase_with_no_grid_ct_gets_no_grid_headroom():
    limits = _calculate_grid_limit(_site((False, True, True)))
    assert (limits.A, limits.B, limits.C) == (23.0, 0, 0)


def test_without_the_mask_the_phantom_phases_got_the_whole_breaker():
    limits = _calculate_grid_limit(_site((False, False, False)))
    assert (limits.A, limits.B, limits.C) == (23.0, 25.0, 25.0)


def test_the_managed_draw_is_not_taken_off_a_phase_with_no_grid_ct():
    cons, exp = grid_without_managed_draws(
        PhaseValues(2.0, 0.0, 0.0), PhaseValues(0.0, 0.0, 0.0), (1.0, 8.0, 8.0),
        (False, True, True),
    )
    # A is rebuilt as before; B and C would otherwise show 8 A of export from nowhere.
    assert (cons.a, cons.b, cons.c) == (1.0, 0.0, 0.0)
    assert (exp.a, exp.b, exp.c) == (0.0, 0.0, 0.0)
    _, plain = grid_without_managed_draws(
        PhaseValues(2.0, 0.0, 0.0), PhaseValues(0.0, 0.0, 0.0), (1.0, 8.0, 8.0)
    )
    assert plain.b == 8.0


def _read(monkeypatch, grid, inverter_phases):
    monkeypatch.setattr(hub_calculation, "_read_grid_phases", lambda *_: list(grid))
    inverter = SimpleNamespace(
        data={}, options={c: "sensor.x" for c in inverter_phases}
    )
    monkeypatch.setattr(hub_calculation, "get_inverters_for_hub", lambda *_: [inverter])
    monkeypatch.setattr(
        hub_calculation, "get_entry_value",
        lambda entry, key, default=None: entry.options.get(key, default),
    )
    return hub_calculation._read_site_phases(None, SimpleNamespace(entry_id="h"), V)


ABC = (
    CONF_INVERTER_OUTPUT_PHASE_A_ENTITY_ID,
    CONF_INVERTER_OUTPUT_PHASE_B_ENTITY_ID,
    CONF_INVERTER_OUTPUT_PHASE_C_ENTITY_ID,
)


def test_the_mask_marks_inverter_phases_without_a_ct_on_a_grid_site(monkeypatch):
    phases, has_cts, gridless = _read(monkeypatch, [1.5, None, None], ABC)
    assert phases == [1.5, 0.0, 0.0] and has_cts
    assert gridless == (False, True, True)


def test_off_grid_has_no_mask(monkeypatch):
    phases, has_cts, gridless = _read(monkeypatch, [None, None, None], ABC)
    assert phases == [0.0, 0.0, 0.0] and not has_cts
    assert gridless == (False, False, False)


def test_a_one_phase_array_on_a_three_phase_grid_has_no_mask(monkeypatch):
    _, _, gridless = _read(monkeypatch, [1.0, 1.0, 1.0], ABC[:1])
    assert gridless == (False, False, False)
