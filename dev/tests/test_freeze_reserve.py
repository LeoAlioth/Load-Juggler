"""Off-grid, below the minimum SOC the battery carries the tanks and heaters -
at their away setpoint - down to the hub's freeze floor, and nothing else
(Anze, 2026-10-09: Kozolec's battery regions)."""

from custom_components.dynamic_ocpp_evse.calculations.models import (
    LoadContext,
    PhaseValues,
    SiteContext,
)
from custom_components.dynamic_ocpp_evse.calculations.target_calculator import (
    calculate_all_load_targets,
)
from custom_components.dynamic_ocpp_evse.const import (
    BEHAVIOR_FULL_POWER,
    DEVICE_TYPE_EVSE,
    DEVICE_TYPE_HOT_WATER_TANK,
    TANK_MODE_NORMAL,
)
from custom_components.dynamic_ocpp_evse.control.hot_water_tank import (
    resolve_tank_setpoint,
)
from custom_components.dynamic_ocpp_evse.engine.hub_calculation import _latch_floor

V = 230.0


def _load(eid, device_type, amps):
    return LoadContext(
        load_id=eid, entity_id=eid, min_current=amps, max_current=amps,
        phases=1, priority=1, device_type=device_type,
        operating_mode="Normal", mode_behavior=BEHAVIOR_FULL_POWER,
        mode_priority=1, active_phases_mask="A", l1_phase="A",
        connector_status="Charging",
    )


def _run(soc, freeze_floor=5.0):
    """A night: no sun, a 300 W house on a 5 kW battery, the minimum at 10 %.
    A 2 kW tank and a car asking 6 A, both on Continuous-like full power."""
    tank = _load("tank", DEVICE_TYPE_HOT_WATER_TANK, 8.7)
    car = _load("car", DEVICE_TYPE_EVSE, 6.0)
    site = SiteContext(
        voltage=V, main_breaker_rating=0,
        grid_current=PhaseValues(0.0, None, None),
        consumption=PhaseValues(300 / V, None, None),
        export_current=PhaseValues(0.0, None, None),
        solar_production_total=0.0,
        household_consumption_total=300.0,
        battery_soc=soc, battery_power=300.0,
        battery_soc_min=10.0, battery_soc_target=50.0,
        battery_soc_freeze_floor=freeze_floor,
        battery_max_discharge_power=5000.0, inverter_max_power=5000.0,
        inverter_supports_asymmetric=True,
        is_off_grid=True, loads=[tank, car], circuit_groups=[],
    )
    calculate_all_load_targets(site)
    return tank.available_current, car.available_current  # the permits


def test_between_floor_and_minimum_the_battery_carries_the_tank_alone():
    tank, car = _run(soc=8.0)
    assert tank == 8.7
    assert car == 0


def test_below_the_floor_the_tank_gets_nothing_from_the_battery():
    assert _run(soc=4.0) == (0, 0)


def test_without_a_floor_below_the_minimum_nothing_draws_the_battery():
    assert _run(soc=8.0, freeze_floor=None) == (0, 0)


def test_above_the_minimum_both_run():
    assert _run(soc=30.0) == (8.7, 6.0)


def test_below_the_minimum_every_tank_mode_holds_away():
    hub = {"battery_soc": 8, "battery_soc_min": 10, "battery_soc_target": 50,
           "excess_available": True}
    assert resolve_tank_setpoint(TANK_MODE_NORMAL.key, 15, 50, 70, 2000, hub) == (15, "away")


def test_the_floor_stops_at_it_and_resumes_a_hysteresis_above():
    rt = {}
    assert _latch_floor(rt, "k", 6.0, 5.0, 2.0) == (7.0, False)   # not yet resumed
    assert _latch_floor(rt, "k", 7.0, 5.0, 2.0) == (5.0, True)    # resumed at 7
    assert _latch_floor(rt, "k", 5.5, 5.0, 2.0) == (5.0, True)    # held down to 5
    assert _latch_floor(rt, "k", 5.0, 5.0, 2.0) == (7.0, False)   # stopped at 5
