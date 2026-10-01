"""The off-grid household reconstruction, closed through the production cycle.

Machine-authored tests - not yet human-reviewed.

Off-grid there is no grid meter: everything the site draws, managed loads
included, comes out of the inverters, so the engine reconstructs the household
as ``inverter output − Σ managed draw`` (series formula,
``calculations.utils.compute_household_per_phase``) and sizes every permit on
the inverter rating that household leaves. The output is SMOOTHED on the input
EMA (``engine/readers._smooth_member_output``); until 2026-09-24 the draw it
was subtracted from was RAW. So when a charger started, its whole draw came
off at once while the output it is part of was still catching up, the
household read low ("the house shrank"), and the engine handed that phantom
headroom to the charger - the off-grid counterpart of the grid-tied double
advance fixed in 4cbbdd1, where both halves of the subtraction now move on one
EMA step per cycle.

This rig closes the loop through ``run_hub_calculation`` and the real permit
pipeline (``control.smoothing.apply_smoothing``) against a plant: a series
hybrid rated 6 kW carrying a steady 1 kW house on phase A from its battery,
and a charger that slews its draw toward whatever it is commanded. The output
sensor reads house + car, the charger reads the car, both updating in the same
cycle. The household never moves, so the right permit never moves either -
anything above the inverter's allowance is the reconstruction reading the
household low while the charger starts.
"""

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.dynamic_ocpp_evse.const import (
    CONF_BATTERY_MAX_CHARGE_POWER,
    CONF_BATTERY_MAX_DISCHARGE_POWER,
    CONF_BATTERY_POWER_ENTITY_ID,
    CONF_BATTERY_SOC_ENTITY_ID,
    CONF_ENTITY_ID,
    CONF_INVERTER_MAX_POWER,
    CONF_INVERTER_OUTPUT_PHASE_A_ENTITY_ID,
    CONF_MAIN_BREAKER_RATING,
    CONF_NAME,
    CONF_PHASE_VOLTAGE,
    CONF_WIRING_TOPOLOGY,
    DOMAIN,
    ENTRY_TYPE,
    ENTRY_TYPE_HUB,
    WIRING_TOPOLOGY_SERIES,
)

from .closed_loop import (
    AMPS, BATTERY_PCT, DT, SOC_BOUNDS, V, WATTS, close_loop, evse_entry, over_w, slew,
)

RATING_W = 6000.0
RATING_A = RATING_W / V               # 26.09 A: the inverter carries all of it
HOUSE_A = 1000.0 / V                  # a steady 1 kW house
ALLOWANCE_A = RATING_A - HOUSE_A      # 21.74 A: all the charger may take
START = 30                            # cycles on the household alone first
OUTPUT = "sensor.og_inverter_out_a"
STATUS = "sensor.og_evse_status_connector"


def _hub(slug):
    """No grid CT at all: an off-grid series hybrid, output metered on A."""
    return MockConfigEntry(
        domain=DOMAIN, version=2, minor_version=4, title=f"Off-grid Hub {slug}",
        data={CONF_NAME: f"Off-grid Hub {slug}",
              CONF_ENTITY_ID: f"og_hub_{slug}",
              ENTRY_TYPE: ENTRY_TYPE_HUB},
        options={
            CONF_PHASE_VOLTAGE: int(V),
            CONF_MAIN_BREAKER_RATING: 40,
            CONF_INVERTER_OUTPUT_PHASE_A_ENTITY_ID: OUTPUT,
            CONF_INVERTER_MAX_POWER: int(RATING_W),
            CONF_WIRING_TOPOLOGY: WIRING_TOPOLOGY_SERIES,
            CONF_BATTERY_SOC_ENTITY_ID: "sensor.og_battery_soc",
            CONF_BATTERY_POWER_ENTITY_ID: "sensor.og_battery_power",
            # A large pack, so the inverter's rating binds and not the battery.
            CONF_BATTERY_MAX_DISCHARGE_POWER: 20000,
            CONF_BATTERY_MAX_CHARGE_POWER: 5000,
        },
    )


async def _run(hass, slug, car_ramp_a_s, cycles=120, house=None, car=None,
               dynamic_control=True):
    """A 1-phase 6→32 A Standard EVSE on the inverter: it binds, not the car.

    The connector reads Available (no car) for ``START`` cycles, so every input
    EMA is settled on the household alone. Then the car plugs in and slews its
    draw toward its last command at ``car_ramp_a_s``; the connector reports
    Charging from that cycle on. ``house(i)`` overrides the household current
    per cycle (default: a steady ``HOUSE_A``), ``car(i)`` the car's draw (a
    car that ignores its command). At night the battery supplies the whole
    output. One row per cycle.
    """
    hub = _hub(slug)
    hass.states.async_set(STATUS, "Available")
    hass.states.async_set("sensor.og_battery_soc", "80", BATTERY_PCT)
    draw = 0.0

    def plant(i, command):
        nonlocal draw
        if i == START:
            hass.states.async_set(STATUS, "Charging")
        if car is not None:
            draw = car(i)
        elif i >= START:
            draw = slew(draw, command, car_ramp_a_s * DT)
        supply = (HOUSE_A if house is None else house(i)) + draw
        hass.states.async_set(OUTPUT, f"{supply:.3f}", AMPS)
        hass.states.async_set("sensor.og_evse_current", f"{draw:.3f}", AMPS)
        hass.states.async_set("sensor.og_battery_power", f"{supply * V:.1f}", WATTS)
        return {"draw": draw, "supply": supply}

    return await close_loop(
        hass, hub, evse_entry(hub, "og_evse"), cycles, plant,
        at="2026-09-24 22:00:00+00:00", hub_data=SOC_BOUNDS,
        load_data={"dynamic_control": dynamic_control})


@pytest.mark.parametrize("car_ramp_a_s", [1.0, 100.0], ids=["car-1A/s", "step"])
async def test_a_starting_charger_is_never_permitted_past_the_inverter(
    hass, car_ramp_a_s
):
    """The permit holds the inverter's allowance while the charger starts.

    Measured against this rig, as W over at peak (permit over the allowance /
    inverter output over its rating):

    ====================================  ===========  ===========
    managed draw in the household         1 A/s car    step
    ====================================  ===========  ===========
    raw (before 2026-09-24)               permit  980  permit  773
                                          output  911  output  566
    no input smoothing at all             0 / 0        0 / 0
    smoothed, once a cycle (production)   0 / 0        0 / 0
    ====================================  ===========  ===========

    The ramping car is the worse case: the output filter lags a ramp by a
    constant ``rate × (1 − α) / α`` (4.7 A at 1 A/s), more than the whole
    household, so the household read 0 until the hold decayed it away and the
    charger was offered the inverter's entire rating. Removing the filter
    altogether also passes here - raw minus raw is exact - which is why
    ``test_a_one_reading_house_transient_is_still_filtered`` checks what the
    filter is for. The budget is one step of the permit's own 0.1 A rounding.
    """
    trace = await _run(hass, f"s{car_ramp_a_s:g}", car_ramp_a_s)

    permit_over = over_w(trace, "permit", ALLOWANCE_A)
    supply_over = over_w(trace, "supply", RATING_A)
    assert permit_over <= 0.1 * V, (
        f"permit {permit_over:.0f} W over the inverter's allowance"
    )
    assert supply_over <= 0.1 * V, (
        f"inverter output {supply_over:.0f} W over its {RATING_W:.0f} W rating"
    )
    # And not by holding the car back: it ramped all the way to the allowance.
    assert trace[-1]["draw"] >= ALLOWANCE_A - 0.1


SPIKE_W = 2000.0     # a one-reading house transient, e.g. a motor's inrush
SPIKE_AT = START + 40  # the car long settled at the allowance


async def test_a_one_reading_house_transient_is_still_filtered(hass):
    """The input filter still does its job on the household: a transient that
    shows in ONE inverter-output reading moves the household - and the permit
    - by the filter's weight, not in full.

    That is what the output EMA is there for (const.EMA_TAU_S, "how noisy the
    readings are"; a single reading is also how a motor's start-up inrush
    looks, const.CTRL_FAST_TAU_S). The fix must not buy the exact subtraction
    by dropping it: raw output minus raw draw also cures the start overshoot.

    A 2 kW one-reading spike with the car settled at the allowance, measured
    against this rig (W, deepest cut below the settled value):

    ====================================  =============  ==============
    input smoothing                       engine permit  sent command
    ====================================  =============  ==============
    none (the input filter's tau → 0)     2001           667
    production (EMA_TAU_S), before fix    598            207
    production (EMA_TAU_S)                598            230
    ====================================  =============  ==============

    The budget is the filter's own weight on the spike, plus the permit's
    0.1 A rounding.
    """
    from custom_components.dynamic_ocpp_evse.const import ema_alpha_for

    spike_a = SPIKE_W / V
    trace = await _run(
        hass, "spike", 100.0, cycles=SPIKE_AT + 20,
        house=lambda i: HOUSE_A + (spike_a if i == SPIKE_AT else 0.0),
    )
    settled = trace[SPIKE_AT - 1]
    assert settled["permit"] >= ALLOWANCE_A - 0.1, "the car settled first"
    after = [row for row in trace if row["i"] >= SPIKE_AT]
    cut_w = (settled["permit"] - min(row["permit"] for row in after)) * V

    weight = ema_alpha_for(DT)
    assert cut_w <= (weight * spike_a + 0.1) * V, (
        f"a {SPIKE_W:.0f} W one-reading transient cut the permit by "
        f"{cut_w:.0f} W - the filter passes only {weight * SPIKE_W:.0f} W of it"
    )
    # And it is not simply ignored: the household did see a share of it.
    assert cut_w >= (weight * spike_a - 0.1) * V


async def test_a_load_with_dynamic_control_off_is_household_off_grid(hass):
    """A charger handed back to the user (Dynamic Control OFF) is household,
    and off-grid its draw stays in the household the engine sizes the
    inverter's allowance on - as it already did grid-tied (the feedback loop
    leaves it in the meter figure, _managed_phase_draws).

    Until 2026-09-24 the off-grid household took EVERY load's reading off the
    output, this one included, so its 2.3 kW vanished from the house figure
    while the engine did not reserve it either (it competes for nothing):
    household 998 W with the inverter putting out 3300 W, and the inverter's
    allowance sized as if those 2.3 kW were free. Now the household takes off
    the same smoothed list every other view subtracts, which skips an
    unmanaged load - so the published household plus the managed power add up
    to the inverter's output again.
    """
    unmanaged_a = 10.0
    trace = await _run(
        hass, "unmanaged", 0.0, cycles=START + 20,
        car=lambda i: unmanaged_a if i >= START else 0.0,
        dynamic_control=False,
    )
    last = trace[-1]
    household_w = last["result"]["household_power"]
    assert last["result"]["total_evse_power"] == 0
    assert household_w == pytest.approx((HOUSE_A + unmanaged_a) * V, abs=5), (
        f"household {household_w:.0f} W, inverter output "
        f"{last['supply'] * V:.0f} W"
    )
