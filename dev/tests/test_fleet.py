"""Tests for the inverter fleet aggregation — engine.fleet.

Machine-authored tests — not yet human-reviewed.

Many inverter entries reduce to the single-inverter/single-battery scalars
SiteContext expects. The per-member gating the scalar form cannot express
happens here: charge capacity excludes members whose OWN battery is at its
OWN full-SOC; discharge capacity excludes members below the hub minimum;
fleet SOC is capacity-weighted; solar sums parallel outputs plus series
outputs minus their own battery power. With a single member every aggregate
must reduce to exactly the classic singleton value.
"""

from custom_components.dynamic_ocpp_evse.calculations import PhaseValues
from custom_components.dynamic_ocpp_evse.engine.fleet import (
    FleetMember,
    battery_power_total,
    capacity_total,
    charge_power_total,
    charging_power_total,
    discharge_power_total,
    fleet_topology,
    inverter_limits,
    forecast_device_ids,
    member_solar,
    member_solar_production,
    mixed_topologies,
    soc_full_scalar,
    solar_is_measured,
    solar_total,
    sum_outputs,
    weighted_soc,
)

V = 230.0


def _member(entry_id="inv1", **kwargs):
    return FleetMember(entry_id=entry_id, **kwargs)


def _battery(entry_id="inv1", soc=None, power=None, charge=5000.0,
             discharge=5000.0, full=97.0, capacity=10.0, **kwargs):
    return FleetMember(
        entry_id=entry_id,
        has_battery=True,
        has_battery_power_entity=power is not None,
        battery_soc=soc,
        battery_power=power,
        charge_cap=charge,
        discharge_cap=discharge,
        soc_full=full,
        capacity_kwh=capacity,
        **kwargs,
    )


# --- Fleet SOC ----------------------------------------------------------------

def test_weighted_soc_by_capacity():
    # 80% of 10 kWh and 40% of 5 kWh → (800 + 200) / 15
    members = [_battery("a", soc=80, capacity=10), _battery("b", soc=40, capacity=5)]
    assert abs(weighted_soc(members) - 1000 / 15) < 1e-9


def test_weighted_soc_falls_back_to_mean_without_capacities():
    members = [
        _battery("a", soc=80, capacity=0),
        _battery("b", soc=40, capacity=0),
    ]
    assert weighted_soc(members) == 60


def test_weighted_soc_none_without_batteries():
    assert weighted_soc([_member()]) is None


def test_single_member_soc_is_its_own():
    assert weighted_soc([_battery(soc=72.5)]) == 72.5


# --- Battery power ------------------------------------------------------------

def test_battery_power_sums_signed():
    members = [_battery("a", power=-2000), _battery("b", power=500)]
    assert battery_power_total(members) == -1500


def test_battery_power_none_without_any_power_sensor():
    # No member has a power entity → None (feeds the derived-solar gate)
    assert battery_power_total([_battery("a", soc=50, power=None)]) is None


# --- Charge capacity: per-member full gating -----------------------------------

def test_charge_cap_excludes_full_member():
    # One battery full, one empty: only the empty one's cap counts — the
    # scalar fleet form could never express this.
    members = [
        _battery("full", soc=98, full=97, charge=5000),
        _battery("empty", soc=30, full=97, charge=3000),
    ]
    assert charge_power_total(members) == 3000


def test_charge_cap_all_full_is_none():
    assert charge_power_total([_battery(soc=98, full=97)]) is None


def test_charge_cap_unknown_soc_counts_as_not_full():
    assert charge_power_total([_battery(soc=None, charge=4000)]) == 4000


def test_single_member_charge_cap_passthrough_below_full():
    assert charge_power_total([_battery(soc=80, charge=5000)]) == 5000


# --- Discharge capacity: per-member below-min exclusion -------------------------

def test_discharge_excludes_member_below_min():
    # The big full battery lifts the fleet SOC, but the small one below the
    # floor must not be counted dischargeable.
    members = [
        _battery("big", soc=90, discharge=8000, capacity=15),
        _battery("small", soc=10, discharge=3000, capacity=5),
    ]
    assert discharge_power_total(members, soc_min=20) == 8000


def test_discharge_includes_unknown_soc():
    assert discharge_power_total([_battery(soc=None, discharge=5000)], 20) == 5000


def test_discharge_all_below_min_is_none():
    assert discharge_power_total([_battery(soc=10, discharge=5000)], 20) is None


# --- Full-SOC scalar ------------------------------------------------------------

def test_soc_full_scalar_single_battery_is_its_own():
    assert soc_full_scalar([_battery(full=95), _member("noinv")]) == 95


def test_soc_full_scalar_multi_battery_is_none():
    assert soc_full_scalar([_battery("a"), _battery("b")]) is None


# --- Outputs and solar ----------------------------------------------------------

def test_sum_outputs_per_phase():
    members = [
        _member("a", output=PhaseValues(a=10.0, b=None, c=None)),
        _member("b", output=PhaseValues(a=5.0, b=4.0, c=None)),
    ]
    summed = sum_outputs(members)
    assert summed.a == 15.0
    assert summed.b == 4.0
    assert summed.c is None


def test_solar_parallel_output_is_production():
    m = _member(output=PhaseValues(a=10.0, b=None, c=None), topology="parallel")
    assert member_solar(m, V) == 10.0 * V


def test_solar_series_output_subtracts_own_battery():
    # 10 A output at 230 V = 2300 W, battery discharging 500 W → 1800 W solar
    m = _battery(
        soc=80, power=500,
        output=PhaseValues(a=10.0, b=None, c=None), topology="series",
    )
    assert member_solar(m, V) == 10.0 * V - 500


def test_solar_mixed_fleet_sums_per_member():
    par = _member("p", output=PhaseValues(a=10.0, b=None, c=None), topology="parallel")
    ser = _battery(
        "s", soc=80, power=-1000,
        output=PhaseValues(a=5.0, b=None, c=None), topology="series",
    )
    # parallel 2300 + series (1150 − (−1000)) = 2300 + 2150
    assert solar_total([par, ser], V) == 2300 + 5.0 * V + 1000


def test_solar_none_without_outputs():
    assert solar_total([_battery(soc=50)], V) is None


def test_solar_production_sensor_wins_over_output():
    """A member with its own production sensor reports it, output ignored."""
    m = _member(
        output=PhaseValues(a=10.0, b=None, c=None),
        topology="parallel",
        has_solar_entity=True,
        solar_measured=1800.0,
    )
    assert member_solar_production(m, V) == 1800.0
    assert solar_total([m], V) == 1800.0


def test_solar_mixed_measured_and_derived_are_summed():
    """One inverter with a production sensor, one with only outputs — the
    fleet total is the sum, which is why derivation is per member."""
    measured = _member("m", has_solar_entity=True, solar_measured=3000.0)
    derived = _member("d", output=PhaseValues(a=10.0, b=None, c=None))
    assert solar_total([measured, derived], V) == 3000.0 + 10.0 * V


def test_solar_is_measured_only_when_every_member_measures():
    measured = _member("m", has_solar_entity=True, solar_measured=3000.0)
    derived = _member("d", output=PhaseValues(a=10.0, b=None, c=None))
    assert solar_is_measured([measured]) is True
    assert solar_is_measured([measured, derived]) is False
    # No members at all: nothing is measured, so solar stays derived.
    assert solar_is_measured([]) is False


def test_forecast_device_ids_merge_and_dedupe():
    """Each PV array belongs to an inverter, but clipping is site-wide — the
    fleet's devices merge into one list, with shared devices counted once."""
    a = _member("a", forecast_device_ids=("east", "west"))
    b = _member("b", forecast_device_ids=("west", "north"))
    assert forecast_device_ids([a, b]) == ["east", "west", "north"]
    assert forecast_device_ids([_member("c")]) == []


def test_charging_power_total_for_fallback():
    members = [_battery("a", power=-2000), _battery("b", power=300)]
    assert charging_power_total(members) == 2000


# --- Topology --------------------------------------------------------------------

def test_topology_series_if_any_series():
    members = [
        _member("p", topology="parallel", output=PhaseValues(a=1.0, b=None, c=None)),
        _member("s", topology="series", output=PhaseValues(a=1.0, b=None, c=None)),
    ]
    assert fleet_topology(members) == "series"
    assert mixed_topologies(members)


def test_topology_uniform_not_mixed():
    members = [
        _member("a", topology="parallel", output=PhaseValues(a=1.0, b=None, c=None)),
        _member("b", topology="parallel", output=PhaseValues(a=1.0, b=None, c=None)),
    ]
    assert fleet_topology(members) == "parallel"
    assert not mixed_topologies(members)


# --- Inverter capacity ------------------------------------------------------------

def test_inverter_limits_sum_totals():
    members = [_member("a", max_power=10000.0), _member("b", max_power=6000.0)]
    max_power, _, _ = inverter_limits(members)
    assert max_power == 16000


def test_per_phase_collapse_is_min_over_fed_phases():
    # 3-phase 4 kW/ph + single-phase (A only) 3 kW/ph:
    # phase A carries 7 kW, B and C carry 4 kW → conservative scalar 4 kW.
    three_phase = _member(
        "abc", max_power_per_phase=4000.0,
        output=PhaseValues(a=1.0, b=1.0, c=1.0),
    )
    single_phase = _member(
        "a", max_power_per_phase=3000.0,
        output=PhaseValues(a=1.0, b=None, c=None),
    )
    _, per_phase, _ = inverter_limits([three_phase, single_phase])
    assert per_phase == 4000


def test_per_phase_unlimited_when_a_feeder_is_uncapped():
    capped = _member("a", max_power_per_phase=4000.0)
    uncapped = _member("b", max_power=5000.0)  # spans all phases, no per-phase cap
    _, per_phase, _ = inverter_limits([capped, uncapped])
    assert per_phase is None


def test_asymmetric_requires_all_members():
    asym = _member("a", max_power=5000.0, supports_asymmetric=True)
    sym = _member("b", max_power=5000.0, supports_asymmetric=False)
    assert inverter_limits([asym])[2] is True
    assert inverter_limits([asym, sym])[2] is False


def test_single_member_limits_passthrough():
    m = _member(max_power=12000.0, max_power_per_phase=4000.0,
                supports_asymmetric=True)
    assert inverter_limits([m]) == (12000.0, 4000.0, True)


def test_capacity_total():
    assert capacity_total([_battery("a", capacity=10), _battery("b", capacity=5)]) == 15
