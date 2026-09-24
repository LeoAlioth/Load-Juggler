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

from types import SimpleNamespace

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.dynamic_ocpp_evse.const import (
    CONF_BATTERY_MAX_CHARGE_POWER,
    CONF_BATTERY_MAX_DISCHARGE_POWER,
    CONF_BATTERY_POWER_ENTITY_ID,
    CONF_BATTERY_SOC_ENTITY_ID,
    CONF_CHARGER_ID,
    CONF_ENTITY_ID,
    CONF_EVSE_CURRENT_IMPORT_ENTITY_ID,
    CONF_EVSE_MAXIMUM_CHARGE_CURRENT,
    CONF_EVSE_MINIMUM_CHARGE_CURRENT,
    CONF_HUB_ENTRY_ID,
    CONF_INVERTER_MAX_POWER,
    CONF_INVERTER_OUTPUT_PHASE_A_ENTITY_ID,
    CONF_LOAD_PRIORITY,
    CONF_MAIN_BREAKER_RATING,
    CONF_NAME,
    CONF_PHASES,
    CONF_PHASE_VOLTAGE,
    CONF_WIRING_TOPOLOGY,
    DOMAIN,
    ENTRY_TYPE,
    ENTRY_TYPE_HUB,
    ENTRY_TYPE_LOAD,
    WIRING_TOPOLOGY_SERIES,
)

V = 230.0
RATING_W = 6000.0
RATING_A = RATING_W / V               # 26.09 A: the inverter carries all of it
HOUSE_A = 1000.0 / V                  # a steady 1 kW house
ALLOWANCE_A = RATING_A - HOUSE_A      # 21.74 A: all the charger may take
DT = 2                                # the default site cycle, seconds
START = 30                            # cycles on the household alone first
OUTPUT = "sensor.og_inverter_out_a"
STATUS = "sensor.og_evse_status_connector"


def _hub(slug, options=None):
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
            **(options or {}),
        },
    )


def _evse(hub):
    """A 1-phase 6→32 A Standard EVSE: the inverter binds, not the car."""
    return MockConfigEntry(
        domain=DOMAIN, version=2, minor_version=4, title="Off-grid EVSE",
        data={CONF_NAME: "Off-grid EVSE",
              CONF_ENTITY_ID: "og_evse",
              ENTRY_TYPE: ENTRY_TYPE_LOAD,
              CONF_CHARGER_ID: "og_evse",
              CONF_EVSE_CURRENT_IMPORT_ENTITY_ID: "sensor.og_evse_current",
              CONF_HUB_ENTRY_ID: hub.entry_id},
        options={
            CONF_LOAD_PRIORITY: 1,
            CONF_EVSE_MINIMUM_CHARGE_CURRENT: 6,
            CONF_EVSE_MAXIMUM_CHARGE_CURRENT: 32,
            CONF_PHASES: 1,
        },
    )


def _amps(value):
    return {"device_class": "current", "unit_of_measurement": "A"}, f"{value:.3f}"


async def _run(hass, slug, car_ramp_a_s, cycles=120, house=None, each_cycle=None,
               options=None, car=None, dynamic_control=True):
    """Close the loop: engine permit → permit pipeline → car → inverter → engine.

    The connector reads Available (no car) for ``START`` cycles, so every input
    EMA is settled on the household alone. Then the car plugs in and slews its
    draw toward its last command at ``car_ramp_a_s``; the connector reports
    Charging from that cycle on. ``house(i)`` overrides the household current
    per cycle (default: a steady ``HOUSE_A``), ``car(i)`` the car's draw (a
    car that ignores its command). At night the battery supplies the whole
    output. ``each_cycle(i, result)`` runs after every engine cycle;
    ``options`` adds hub options. One row per cycle.
    """
    from freezegun import freeze_time
    from custom_components.dynamic_ocpp_evse.control.smoothing import (
        apply_smoothing,
    )
    from custom_components.dynamic_ocpp_evse.engine.hub_calculation import (
        run_hub_calculation,
    )

    hub = _hub(slug, options)
    hub.add_to_hass(hass)
    evse = _evse(hub)
    evse.add_to_hass(hass)
    hass.data[DOMAIN] = {
        "hubs": {hub.entry_id: {
            "loads": [evse.entry_id],
            "battery_soc_min": 20,
            "battery_soc_target": 50,
        }},
        "loads": {evse.entry_id: {
            "entry": evse, "hub_entry_id": hub.entry_id,
            "dynamic_control": dynamic_control,
        }},
        "load_allocations": {evse.entry_id: 0},
        "inverters": {},
    }
    hass.states.async_set(STATUS, "Available")
    hass.states.async_set(
        "sensor.og_battery_soc", "80",
        {"device_class": "battery", "unit_of_measurement": "%"})
    # apply_smoothing keeps its state on the load entity and touches only these.
    permit_state = SimpleNamespace(
        _attr_name="og_evse", _ema_current=None, _schmitt_current=None,
        _schmitt_state="rising", _rate_limited_current=0.0,
    )

    trace = []
    draw = command = 0.0
    step = car_ramp_a_s * DT
    with freeze_time("2026-09-24 22:00:00+00:00") as frozen:
        for i in range(cycles):
            frozen.tick(DT)
            if i == START:
                hass.states.async_set(STATUS, "Charging")
            if car is not None:
                draw = car(i)
            elif i >= START:
                draw += max(-step, min(step, command - draw))
            house_a = HOUSE_A if house is None else house(i)
            supply = house_a + draw
            attrs, state = _amps(supply)
            hass.states.async_set(OUTPUT, state, attrs)
            attrs, state = _amps(draw)
            hass.states.async_set("sensor.og_evse_current", state, attrs)
            hass.states.async_set(
                "sensor.og_battery_power", f"{supply * V:.1f}",
                {"device_class": "power", "unit_of_measurement": "W"})
            result = run_hub_calculation(hass, hub)
            if each_cycle is not None:
                each_cycle(i, result)
            # The load processor's own rounding (entities/load.py).
            permit = round(result["load_available"][evse.entry_id], 1)
            command = apply_smoothing(permit_state, permit, False, hub)
            trace.append({"i": i, "draw": draw, "permit": permit,
                          "command": command, "supply": supply,
                          "household_w": result["household_power"],
                          "managed_w": result["total_evse_power"]})
    return trace


def _over_w(trace, key, limit_a):
    return max(0.0, max(row[key] - limit_a for row in trace)) * V


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

    permit_over = _over_w(trace, "permit", ALLOWANCE_A)
    supply_over = _over_w(trace, "supply", RATING_A)
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
    assert last["managed_w"] == 0
    assert last["household_w"] == pytest.approx((HOUSE_A + unmanaged_a) * V, abs=5), (
        f"household {last['household_w']:.0f} W, inverter output "
        f"{last['supply'] * V:.0f} W"
    )
