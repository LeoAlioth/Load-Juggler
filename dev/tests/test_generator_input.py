"""A backup generator on the grid input of an off-grid site (JUG-13).

Machine-authored tests - not yet human-reviewed.
"""

from types import SimpleNamespace

from custom_components.dynamic_ocpp_evse.calculations import PhaseValues
from custom_components.dynamic_ocpp_evse.calculations.models import SiteContext
from custom_components.dynamic_ocpp_evse.calculations.target_calculator import (
    _calculate_grid_limit,
    _calculate_site_limit,
)
from custom_components.dynamic_ocpp_evse.const import (
    CONF_GENERATOR_RATING,
    CONF_GRID_INPUT_IS_GENERATOR,
)
from custom_components.dynamic_ocpp_evse.engine import hub_calculation
from custom_components.dynamic_ocpp_evse.engine.readers import _UNAVAILABLE

V = 230.0


def _site(rating=6.0, current=PhaseValues(2.0, None, None), **kw):
    """Single-phase off-grid site with a 1 kW pack."""
    return SiteContext(
        voltage=V,
        main_breaker_rating=25,
        consumption=PhaseValues(0.0, None, None),
        export_current=PhaseValues(0.0, None, None),
        is_off_grid=True,
        battery_soc=60.0,
        battery_soc_min=20.0,
        battery_max_discharge_power=1000.0,
        generator_rating=rating,
        generator_current=current,
        **kw,
    )


def test_the_generator_pool_is_its_rating_less_what_it_carries():
    assert _calculate_grid_limit(_site()).A == 4.0


def test_a_generator_at_its_rating_leaves_nothing():
    assert _calculate_grid_limit(_site(current=PhaseValues(7.0, None, None))).A == 0


def test_the_main_breaker_is_not_the_cap():
    assert _calculate_grid_limit(_site(current=PhaseValues(0.0, None, None))).A == 6.0


def test_an_unavailable_reading_is_plain_off_grid():
    # Today's off-grid result: no grid pool at all, the same as a site with no generator.
    for site in (_site(current=None), _site(rating=0.0)):
        limits = _calculate_grid_limit(site)
        assert (limits.A, limits.B, limits.C, limits.ABC) == (0, 0, 0, 0)
    off = _site(current=None)
    plain = _site(rating=0.0)
    assert _calculate_site_limit(off)[0].ABC == _calculate_site_limit(plain)[0].ABC


def test_a_reading_without_a_rating_changes_nothing():
    assert _calculate_grid_limit(_site(rating=0.0)).A == 0


def test_a_phase_the_site_does_not_have_gets_nothing():
    site = _site(current=PhaseValues(1.0, 1.0, None))  # consumption exists on A only
    limits = _calculate_grid_limit(site)
    assert (limits.A, limits.B) == (5.0, 0)


def test_the_power_buffer_comes_off_the_generator_pool_too():
    # 230 W buffer over one phase = 1 A.
    assert _calculate_grid_limit(_site(power_buffer=230.0)).A == 3.0


def test_the_pool_adds_to_the_inverter_half():
    pool, grid, inverter = _calculate_site_limit(_site(current=PhaseValues(1.0, None, None), battery_power=0.0, household_consumption_total=230.0))
    assert grid.A == 5.0 and pool.A == grid.A + inverter.A and inverter.A > 0


def test_grid_tied_sites_ignore_the_generator_fields():
    site = _site()
    site.is_off_grid = False
    site.consumption = PhaseValues(10.0, None, None)
    assert _calculate_grid_limit(site).A == 15.0  # the main breaker, as ever


# --- the engine edge -------------------------------------------------------


def _engine(monkeypatch, options, grid):
    monkeypatch.setattr(hub_calculation, "_read_grid_phases", lambda *_: list(grid))
    monkeypatch.setattr(
        hub_calculation, "get_entry_value",
        lambda entry, key, default=None: entry.options.get(key, default),
    )
    monkeypatch.setattr(hub_calculation, "get_inverters_for_hub", lambda *_: [])
    return SimpleNamespace(entry_id="h", options=options)


def test_a_generator_hub_is_never_grid_tied(monkeypatch):
    hub = _engine(monkeypatch, {CONF_GRID_INPUT_IS_GENERATOR: True}, [1.5, None, None])
    phases, has_cts, gridless = hub_calculation._read_site_phases(None, hub, V)
    assert not has_cts and phases == [0.0, None, None]


def test_a_grid_hub_still_reads_its_cts(monkeypatch):
    hub = _engine(monkeypatch, {}, [1.5, None, None])
    phases, has_cts, _ = hub_calculation._read_site_phases(None, hub, V)
    assert has_cts and phases == [1.5, None, None]


def test_the_generator_reading_is_taken_apart_from_the_cts(monkeypatch):
    hub = _engine(monkeypatch, {CONF_GRID_INPUT_IS_GENERATOR: True, CONF_GENERATOR_RATING: 6}, [2.0, None, None])
    rating, current = hub_calculation._read_generator(None, hub, V, {})
    assert rating == 6.0 and (current.a, current.b, current.c) == (2.0, None, None)


def test_an_unreadable_generator_reading_means_the_generator_is_off(monkeypatch):
    hub = _engine(monkeypatch, {CONF_GRID_INPUT_IS_GENERATOR: True}, [_UNAVAILABLE, None, None])
    ema = {"generator_0": 3.0}
    rating, current = hub_calculation._read_generator(None, hub, V, ema)
    assert rating == 6.0 and current is None
    assert "generator_0" not in ema  # not held: the next start reads fresh


def test_a_negative_reading_is_no_load(monkeypatch):
    hub = _engine(monkeypatch, {CONF_GRID_INPUT_IS_GENERATOR: True}, [-1.0, None, None])
    _, current = hub_calculation._read_generator(None, hub, V, {})
    assert current.a == 0.0


def test_not_a_generator_hub_reads_none(monkeypatch):
    hub = _engine(monkeypatch, {}, [2.0, None, None])
    assert hub_calculation._read_generator(None, hub, V, {}) == (0.0, None)
