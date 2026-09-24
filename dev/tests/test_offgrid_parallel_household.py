"""The off-grid household on a PARALLEL-wired inverter with output sensors.

Machine-authored tests - not yet human-reviewed.

Off-grid there is no grid for an inverter to feed beside: everything the site
consumes - the house AND our own managed loads - comes out of the inverters,
whatever their wiring (``engine/hub_calculation._supply_per_phase`` says the
same for the stuck-readout watch). So the household is the inverter output
less the managed draw on both wirings. ``compute_household_per_phase`` has two
formulas: series takes the draw off the output; parallel is
``grid consumption + output - grid export``, which grid-tied is right because
the feedback loop has already taken the draws off the grid terms. Off-grid the
grid terms are synthetic zeros the feedback loop leaves alone, so until
2026-09-24 the parallel household was the inverter's whole output, nothing
taken off: a charger's own draw was read as house load, the inverter's
allowance for it (rating - household) shrank by that draw, and the car met its
own draw halfway. Parallel is the setup form's DEFAULT wiring.

This rig closes the loop through ``run_hub_calculation`` and the real permit
pipeline (``control.smoothing.apply_smoothing``) against a plant: one inverter
rated 6 kW, output metered on phase A, carrying a steady 1 kW house from its
battery at night, and a charger that slews toward its command. The output
sensor reads house + car, the battery sensor the same power, the charger the
car. The house never moves, so the right permit is the inverter's rating less
the house - 21.74 A - on every cycle, and the output must never pass 6 kW.
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
    ENTRY_TYPE_INVERTER,
    ENTRY_TYPE_LOAD,
    WIRING_TOPOLOGY_PARALLEL,
    WIRING_TOPOLOGY_SERIES,
)

V = 230.0
RATING_W = 6000.0
RATING_A = RATING_W / V               # 26.09 A: the inverter carries all of it
HOUSE_A = 1000.0 / V                  # a steady 1 kW house
ALLOWANCE_A = RATING_A - HOUSE_A      # 21.74 A: all the charger may take
DT = 2                                # the default site cycle, seconds
START = 30                            # cycles on the household alone first
OUTPUT = "sensor.ogp_inverter_out_a"
BATTERY = "sensor.ogp_battery_power"
CAR = "sensor.ogp_evse_current"
STATUS = "sensor.ogp_evse_status_connector"
# The mixed fleet's second member: an AC-coupled PV inverter on the same bus.
PV_OUTPUT = "sensor.ogp_pv_out_a"
PV_A = 500.0 / V


def _hub(slug, topology):
    """No grid CT at all: one inverter, its output metered on A."""
    return MockConfigEntry(
        domain=DOMAIN, version=2, minor_version=4, title=f"Off-grid Hub {slug}",
        data={CONF_NAME: f"Off-grid Hub {slug}",
              CONF_ENTITY_ID: f"ogp_hub_{slug}",
              ENTRY_TYPE: ENTRY_TYPE_HUB},
        options={
            CONF_PHASE_VOLTAGE: int(V),
            CONF_MAIN_BREAKER_RATING: 40,
            CONF_INVERTER_OUTPUT_PHASE_A_ENTITY_ID: OUTPUT,
            CONF_INVERTER_MAX_POWER: int(RATING_W),
            CONF_WIRING_TOPOLOGY: topology,
            CONF_BATTERY_SOC_ENTITY_ID: "sensor.ogp_battery_soc",
            CONF_BATTERY_POWER_ENTITY_ID: BATTERY,
            # A large pack, so the inverter's rating binds and not the battery.
            CONF_BATTERY_MAX_DISCHARGE_POWER: 20000,
            CONF_BATTERY_MAX_CHARGE_POWER: 5000,
        },
    )


def _pv_inverter(hub):
    """An unrated AC-coupled PV inverter beside the hub's own hybrid."""
    return MockConfigEntry(
        domain=DOMAIN, version=2, minor_version=4, title="Off-grid PV",
        data={CONF_NAME: "Off-grid PV",
              CONF_ENTITY_ID: "ogp_pv",
              ENTRY_TYPE: ENTRY_TYPE_INVERTER,
              CONF_HUB_ENTRY_ID: hub.entry_id},
        options={
            CONF_INVERTER_OUTPUT_PHASE_A_ENTITY_ID: PV_OUTPUT,
            CONF_WIRING_TOPOLOGY: WIRING_TOPOLOGY_PARALLEL,
        },
    )


def _evse(hub):
    """A 1-phase 6→32 A Standard EVSE: the inverter binds, not the car."""
    return MockConfigEntry(
        domain=DOMAIN, version=2, minor_version=4, title="Off-grid EVSE",
        data={CONF_NAME: "Off-grid EVSE",
              CONF_ENTITY_ID: "ogp_evse",
              ENTRY_TYPE: ENTRY_TYPE_LOAD,
              CONF_CHARGER_ID: "ogp_evse",
              CONF_EVSE_CURRENT_IMPORT_ENTITY_ID: CAR,
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


async def _run(hass, slug, topology, car_ramp_a_s, cycles=200, mixed=False):
    """Close the loop: engine permit → permit pipeline → car → inverter → engine.

    The connector reads Available for ``START`` cycles, so every input EMA is
    settled on the household alone; then the car plugs in and slews toward its
    last command at ``car_ramp_a_s``. ``mixed`` adds a parallel PV inverter
    putting out ``PV_A`` on the same bus, so the hub's own inverter carries
    the rest. One row per cycle.
    """
    from freezegun import freeze_time
    from custom_components.dynamic_ocpp_evse.control.smoothing import (
        apply_smoothing,
    )
    from custom_components.dynamic_ocpp_evse.engine.hub_calculation import (
        run_hub_calculation,
    )

    hub = _hub(slug, topology)
    hub.add_to_hass(hass)
    if mixed:
        _pv_inverter(hub).add_to_hass(hass)
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
            "dynamic_control": True,
        }},
        "load_allocations": {evse.entry_id: 0},
        "inverters": {},
    }
    hass.states.async_set(STATUS, "Available")
    hass.states.async_set(
        "sensor.ogp_battery_soc", "80",
        {"device_class": "battery", "unit_of_measurement": "%"})
    pv_a = PV_A if mixed else 0.0
    if mixed:
        attrs, state = _amps(pv_a)
        hass.states.async_set(PV_OUTPUT, state, attrs)
    # apply_smoothing keeps its state on the load entity and touches only these.
    permit_state = SimpleNamespace(
        _attr_name="ogp_evse", _ema_current=None, _schmitt_current=None,
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
            if i >= START:
                draw += max(-step, min(step, command - draw))
            supply = HOUSE_A + draw
            # The hub's own inverter carries whatever the PV inverter does not,
            # all of it from its battery at night.
            attrs, state = _amps(supply - pv_a)
            hass.states.async_set(OUTPUT, state, attrs)
            hass.states.async_set(
                BATTERY, f"{(supply - pv_a) * V:.1f}",
                {"device_class": "power", "unit_of_measurement": "W"})
            attrs, state = _amps(draw)
            hass.states.async_set(CAR, state, attrs)
            result = run_hub_calculation(hass, hub)
            # The load processor's own rounding (entities/load.py).
            permit = round(result["load_available"][evse.entry_id], 1)
            command = apply_smoothing(permit_state, permit, False, hub)
            trace.append({"i": i, "draw": draw, "permit": permit,
                          "command": command, "supply": supply,
                          "household_w": result["household_power"]})
    return trace


def _over_w(trace, key, limit_a):
    return max(0.0, max(row[key] - limit_a for row in trace)) * V


@pytest.mark.parametrize("topology", [WIRING_TOPOLOGY_PARALLEL, WIRING_TOPOLOGY_SERIES])
@pytest.mark.parametrize("car_ramp_a_s", [1.0, 100.0], ids=["car-1A/s", "step"])
async def test_a_charger_gets_the_inverter_rating_less_the_house(
    hass, car_ramp_a_s, topology
):
    """The charger reaches the inverter's rating less the house, on either
    wiring, and the inverter never passes its rating while the car starts.

    Measured against this rig (6 kW inverter, 1 kW house, allowance 21.74 A),
    either car:

    ======================================  ============  =============
    off-grid household                      car settles   output > 6 kW
    ======================================  ============  =============
    parallel, draw not taken off (before)   10.9 A        never
    parallel, the smoothed draw taken off   21.7 A        never
    series (unchanged)                      21.7 A        never
    ======================================  ============  =============

    10.9 A is the fixed point of the self-counting: permit = allowance − car,
    so the car meets its own draw halfway. The budget is one step of the
    permit's own 0.1 A rounding.
    """
    trace = await _run(hass, f"{topology}{car_ramp_a_s:g}", topology, car_ramp_a_s)

    settled = trace[-1]["draw"]
    shortfall_w = (ALLOWANCE_A - settled) * V
    assert settled >= ALLOWANCE_A - 0.1, (
        f"{topology}: car settled at {settled:.1f} A against the inverter's "
        f"{ALLOWANCE_A:.1f} A allowance - {shortfall_w:.0f} W short: its own "
        f"draw was counted as house load"
    )
    # And not by going past the inverter while it started.
    permit_over = _over_w(trace, "permit", ALLOWANCE_A)
    supply_over = _over_w(trace, "supply", RATING_A)
    assert permit_over <= 0.1 * V, (
        f"{topology}: permit {permit_over:.0f} W over the inverter's allowance"
    )
    assert supply_over <= 0.1 * V, (
        f"{topology}: inverter output {supply_over:.0f} W over its "
        f"{RATING_W:.0f} W rating"
    )


@pytest.mark.parametrize("car_ramp_a_s", [1.0, 100.0], ids=["car-1A/s", "step"])
async def test_both_wirings_give_the_same_permits_off_grid(hass, car_ramp_a_s):
    """Off-grid the wiring does not change what the output contains, so the
    two formulas must agree: the same rig on parallel and on series issues the
    same permit and the car draws the same current, cycle for cycle. Before
    2026-09-24 the parallel permits parted from the series ones on the first
    cycle the car drew current.
    """
    parallel = await _run(
        hass, f"agree_p{car_ramp_a_s:g}", WIRING_TOPOLOGY_PARALLEL, car_ramp_a_s)
    series = await _run(
        hass, f"agree_s{car_ramp_a_s:g}", WIRING_TOPOLOGY_SERIES, car_ramp_a_s)

    differing = [
        (p["i"], p["permit"], s["permit"])
        for p, s in zip(parallel, series)
        if (p["permit"], p["draw"]) != (s["permit"], s["draw"])
    ]
    assert not differing, (
        f"{len(differing)} cycles where parallel and series disagree off-grid, "
        f"first (cycle, parallel permit, series permit): {differing[0]}"
    )


async def test_the_published_household_is_the_house_not_the_charger(hass):
    """The published household figure is built from the same per-phase
    household (engine/hub_result.py), so on the parallel wiring it read the
    inverter's whole output, the car included: 3508 W against a 1000 W house
    with the car settled at 10.9 A. Now it is the house, within the permit's
    0.1 A rounding.
    """
    trace = await _run(hass, "published", WIRING_TOPOLOGY_PARALLEL, 100.0)

    last = trace[-1]
    assert last["household_w"] == pytest.approx(HOUSE_A * V, abs=0.1 * V), (
        f"household published as {last['household_w']:.0f} W with a "
        f"{HOUSE_A * V:.0f} W house and the car drawing {last['draw'] * V:.0f} W"
    )


@pytest.mark.parametrize("car_ramp_a_s", [1.0, 100.0], ids=["car-1A/s", "step"])
async def test_a_mixed_fleet_takes_the_draw_off_once(hass, car_ramp_a_s):
    """A mixed fleet off-grid - the hub's series hybrid plus a parallel PV
    inverter on the same bus - takes the managed draw off exactly once: the
    composite household is the parallel output plus the series output less
    the draw (hub_calculation._mixed_household_per_phase). The parallel half
    must not take it off a second time now that the parallel formula does so
    off-grid; if it did, the household would read low by the car's draw and
    the permit would run away from the allowance with the car.

    The fleet's rating is the hybrid's 6 kW (the PV inverter has none), so
    the allowance is the same 21.74 A as the single-inverter rig. Before and
    after this change: the car settles at 21.7 A and the permit never passes
    the allowance.
    """
    trace = await _run(
        hass, f"mixed{car_ramp_a_s:g}", WIRING_TOPOLOGY_SERIES, car_ramp_a_s,
        mixed=True,
    )

    settled = trace[-1]["draw"]
    assert settled >= ALLOWANCE_A - 0.1, (
        f"mixed fleet: car settled at {settled:.1f} A against the "
        f"{ALLOWANCE_A:.1f} A allowance - "
        f"{(ALLOWANCE_A - settled) * V:.0f} W short"
    )
    permit_over = _over_w(trace, "permit", ALLOWANCE_A)
    assert permit_over <= 0.1 * V, (
        f"mixed fleet: permit {permit_over:.0f} W over the allowance - the "
        f"draw came off the household twice"
    )
