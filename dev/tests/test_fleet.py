"""Tests for the inverter fleet aggregation - engine.fleet.

Machine-authored tests - not yet human-reviewed.

Many inverter entries reduce to the single-inverter/single-battery scalars
SiteContext expects. The per-member gating the scalar form cannot express
happens here: charge capacity excludes members whose OWN battery is at its
OWN full-SOC; discharge capacity excludes members below the hub minimum;
fleet SOC is capacity-weighted; solar sums each member's output less its
input less its own battery power. With a single member every aggregate
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
    inverter_limits,
    forecast_device_ids,
    member_input,
    member_solar,
    member_solar_production,
    member_solar_published,
    net_output,
    output_power_estimate,
    output_power_measured,
    output_power_total,
    soc_full_scalar,
    soc_target_weighted,
    split_charge_limit,
    solar_is_assumed,
    solar_is_measured,
    solar_is_unsplit,
    solar_total,
    weighted_soc,
)

V = 230.0
# The signed grid reading per phase; only a grid-input member reads it.
G = (None, None, None)


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
    # One battery full, one empty: only the empty one's cap counts - the
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


# --- Charge capacity: what the battery is PERMITTED to take ---------------------
#
# The sum is the allowance the Excess verdict compares the site's placed power
# against, so it must be the rate each battery MAY take. While our own charge
# control holds a member's register below its nameplate rate - the PV clipping
# forecast reserving room for the afternoon - the difference is not a place the
# site can put production. Only enforcement narrows: a member that is merely
# advised a lower rate (its switch off) still charges at its rating.

def test_an_enforced_limit_narrows_that_members_share():
    # 10 kW rated, held at 6.5 kW by the forecast: 6.5 kW is what it may take.
    members = [_battery(soc=70, charge=10000, enforced_charge_limit=6500)]
    assert charge_power_total(members) == 6500


def test_an_advice_only_member_keeps_its_nameplate_rate():
    # Nothing is written to this inverter, so it really does still charge at its
    # rating - narrowing here would under-report the allowance and over-trigger.
    members = [_battery(soc=70, charge=10000, enforced_charge_limit=None)]
    assert charge_power_total(members) == 10000


def test_only_the_enforcing_member_of_a_mixed_fleet_narrows():
    members = [
        _battery("enforcing", soc=70, charge=10000, enforced_charge_limit=6500),
        _battery("advice_only", soc=70, charge=4000),
    ]
    assert charge_power_total(members) == 10500


def test_an_enforced_limit_above_the_rating_is_not_a_lift():
    # min(), never max(): a register held at more than the inverter is rated for
    # does not make the battery take more than its plate.
    members = [_battery(soc=70, charge=5000, enforced_charge_limit=9000)]
    assert charge_power_total(members) == 5000


def test_an_enforced_zero_leaves_no_allowance_at_all():
    # A hard 0 A charge limit: the battery is not a sink, so the export
    # allowance alone stands between the site and Excess. Not None - the member
    # is still there with a configured cap, it is just permitted nothing.
    members = [_battery(soc=70, charge=10000, enforced_charge_limit=0)]
    assert charge_power_total(members) == 0


def test_a_full_member_stays_excluded_whatever_is_enforced():
    members = [
        _battery("full", soc=98, full=97, charge=10000, enforced_charge_limit=6500),
        _battery("empty", soc=30, full=97, charge=3000),
    ]
    assert charge_power_total(members) == 3000


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


# --- Battery destination: the anchor the clipping reserve is carved below -------

def test_soc_target_single_battery_is_its_own():
    # The single-battery case must reduce to exactly the member's own ceiling.
    assert soc_target_weighted([_battery(capacity=20, soc_target=95.0)]) == 95.0


def test_soc_target_all_unconfigured_is_a_hundred():
    # No ceiling source anywhere - every battery is heading for 100 %, which is
    # the pre-destination behaviour of every site that never configured one.
    members = [_battery("a", capacity=10), _battery("b", capacity=5)]
    assert soc_target_weighted(members) == 100.0


def test_soc_target_is_capacity_weighted():
    # 90 % of 10 kWh and 100 % of 30 kWh → (900 + 3000) / 40 = 97.5 %, so one
    # uniform ceiling leaves exactly the kWh the members' own ceilings imply.
    members = [
        _battery("a", capacity=10, soc_target=90.0),
        _battery("b", capacity=30, soc_target=100.0),
    ]
    assert soc_target_weighted(members) == 97.5


def test_soc_target_unconfigured_member_contributes_a_hundred():
    # Mixed: a configured 80 % on 10 kWh beside an unconfigured 10 kWh pack.
    members = [
        _battery("a", capacity=10, soc_target=80.0),
        _battery("b", capacity=10),
    ]
    assert soc_target_weighted(members) == 90.0


def test_soc_target_ignores_members_without_a_battery_or_capacity():
    # A PV-only inverter has no destination, and a battery with no configured
    # capacity contributes no headroom - neither may drag the mean.
    members = [
        _battery("a", capacity=20, soc_target=95.0),
        _battery("b", capacity=0, soc_target=50.0),
        _member("pv"),
    ]
    assert soc_target_weighted(members) == 95.0


def test_soc_target_without_batteries_is_the_default():
    assert soc_target_weighted([_member("pv")]) == 100.0
    assert soc_target_weighted([]) == 100.0


# --- Outputs and solar ----------------------------------------------------------

def test_member_input_is_the_sensors_or_the_grid_meter():
    sensors = PhaseValues(a=2.0, b=None, c=None)
    m = _member(input=sensors, input_raw=PhaseValues(a=3.0, b=None, c=None))
    assert member_input(m, (5.0, 6.0, 7.0)) is sensors
    assert member_input(m, (5.0, 6.0, 7.0), raw=True).a == 3.0
    tick = _member(grid_input=True)
    assert member_input(tick, (5.0, 6.0, 7.0)) == PhaseValues(5.0, 6.0, 7.0)
    assert member_input(_member(), (5.0, 6.0, 7.0)) is None


def test_net_output_per_phase():
    members = [
        _member("a", output=PhaseValues(a=10.0, b=None, c=None)),
        _member("b", output=PhaseValues(a=5.0, b=4.0, c=None)),
    ]
    net = net_output(members, G)
    assert net.a == 15.0
    assert net.b == 4.0
    assert net.c is None
    assert net_output([_member()], G) is None


def test_net_output_subtracts_the_input_sensors():
    hybrid = _member(
        output=PhaseValues(a=10.0, b=10.0, c=None),
        input=PhaseValues(a=4.0, b=None, c=None),  # B has no input sensor: 0
    )
    net = net_output([hybrid], G)
    assert (net.a, net.b, net.c) == (6.0, 10.0, None)


def test_net_output_cancels_the_grid_for_a_grid_input_member():
    # The grid it passes through is the input, so whatever the grid reads the
    # net is the output less that - and the household built on it cancels it.
    hybrid = _member(output=PhaseValues(a=10.0, b=8.0, c=None), grid_input=True)
    net = net_output([hybrid], (3.0, -2.0, None))
    assert (net.a, net.b, net.c) == (7.0, 10.0, None)


def test_net_output_smoothed_and_raw_are_separate_readings():
    m = _member(
        output=PhaseValues(a=10.0, b=None, c=None),
        output_raw=PhaseValues(a=12.0, b=None, c=None),
        input=PhaseValues(a=2.0, b=None, c=None),
        input_raw=PhaseValues(a=3.0, b=None, c=None),
    )
    assert net_output([m], G).a == 8.0
    assert net_output([m], G, raw=True).a == 9.0


def test_solar_pv_inverter_output_is_production():
    m = _member(output=PhaseValues(a=10.0, b=None, c=None))
    assert member_solar(m, V, G) == 10.0 * V


def test_solar_hybrid_with_sensors_is_net_of_input_and_battery():
    # 10 A out, 4 A taken in at its AC input, battery discharging 500 W:
    # 6 A * 230 V = 1380 W net, less the 500 W the battery gave = 880 W solar.
    m = _battery(
        soc=80, power=500,
        output=PhaseValues(a=10.0, b=None, c=None),
        input=PhaseValues(a=4.0, b=None, c=None),
    )
    assert member_solar(m, V, G) == 6.0 * V - 500


def test_solar_hybrid_with_the_tick_leaves_out_the_grid_passing_through():
    # out - grid - battery: 10 A out of which 3 A is the grid passing through.
    m = _battery(
        soc=80, power=500,
        output=PhaseValues(a=10.0, b=None, c=None), grid_input=True,
    )
    assert member_solar(m, V, (3.0, None, None)) == 7.0 * V - 500


def test_solar_off_grid_subtracts_own_battery():
    # Off-grid the output is the site's supply: 2300 W out, 2300 W of it from
    # the battery at night is 0 W of solar, not 2300 W; by day, charging
    # 1000 W beside a 2300 W output, the panels make 3300 W. The grid is 0, so
    # a tick or no input at all reads the same.
    out = PhaseValues(a=10.0, b=None, c=None)
    night = _battery(soc=80, power=10.0 * V, output=out)
    day = _battery(soc=80, power=-1000, output=out, grid_input=True)
    assert member_solar(night, V, G) == 0.0
    assert member_solar(day, V, (0.0, None, None)) == 10.0 * V + 1000


def test_solar_mixed_fleet_sums_per_member():
    pv = _member("p", output=PhaseValues(a=10.0, b=None, c=None))
    hybrid = _battery(
        "s", soc=80, power=-1000,
        output=PhaseValues(a=5.0, b=None, c=None),
    )
    # PV 2300 + hybrid (1150 − (−1000)) = 2300 + 2150
    assert solar_total([pv, hybrid], V, G) == 2300 + 5.0 * V + 1000


def test_solar_a_battery_belongs_to_its_inverter_whatever_it_is_called():
    # No wiring word: a member with a battery behind it nets the battery's flow
    # off its output, the signed power in both directions.
    out = PhaseValues(a=10.0, b=None, c=None)
    discharging = _battery(soc=80, power=500, output=out)
    charging = _battery(soc=80, power=-500, output=out)
    assert member_solar(discharging, V, G) == 10.0 * V - 500
    assert member_solar(charging, V, G) == 10.0 * V + 500


def test_solar_none_without_outputs():
    assert solar_total([_battery(soc=50)], V, G) is None


def test_solar_production_sensor_wins_over_output():
    """A member with its own production sensor reports it, output ignored."""
    m = _member(
        output=PhaseValues(a=10.0, b=None, c=None),
        has_solar_entity=True,
        solar_measured=1800.0,
    )
    assert member_solar_production(m, V, G) == 1800.0
    assert solar_total([m], V, G) == 1800.0


def test_solar_mixed_measured_and_derived_are_summed():
    """One inverter with a production sensor, one with only outputs - the
    fleet total is the sum, which is why derivation is per member."""
    measured = _member("m", has_solar_entity=True, solar_measured=3000.0)
    derived = _member("d", output=PhaseValues(a=10.0, b=None, c=None))
    assert solar_total([measured, derived], V, G) == 3000.0 + 10.0 * V


def test_solar_is_measured_only_when_every_member_measures():
    measured = _member("m", has_solar_entity=True, solar_measured=3000.0)
    derived = _member("d", output=PhaseValues(a=10.0, b=None, c=None))
    assert solar_is_measured([measured]) is True
    assert solar_is_measured([measured, derived]) is False
    # No members at all: nothing is measured, so solar stays derived.
    assert solar_is_measured([]) is False


def test_an_invented_zero_is_not_published_as_production():
    """A dead production sensor computes as 0 W and publishes as nothing.

    ``solar_assumed`` is set by the reader when the configured sensor is
    unreadable and there is nothing to hold, so the 0 W in ``solar_measured``
    was invented rather than measured. The calculation keeps using it - 0 W is
    the conservative figure and the household maths cannot take None - but the
    member's published production is None, so its device sensor reads unknown
    instead of a confident 0 W in full sun.
    """
    dead = _member("d", has_solar_entity=True, solar_measured=0.0, solar_assumed=True)
    assert member_solar_production(dead, V, G) == 0.0  # what the engine allocates on
    assert member_solar_published(dead, V, G) is None  # what the sensor shows
    assert solar_is_assumed([dead]) is True


def test_a_readable_production_sensor_publishes_normally():
    live = _member("m", has_solar_entity=True, solar_measured=1800.0)
    assert member_solar_published(live, V, G) == 1800.0
    assert solar_is_assumed([live]) is False
    # A real 0 W (night, or a sensor genuinely reading zero) still publishes:
    # "measured 0" and "invented 0" are different things and only the second
    # one is the bug.
    night = _member("n", has_solar_entity=True, solar_measured=0.0)
    assert member_solar_published(night, V, G) == 0.0
    assert solar_is_assumed([night]) is False


def test_a_derived_member_is_never_assumed():
    """No production sensor configured is not a fabrication.

    Such a member derives its production from its inverter output (or the site
    falls back to grid export), and nothing there is invented - so a site with
    no solar sensor at all publishes exactly what it always did.
    """
    derived = _member("d", output=PhaseValues(a=10.0, b=None, c=None))
    assert member_solar_published(derived, V, G) == 10.0 * V
    assert solar_is_assumed([derived]) is False
    assert solar_is_assumed([]) is False


def test_one_dead_member_keeps_its_sibling_honest_and_silences_the_total():
    """Per-member publication, fleet-total suppression.

    Each inverter publishes a production sensor of its OWN, so the healthy
    member keeps reporting its real figure - that is a measurement worth
    keeping. The fleet TOTAL is a sum containing one invented term, which
    makes the whole sum fabricated (the rule the grid phases already follow).
    """
    live = _member("m", has_solar_entity=True, solar_measured=3000.0)
    dead = _member("d", has_solar_entity=True, solar_measured=0.0, solar_assumed=True)
    assert member_solar_published(live, V, G) == 3000.0
    assert member_solar_published(dead, V, G) is None
    # The engine still allocates on the partial sum...
    assert solar_total([live, dead], V, G) == 3000.0
    # ...and the publisher is told the sum cannot be shown as a measurement.
    assert solar_is_assumed([live, dead]) is True


def test_an_off_grid_output_nothing_splits_is_not_published():
    """With the battery's power unread, the output is the whole supply and
    nothing takes the battery back out: the engine keeps the output as its
    solar, the publisher gets None. A read battery (held or live) and a
    battery-less inverter publish."""
    out = PhaseValues(a=10.0, b=None, c=None)
    unread = _battery(soc=80, power=None, output=out)
    read = _battery(soc=80, power=1000.0, output=out)
    pv_only = _member(output=out)
    assert solar_total([unread], V, G) == 10.0 * V
    assert member_solar_published(unread, V, G) is None
    assert solar_is_assumed([unread]) is True
    assert member_solar_published(read, V, G) == 10.0 * V - 1000.0
    assert member_solar_published(pv_only, V, G) == 10.0 * V
    assert solar_is_assumed([read, pv_only]) is False


def test_forecast_device_ids_merge_and_dedupe():
    """Each PV array belongs to an inverter, but clipping is site-wide - the
    fleet's devices merge into one list, with shared devices counted once."""
    a = _member("a", forecast_device_ids=("east", "west"))
    b = _member("b", forecast_device_ids=("west", "north"))
    assert forecast_device_ids([a, b]) == ["east", "west", "north"]
    assert forecast_device_ids([_member("c")]) == []


def test_charging_power_total_for_fallback():
    members = [_battery("a", power=-2000), _battery("b", power=300)]
    assert charging_power_total(members) == 2000


# --- The fleet's AC output --------------------------------------------------------

def test_output_power_measured_is_net_of_input():
    hybrid = _member(
        output=PhaseValues(a=10.0, b=10.0, c=None),
        input=PhaseValues(a=4.0, b=None, c=None),
    )
    assert output_power_measured([hybrid], V, G) == (6.0 + 10.0) * V
    assert output_power_measured([_member()], V, G) is None


def test_output_power_measured_leaves_out_the_grid_a_tick_member_passes_on():
    hybrid = _member(output=PhaseValues(a=10.0, b=None, c=None), grid_input=True)
    assert output_power_measured([hybrid], V, (3.0, None, None)) == 7.0 * V


def test_the_estimate_uses_the_signed_battery_power():
    # Charging takes AC power away, whatever the inverter is called: the sun
    # 3000 W with a battery taking 2000 W puts 1000 W on the bus.
    hybrid = _battery(soc=50, power=-2000)
    assert output_power_estimate([hybrid], 3000.0) == 1000.0
    assert output_power_total([hybrid], V, solar_w=3000.0) == 1000.0
    discharging = _battery(soc=50, power=1500)
    assert output_power_estimate([discharging], 3000.0) == 4500.0


def test_the_estimate_falls_back_to_the_fleet_battery_reading():
    assert output_power_estimate([_member()], 3000.0, -500.0) == 2500.0
    assert output_power_estimate([], None, None) == 0.0


def test_the_unmetered_member_adds_its_signed_battery_beside_a_metered_one():
    metered = _member("m", output=PhaseValues(a=10.0, b=None, c=None))
    unmetered = _battery(
        "u", soc=50, power=-800, has_solar_entity=True, solar_measured=1500.0
    )
    total = output_power_total([metered, unmetered], V, grid=G)
    assert total == 10.0 * V + 1500.0 - 800.0


# --- Unsplit solar -------------------------------------------------------------------

def test_solar_is_unsplit_holds_grid_tied_too():
    # A battery whose power is unread leaves the output unsplit from it, on any
    # site: the member's output carries its flow wherever the site is wired.
    out = PhaseValues(a=10.0, b=None, c=None)
    assert solar_is_unsplit(_battery(soc=80, power=None, output=out))
    assert not solar_is_unsplit(_battery(soc=80, power=1000.0, output=out))
    assert not solar_is_unsplit(_member(output=out))


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


# ---------------------------------------------------------------------------
# split_charge_limit - the fleet advice divided by remaining headroom
# ---------------------------------------------------------------------------
def _pack(entry_id, soc, capacity, charge=5000.0):
    return _battery(entry_id, soc=soc, capacity=capacity, charge=charge, power=0.0)


def test_split_single_battery_is_min_of_cap_and_limit():
    assert split_charge_limit([_pack("a", 50, 10)], 1000.0, 90) == {"a": 1000.0}
    assert split_charge_limit([_pack("a", 50, 10, charge=800.0)], 1000.0, 90) == {
        "a": 800.0
    }


def test_split_equal_soc_divides_by_capacity():
    shares = split_charge_limit([_pack("a", 50, 10), _pack("b", 50, 20)], 3000.0, 90)
    assert shares["a"] == 1000.0 and shares["b"] == 2000.0


def test_split_follows_remaining_headroom_not_charge_cap():
    # Same capacity and cap, but a is 5 points under the ceiling and b 15:
    # b has three times the room and takes three quarters.
    shares = split_charge_limit([_pack("a", 90, 10), _pack("b", 80, 10)], 2000.0, 95)
    assert shares["a"] == 500.0 and shares["b"] == 1500.0


def test_split_clamps_at_the_cap_and_refills_the_rest():
    # b would want 1500 but its charger only does 1000 - a takes the rest.
    shares = split_charge_limit(
        [_pack("a", 90, 10), _pack("b", 80, 10, charge=1000.0)], 2000.0, 95
    )
    assert shares == {"a": 1000.0, "b": 1000.0}


def test_split_all_at_the_ceiling_falls_back_to_capacity():
    # The destination hold: both parked at 95, overflow shared by capacity.
    shares = split_charge_limit([_pack("a", 95, 10), _pack("b", 96, 30)], 4000.0, 95)
    assert shares["a"] == 1000.0 and shares["b"] == 3000.0


def test_split_overflow_reaches_the_pack_at_the_ceiling():
    # a is parked at the ceiling, b has room but a 1 kW charger: the 2 kW the
    # loop asked for is 1 kW into b and the rest offered to a as overflow.
    shares = split_charge_limit(
        [_pack("a", 95, 10), _pack("b", 80, 10, charge=1000.0)], 2000.0, 95
    )
    assert shares == {"a": 1000.0, "b": 1000.0}


def test_split_unknown_soc_anywhere_uses_capacity_for_all():
    shares = split_charge_limit([_pack("a", None, 10), _pack("b", 80, 30)], 4000.0, 95)
    assert shares["a"] == 1000.0 and shares["b"] == 3000.0


def test_split_excludes_members_that_cannot_carry_a_limit():
    members = [
        _pack("a", 50, 10),
        _battery("nocap", soc=50, capacity=10, charge=None),
        _battery("nocapacity", soc=50, capacity=0),
        _member("pv_only"),
    ]
    assert split_charge_limit(members, 1000.0, 90) == {"a": 1000.0}
    assert split_charge_limit(members, None, 90) == {}
    assert split_charge_limit([_member("pv_only")], 1000.0, 90) == {}


def test_split_never_hands_out_more_than_the_caps():
    shares = split_charge_limit(
        [_pack("a", 50, 10, charge=500.0), _pack("b", 50, 10, charge=500.0)], 5000.0, 90
    )
    assert shares == {"a": 500.0, "b": 500.0}
