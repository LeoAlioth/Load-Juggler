"""lj-7 fix A: a running binary load is allocated in every distribution mode.

Judge the allocation on the grid it is given: the minimum is rounded to the same
0.1 A, or a tank rated 900 W on three phases (1.304 A) came out at 1.3 A, under its
own minimum, and got nothing in the two Sequential modes. (Fix B, a denied tank idle
at its floor target, is tested in test_hot_water_tank.py.)
"""

from custom_components.dynamic_ocpp_evse.calculations.models import (
    LoadContext,
    PhaseValues,
    SiteContext,
)
from custom_components.dynamic_ocpp_evse.calculations.target_calculator import (
    calculate_all_load_targets,
)
from custom_components.dynamic_ocpp_evse.const import (
    DISTRIBUTION_MODE_SEQUENTIAL_OPTIMIZED,
    DISTRIBUTION_MODE_SEQUENTIAL_STRICT,
    DISTRIBUTION_MODE_SHARED,
    DISTRIBUTION_MODE_PRIORITY,
)


# ---------------------------------------------------------------------------
# Fix A: binary load allocation in Sequential modes (rounding bug)
# ---------------------------------------------------------------------------

def _tank_load(entity_id, min_current, max_current, phases=3):
    return LoadContext(
        load_id=entity_id,
        entity_id=entity_id,
        min_current=min_current,
        max_current=max_current,
        phases=phases,
        priority=1,
        operating_mode="solar_priority",
    )


def _site(available, loads, mode):
    return SiteContext(
        voltage=230.0,
        main_breaker_rating=available,
        consumption=PhaseValues(0.6, 0.6, 0.6),  # 0.6 A house
        export_current=PhaseValues(0.0, 0.0, 0.0),
        distribution_mode=mode,
        loads=loads,
    )


def test_a_running_tank_is_allocated_in_every_mode():
    """A 900 W / 3 ph tank (min_current = max_current = 900 / 690 = 1.3043...)
    is allocated 1.3 A in all four modes. Before the fix, Optimized and Strict
    gave it 0 because the rounded allocation (1.3) was compared with the
    unrounded minimum (1.304...) and judged under."""
    rated = 900.0 / 690.0  # ~1.3043
    tank = _tank_load("tank_1", rated, rated, 3)
    tank.connector_status = "Charging"
    tank.l1_current = tank.l2_current = tank.l3_current = rated
    site = _site(20.0, [tank], DISTRIBUTION_MODE_SHARED)
    calculate_all_load_targets(site)
    assert tank.allocated_current == round(rated, 1)

    for mode in (DISTRIBUTION_MODE_PRIORITY,
                 DISTRIBUTION_MODE_SEQUENTIAL_OPTIMIZED,
                 DISTRIBUTION_MODE_SEQUENTIAL_STRICT):
        tank2 = _tank_load("tank_2", rated, rated, 3)
        tank2.connector_status = "Charging"
        tank2.l1_current = tank2.l2_current = tank2.l3_current = rated
        site2 = _site(20.0, [tank2], mode)
        calculate_all_load_targets(site2)
        assert tank2.allocated_current == round(rated, 1), f"Failed in {mode}: got {tank2.allocated_current}"


def test_a_1500w_plug_on_one_phase_is_allocated():
    """A 1500 W / 1 ph plug (6.5217 A) is allocated 6.5 A in all modes.
    Before the fix, Optimized and Strict gave it 0 (6.5 < 6.5217)."""
    rated = 1500.0 / 230.0  # ~6.5217
    plug = _tank_load("plug_1", rated, rated, 1)
    plug.active_phases_mask = "A"
    plug.connector_status = "Charging"
    plug.l1_current = rated
    site = _site(20.0, [plug], DISTRIBUTION_MODE_SHARED)
    calculate_all_load_targets(site)
    assert plug.allocated_current == round(rated, 1)

    for mode in (DISTRIBUTION_MODE_PRIORITY,
                 DISTRIBUTION_MODE_SEQUENTIAL_OPTIMIZED,
                 DISTRIBUTION_MODE_SEQUENTIAL_STRICT):
        plug2 = _tank_load("plug_2", rated, rated, 1)
        plug2.active_phases_mask = "A"
        plug2.connector_status = "Charging"
        plug2.l1_current = rated
        site2 = _site(20.0, [plug2], mode)
        calculate_all_load_targets(site2)
        assert plug2.allocated_current == round(rated, 1), f"Failed in {mode}: got {plug2.allocated_current}"
