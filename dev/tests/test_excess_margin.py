"""Tests for the Excess trigger — calculations.excess_margin.

Machine-authored tests — not yet human-reviewed.

Excess means the site can no longer place its own production anywhere else. One
number decides it for every Excess-mode load:

    margin = (grid export + battery charge power + our own managed draws)
           - (export allowance + battery charge allowance - hysteresis)

``margin >= 0`` means Excess is on, and the value is the excess pool in watts. A
sink contributes its allowance only while it can actually absorb — no grid means
no export allowance, and no battery (or a full one) means no charge allowance.
"""

from custom_components.dynamic_ocpp_evse.calculations import excess_margin
from custom_components.dynamic_ocpp_evse.calculations.models import (
    LoadContext,
    PhaseValues,
    SiteContext,
)

EXPORT_LIMIT = 13000.0
CHARGE_LIMIT = 5000.0
SOC_FULL = 97.0
SOC_TARGET = 80.0
HYSTERESIS = 500.0


def _load(draw_w, voltage=230.0):
    """A managed load drawing ``draw_w`` on phase A."""
    return LoadContext(
        load_id="load",
        entity_id="load",
        min_current=0,
        max_current=draw_w / voltage,
        phases=1,
        active_phases_mask="A",
        l1_current=draw_w / voltage,
        l1_phase="A",
    )


def _site(
    export=0.0,
    battery_power=None,
    soc=None,
    charge_limit=CHARGE_LIMIT,
    export_limit=EXPORT_LIMIT,
    off_grid=False,
    loads=(),
):
    """Build a SiteContext with the fields the margin reads.

    ``export`` is in watts and converted to the per-phase current the property
    derives from; ``battery_power`` follows the site convention (positive
    discharging, negative charging).
    """
    voltage = 230.0
    return SiteContext(
        voltage=voltage,
        consumption=PhaseValues(a=0.0, b=None, c=None),
        export_current=PhaseValues(a=export / voltage, b=None, c=None),
        battery_power=battery_power,
        battery_soc=soc,
        battery_soc_full=SOC_FULL,
        battery_max_charge_power=charge_limit,
        excess_export_threshold=export_limit,
        battery_soc_target=SOC_TARGET,
        is_off_grid=off_grid,
        loads=list(loads),
    )


# --- No battery: the export allowance alone ---------------------------------

def test_no_battery_export_below_limit_is_negative():
    assert excess_margin(_site(export=12000, charge_limit=None)) == -1000


def test_no_battery_export_at_limit_is_zero_and_on():
    # Zero is the saturated case and counts as on.
    assert excess_margin(_site(export=EXPORT_LIMIT, charge_limit=None)) == 0


def test_no_battery_export_above_limit_is_the_pool():
    # The margin IS the excess pool: 2 kW past the allowance.
    assert excess_margin(_site(export=15000, charge_limit=None)) == 2000


def test_no_battery_no_export_is_negative():
    # Night. The full export allowance stands between the site and Excess.
    assert excess_margin(_site(export=0, charge_limit=None)) == -EXPORT_LIMIT


# --- Battery with headroom: both sinks count --------------------------------

def test_battery_charging_below_max_is_negative():
    # 13 kW export + 2 kW charge = 15 kW absorbed against an 18 kW allowance.
    # The battery still has 3 kW of headroom, so this is not surplus.
    assert excess_margin(_site(export=13000, battery_power=-2000, soc=60)) == -3000


def test_battery_charging_at_max_with_export_at_limit_is_zero():
    # Both sinks saturated — exactly at the trigger, which counts as on.
    assert excess_margin(_site(export=13000, battery_power=-5000, soc=60)) == 0


def test_battery_charging_at_max_but_low_export_is_negative():
    assert excess_margin(_site(export=5000, battery_power=-5000, soc=60)) == -8000


def test_discharging_battery_absorbs_nothing():
    # Positive battery_power is discharging — it adds nothing to the absorbed side.
    assert excess_margin(_site(export=13000, battery_power=3000, soc=60)) == -5000


# --- Full battery: its allowance drops out ----------------------------------

def test_full_battery_frees_allowance_so_export_alone_triggers():
    # The case a naive sum gets backwards: a full battery draws no charge power,
    # so leaving its 5 kW in the allowance would make the trigger unreachable
    # under a 13.6 kW export cap — exactly when the site dumps the most.
    assert excess_margin(_site(export=13600, battery_power=0, soc=98)) == 600


def test_full_battery_still_needs_the_export_allowance_met():
    assert excess_margin(_site(export=9000, battery_power=0, soc=98)) == -4000


def test_battery_at_full_soc_exactly_counts_as_full():
    # SOC exactly at the Full threshold — allowance is the export one only.
    assert excess_margin(_site(export=13000, battery_power=0, soc=SOC_FULL)) == 0


# --- Unset limits -----------------------------------------------------------

def test_unconfigured_charge_limit_contributes_no_allowance():
    # Battery entities exist but no max charge power was configured: its charging
    # still counts as absorbed, it just buys no allowance.
    assert (
        excess_margin(_site(export=13000, battery_power=-2000, soc=60, charge_limit=None))
        == 2000
    )


def test_off_grid_zeroes_the_export_allowance():
    # Off-grid: nothing can leave, so only the battery's headroom holds Excess off.
    assert excess_margin(_site(export=0, battery_power=-5000, soc=60, off_grid=True)) == 0


def test_off_grid_battery_with_headroom_is_negative():
    assert (
        excess_margin(_site(export=0, battery_power=-1000, soc=60, off_grid=True))
        == -4000
    )


def test_off_grid_full_battery_sits_exactly_at_the_trigger():
    # Nothing can leave and nothing can be stored: no allowance at all, so the
    # margin is 0 — on, but with a pool of 0, so only consumers reading the plain
    # verdict (the tank's boost setpoint) act on it. EVSEs and plugs need a pool
    # strictly above zero and still get nothing.
    assert excess_margin(_site(export=0, battery_power=0, soc=98, off_grid=True)) == 0


def test_off_grid_discharging_battery_below_full_is_negative():
    # Night, battery working: SOC has fallen below full, so its charge allowance
    # is back and holds the verdict off.
    assert (
        excess_margin(_site(export=0, battery_power=4000, soc=50, off_grid=True))
        == -5000
    )


# --- Hysteresis -------------------------------------------------------------

def test_hysteresis_keeps_a_marginal_site_engaged():
    # 200 W short of the trigger: off on the way up, still on once engaged.
    site = _site(export=12800, charge_limit=None)
    assert excess_margin(site) == -200
    assert excess_margin(site, HYSTERESIS) == 300


def test_hysteresis_cannot_manufacture_a_pool_beyond_the_real_power():
    # Off-grid with a full battery has no allowance to shrink. Without the clamp
    # the margin would read +500 W — a pool larger than the power that exists.
    site = _site(export=0, battery_power=0, soc=98, off_grid=True)
    assert excess_margin(site, HYSTERESIS) == 0


def test_hysteresis_shrinks_the_allowance_not_the_absorbed_side():
    site = _site(export=13000, battery_power=-2000, soc=60)
    assert excess_margin(site, HYSTERESIS) == -2500


# --- Off-grid: managed draws are added back ---------------------------------
#
# Off-grid the feedback loop has no grid reading to add managed draws back to, so
# excess_margin() adds them itself. That makes a running load a probe: a curtailing
# inverter ramps up to serve it, and the margin settles at the site's true surplus.
# The scenarios below use a 5 kW charge allowance and no household load.

def test_off_grid_curtailed_inverter_holds_the_margin_when_a_load_starts():
    # Array capable of 8 kW, battery taking its 5 kW max, so 3 kW is curtailed.
    # Nothing running: exactly at the trigger.
    idle = _site(battery_power=-5000, soc=90, off_grid=True)
    assert excess_margin(idle) == 0

    # A 2 kW load starts; the inverter ramps to 7 kW so charging stays at 5 kW.
    # The margin rises to the surplus now being used, and Excess stays engaged.
    running = _site(battery_power=-5000, soc=90, off_grid=True, loads=[_load(2000)])
    assert excess_margin(running) == 2000


def test_off_grid_partial_headroom_settles_at_the_true_surplus():
    # Array capable of only 6 kW against a 5 kW allowance — 1 kW of real surplus.
    # The 2 kW load costs the battery 1 kW of charging (5 kW -> 4 kW), and the
    # margin reports exactly the 1 kW that was genuinely spare.
    running = _site(battery_power=-4000, soc=90, off_grid=True, loads=[_load(2000)])
    # Without the add-back this read -1000 and the verdict chattered every cycle.
    assert excess_margin(running) == 1000


def test_off_grid_no_surplus_sits_at_the_trigger_not_below():
    # Array maxed at 5 kW: the load's 2 kW comes entirely out of charging, so
    # there was never any surplus. The margin sits at 0 — engaged, but with a
    # pool of 0, and the battery keeps charging at the reduced rate.
    running = _site(battery_power=-3000, soc=90, off_grid=True, loads=[_load(2000)])
    assert excess_margin(running) == 0


def test_grid_tied_does_not_double_count_managed_draws():
    # Grid-tied, the feedback loop has already added the draw into export.
    with_load = _site(export=13000, charge_limit=None, loads=[_load(2000)])
    without = _site(export=13000, charge_limit=None)
    assert excess_margin(with_load) == excess_margin(without) == 0


# --- Off-grid: a discharging battery self-corrects --------------------------
#
# No SOC floor guards the off-grid case, and none is needed: a discharging battery
# contributes nothing to the absorbed side, so the moment a load pushes the battery
# past charging into discharge the margin collapses and Excess clears on its own.
# The worst a load can do while the margin holds is make the battery charge slower.

def test_off_grid_load_pushing_the_battery_into_discharge_clears_excess():
    # Production can no longer cover household + our load, so the battery is
    # discharging 1 kW to help. Its charging contributes 0, and the load's 2 kW
    # cannot reach the 5 kW allowance on its own.
    site = _site(battery_power=1000, soc=90, off_grid=True, loads=[_load(2000)])
    assert excess_margin(site) == -3000


def test_off_grid_below_target_is_no_special_case():
    # SOC plays no part off-grid beyond the full-battery rule: a battery charging
    # at its maximum is saturated whether it sits at 70% or 90%.
    low = _site(battery_power=-5000, soc=70, off_grid=True)
    high = _site(battery_power=-5000, soc=90, off_grid=True)
    assert excess_margin(low) == excess_margin(high) == 0


def test_off_grid_slower_charging_is_the_worst_a_load_can_do():
    # Array maxed at 5 kW with a 2 kW load running: charging drops to 3 kW, the
    # margin sits at 0 (still engaged), and the battery keeps charging — slower,
    # never draining.
    site = _site(battery_power=-3000, soc=70, off_grid=True, loads=[_load(2000)])
    assert excess_margin(site) == 0


# --- Grid-tied is unaffected by SOC ----------------------------------------

def test_grid_tied_engages_below_target_when_export_says_so():
    # The battery is charge-rate limited and taking all it can; the remainder is
    # genuinely leaving the site. Observable surplus needs no SOC proxy.
    assert excess_margin(_site(export=14000, battery_power=-5000, soc=50)) == 1000
