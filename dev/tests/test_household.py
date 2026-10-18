"""The one household sum: ``grid + Σ (output − input) − draws``.

Machine-authored tests - not yet human-reviewed.

``household_per_phase`` knows no wiring, site type or off-grid flag. Every
arrangement of inverters - a PV inverter on the grid-meter bus, a hybrid the
grid flows through, a hybrid with input sensors, a PV inverter on a hybrid's
load port, a panel on the grid bus ahead of a hybrid, off-grid - must give the
physical household per phase from the same expression.
"""

import pytest

from custom_components.dynamic_ocpp_evse.calculations import PhaseValues
from custom_components.dynamic_ocpp_evse.calculations.models import SiteContext
from custom_components.dynamic_ocpp_evse.calculations.utils import household_per_phase
from custom_components.dynamic_ocpp_evse.engine import fleet
from custom_components.dynamic_ocpp_evse.engine.fleet import FleetMember

from .run_tests import simulate_fleet

V = 230.0


# --- household_per_phase on its own ----------------------------------------------

def test_household_is_grid_plus_net_minus_draws():
    household = household_per_phase(
        [2.0, -1.0, 4.0], PhaseValues(10.0, 10.0, 10.0), [3.0, 3.0, 3.0]
    )
    assert (household.a, household.b, household.c) == (9.0, 6.0, 11.0)


def test_household_is_none_without_output_sensors():
    assert household_per_phase([2.0, 2.0, 2.0], None, [0.0, 0.0, 0.0]) is None


def test_household_keeps_a_missing_grid_phase_missing():
    household = household_per_phase(
        [2.0, None, None], PhaseValues(10.0, 10.0, None), [1.0, 1.0, 1.0]
    )
    assert household.a == 11.0
    assert household.b is None and household.c is None


def test_household_clamps_at_zero():
    household = household_per_phase(
        [-4.0, 0.0, 0.0], PhaseValues(1.0, 1.0, 1.0), [5.0, 5.0, 5.0]
    )
    assert household.a == 0 and household.b == 0 and household.c == 0


def test_a_phase_without_a_net_reading_counts_the_grid_alone():
    household = household_per_phase(
        [3.0, 3.0, 3.0], PhaseValues(None, 2.0, None), [1.0, 1.0, 1.0]
    )
    assert (household.a, household.b, household.c) == (2.0, 4.0, 2.0)


def test_off_grid_zeros_reduce_it_to_output_less_draws():
    # Off-grid the grid is 0 on the phases with outputs, None elsewhere.
    household = household_per_phase(
        [0.0, 0.0, None], PhaseValues(8.0, 5.0, None), [2.0, 1.0, 0.0]
    )
    assert (household.a, household.b, household.c) == (6.0, 4.0, None)


# --- The telescoping property ------------------------------------------------------
#
# The same physical site - 3 A of household a phase, our loads drawing 5 A a
# phase, 10 A a phase of sun - wired six ways. Whatever sits where, the sum
# lands on the physical household.

HOUSE = PhaseValues(3.0, 3.0, 3.0)
DRAWS = (5.0, 5.0, 5.0)
SOLAR_PP = 10.0

ARRANGEMENTS = {
    "a-pv-on-main": ({}, [{"kind": "pv"}]),
    "b-hybrid-grid-tick": ({}, [{"kind": "hybrid", "input": "grid"}]),
    "c-hybrid-input-sensors": ({}, [{"kind": "hybrid", "input": "sensors"}]),
    "d-hybrid-plus-unbacked-panel": (
        {"main_bus_share": 0.4},
        [{"kind": "hybrid", "input": "sensors"}],
    ),
    "e-hybrid-plus-pv-on-main": (
        {},
        [
            {"kind": "hybrid", "input": "sensors"},
            {"kind": "pv", "solar_share": 0.5},
        ],
    ),
    "f-pv-on-hybrid-load-port": (
        {},
        [
            {"kind": "hybrid", "input": "sensors"},
            {"kind": "pv", "feeds": "port", "solar_share": 0.5},
        ],
    ),
}


def _household_of(site_data, arrangement):
    # The physical grid: what the house and our loads take, less the sun.
    grid = [h + d - SOLAR_PP for h, d in zip((3.0, 3.0, 3.0), DRAWS)]
    # PV-only arrangements put the sun on the bus; hybrids carry it through
    # (the harness gives each its share).
    solar_pp = SOLAR_PP
    site = SiteContext(voltage=V, battery_soc=None)
    members = simulate_fleet(arrangement, site_data, site, HOUSE, DRAWS, grid, solar_pp)
    net = fleet.net_output(members, grid)
    return household_per_phase(grid, net, DRAWS)


@pytest.mark.parametrize("name", sorted(ARRANGEMENTS))
def test_every_arrangement_gives_the_physical_household(name):
    site_data, arrangement = ARRANGEMENTS[name]
    household = _household_of(site_data, arrangement)
    assert (household.a, household.b, household.c) == pytest.approx((3.0, 3.0, 3.0))


def test_the_six_arrangements_agree_with_each_other():
    results = {
        name: _household_of(*ARRANGEMENTS[name]) for name in ARRANGEMENTS
    }
    first = next(iter(results.values()))
    for name, household in results.items():
        assert household.a == pytest.approx(first.a), name
        assert household.b == pytest.approx(first.b), name
        assert household.c == pytest.approx(first.c), name


def test_the_hybrid_reads_negative_while_the_pv_on_its_port_out_produces():
    # arr-f: the hybrid's load port carries 3 + 5 - 5 = 3 A; with the PV there
    # making 10 A * 0.5 = 5 A the port reads below the house - here exactly
    # 3 A. Push the PV to its whole 10 A and the parent reads negative.
    grid = [3.0 + 5.0 - SOLAR_PP] * 3
    arrangement = [
        {"kind": "hybrid", "input": "sensors"},
        {"kind": "pv", "feeds": "port", "solar_share": 1.0},
    ]
    members = simulate_fleet(
        arrangement, {}, SiteContext(voltage=V, battery_soc=None),
        HOUSE, DRAWS, grid, SOLAR_PP,
    )
    assert members[0].output.a < 0
    net = fleet.net_output(members, grid)
    assert household_per_phase(grid, net, DRAWS).a == pytest.approx(3.0)


# --- A phase remap ---------------------------------------------------------------------

def test_a_grid_input_member_gives_output_less_draws_under_a_phase_remap():
    # Grid CT remaps permute the grid phases after the site is built; the
    # inverter outputs are not remapped. A member whose input IS the grid
    # cancels it per phase, whatever the permutation: out - draws, exactly.
    hybrid = FleetMember(
        entry_id="h",
        output=PhaseValues(10.0, 8.0, 6.0),
        output_raw=PhaseValues(10.0, 8.0, 6.0),
        grid_input=True,
    )
    draws = [1.0, 2.0, 3.0]
    for grid in ([1.0, 5.0, 2.0], [5.0, 2.0, 1.0], [2.0, 1.0, 5.0]):
        net = fleet.net_output([hybrid], grid)
        household = household_per_phase(grid, net, draws)
        assert (household.a, household.b, household.c) == pytest.approx(
            (10.0 - 1.0, 8.0 - 2.0, 6.0 - 3.0)
        )
