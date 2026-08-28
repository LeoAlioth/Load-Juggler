"""Tests for the Excess trigger — calculations.excess_margin.

Machine-authored tests — not yet human-reviewed.

Excess means the site can no longer place its own production anywhere else. One
number decides it for every Excess-mode load:

    margin = (grid export + battery charge power + our own managed draws)
           - (export allowance + battery charge allowance - hysteresis)

``margin >= 0`` means Excess is on, and the value is the excess pool in watts. A
sink contributes its allowance only while it can actually absorb — no grid means
no export allowance, and no battery (or a full one) means no charge allowance.
The measured battery discharge counts against the absorbed side, unclamped and
identically on every site, so a draw served by stored energy can never hold the
verdict it engaged on — off-grid, where export is always 0, that makes the
margin the load-off surplus by conservation.
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
    # Positive battery_power is discharging — it adds nothing to the absorbed
    # side, and it comes off the export term as well: only SOLAR export triggers
    # Excess, and export − discharge is production − consumption, so 10 kW of
    # the 13 kW at the meter is the array's. 10 kW absorbed against an 18 kW
    # allowance is 8 kW short.
    assert excess_margin(_site(export=13000, battery_power=3000, soc=60)) == -8000


def test_discharge_beyond_the_meter_counts_against_the_margin():
    # Night: nothing at the meter, the pack serving the house 500 W. The signed
    # term is unclamped, so the absorbed side reads −500 — the site is placing
    # less than nothing, and the margin says so.
    assert excess_margin(_site(export=0, battery_power=500, soc=60)) == -18500


def test_zero_allowance_site_reads_off_while_the_pack_serves_the_house():
    # The corner the old clamp got wrong: a zero-export site (allowance 0) with
    # a full battery at night read a margin of exactly 0 — Excess ON while the
    # pack discharged into the house. Stored energy serving the house is not
    # surplus; the signed term reads it off.
    site = _site(export=0, battery_power=500, soc=98, export_limit=0)
    assert excess_margin(site) == -500


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
    # is back — and the discharge itself counts against the margin, so the
    # verdict is held off by both.
    assert (
        excess_margin(_site(export=0, battery_power=4000, soc=50, off_grid=True))
        == -9000
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
# No SOC floor guards the off-grid case, and none is needed: the measured
# discharge counts AGAINST the margin — the same signed term every site uses,
# which off-grid (export always 0) is all that remains of the export side. The
# moment a load's draw lands on stored energy instead of production the margin
# collapses by exactly that much and Excess clears on its own — whatever the
# combined draw is. By conservation the off-grid margin is
#
#     charge - discharge + managed draws == production - unmanaged household
#
# against the allowance: the load-off surplus, which no engaged load can
# inflate. The worst a load can do while the margin holds is make the battery
# charge slower.

def test_off_grid_load_pushing_the_battery_into_discharge_clears_excess():
    # Production can no longer cover household + our load, so the battery is
    # discharging 1 kW to help. Its charging contributes 0, the discharge
    # subtracts, and the load's 2 kW cannot reach the 5 kW allowance on its own.
    site = _site(battery_power=1000, soc=90, off_grid=True, loads=[_load(2000)])
    assert excess_margin(site) == -4000


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


# --- Off-grid: draws beyond the charge allowance release ---------------------
#
# The correction above used to be capped at the charge rate: the allowance
# returning could absolve at most its own watts of draw, so any COMBINED
# engaged draw beyond it kept vouching for itself — evening, production gone,
# four loads riding the pack to the floor with the margin pinned positive.
# The discharge term removes the cap: stored energy serving the draws counts
# against them watt for watt.

def test_off_grid_engaged_loads_release_when_the_pack_drains():
    # Evening. Four loads (3.7 + 2.3 + 1 + 2 kW) engaged from the sunny
    # afternoon, production gone, the pack serving all 9 kW. The old
    # arithmetic read +4000 here — engaged forever, allowance maxed out,
    # falling SOC changing nothing. The discharge nets it to the truth.
    loads = [_load(3700), _load(2300), _load(1000), _load(2000)]
    site = _site(battery_power=9000, soc=50, off_grid=True, loads=loads)
    assert excess_margin(site) == -5000


def test_off_grid_partial_surplus_reads_the_true_pool():
    # Production 6 kW against the 5 kW allowance: 1 kW of genuine surplus.
    # The same four loads draw 9 kW, so the pack covers 3 kW of it — and the
    # margin still reads exactly the 1 kW that is real, not the 4 kW the
    # draws alone would claim. The allocation layer's pool deduction then
    # keeps only what fits.
    loads = [_load(3700), _load(2300), _load(1000), _load(2000)]
    site = _site(battery_power=3000, soc=90, off_grid=True, loads=loads)
    assert excess_margin(site) == 1000


def test_off_grid_no_battery_reading_degrades_to_the_old_arithmetic():
    # Without a battery power sensor the discharge term is 0 and the margin
    # is the pre-fix number. Deliberate: the degraded mode can only fail to
    # release, never refuse to engage — same shape as every other missing
    # reading in this module.
    loads = [_load(3700), _load(2300), _load(1000), _load(2000)]
    site = _site(battery_power=None, soc=50, off_grid=True, loads=loads)
    assert excess_margin(site) == 4000


# --- Grid-tied is unaffected by SOC ----------------------------------------

def test_grid_tied_engages_below_target_when_export_says_so():
    # The battery is charge-rate limited and taking all it can; the remainder is
    # genuinely leaving the site. Observable surplus needs no SOC proxy.
    assert excess_margin(_site(export=14000, battery_power=-5000, soc=50)) == 1000
