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

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.dynamic_ocpp_evse.const import (
    CONF_BATTERY_MAX_CHARGE_POWER,
    CONF_BATTERY_MAX_DISCHARGE_POWER,
    CONF_BATTERY_POWER_ENTITY_ID,
    CONF_BATTERY_SOC_ENTITY_ID,
    CONF_ENTITY_ID,
    CONF_HUB_ENTRY_ID,
    CONF_INVERTER_MAX_POWER,
    CONF_INVERTER_OUTPUT_PHASE_A_ENTITY_ID,
    CONF_MAIN_BREAKER_RATING,
    CONF_NAME,
    CONF_PHASE_VOLTAGE,
    CONF_WIRING_TOPOLOGY,
    DOMAIN,
    EVSE_MODE_SOLAR_ONLY,
    ENTRY_TYPE,
    ENTRY_TYPE_HUB,
    ENTRY_TYPE_INVERTER,
    WIRING_TOPOLOGY_PARALLEL,
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


async def _run(hass, slug, topology, car_ramp_a_s, cycles=200, mixed=False,
               solar_w=0.0, mode=None):
    """A 1-phase 6→32 A Standard EVSE on the inverter: it binds, not the car.

    The connector reads Available for ``START`` cycles, so every input EMA is
    settled on the household alone; then the car plugs in and slews toward its
    last command at ``car_ramp_a_s``. ``mixed`` adds a parallel PV inverter
    putting out ``PV_A`` on the same bus, so the hub's own inverter carries
    the rest. ``solar_w`` is the hub inverter's own (unmetered) production:
    its battery carries the supply less that, charging when it is negative.
    ``mode`` is the car's operating mode (Standard when None). One row per
    cycle.
    """
    hub = _hub(slug, topology)
    pv_a = PV_A if mixed else 0.0
    if mixed:
        _pv_inverter(hub).add_to_hass(hass)
        hass.states.async_set(PV_OUTPUT, f"{pv_a:.3f}", AMPS)
    hass.states.async_set(STATUS, "Available")
    hass.states.async_set("sensor.ogp_battery_soc", "80", BATTERY_PCT)
    draw = 0.0

    def plant(i, command):
        nonlocal draw
        if i == START:
            hass.states.async_set(STATUS, "Charging")
        if i >= START:
            draw = slew(draw, command, car_ramp_a_s * DT)
        supply = HOUSE_A + draw
        # The hub's own inverter carries whatever the PV inverter does not,
        # all of it from its battery at night.
        hass.states.async_set(OUTPUT, f"{supply - pv_a:.3f}", AMPS)
        hass.states.async_set(
            BATTERY, f"{(supply - pv_a) * V - solar_w:.1f}", WATTS)
        hass.states.async_set(CAR, f"{draw:.3f}", AMPS)
        return {"draw": draw, "supply": supply}

    return await close_loop(
        hass, hub, evse_entry(hub, "ogp_evse"), cycles, plant,
        at="2026-09-24 22:00:00+00:00", hub_data=SOC_BOUNDS,
        load_data={"operating_mode": mode} if mode else None)


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
    permit_over = over_w(trace, "permit", ALLOWANCE_A)
    supply_over = over_w(trace, "supply", RATING_A)
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
    household_w = last["result"]["household_power"]
    assert household_w == pytest.approx(HOUSE_A * V, abs=0.1 * V), (
        f"household published as {household_w:.0f} W with a "
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
    permit_over = over_w(trace, "permit", ALLOWANCE_A)
    assert permit_over <= 0.1 * V, (
        f"mixed fleet: permit {permit_over:.0f} W over the allowance - the "
        f"draw came off the household twice"
    )


@pytest.mark.parametrize("topology", [WIRING_TOPOLOGY_PARALLEL, WIRING_TOPOLOGY_SERIES])
@pytest.mark.parametrize("solar_w", [0.0, 3000.0], ids=["night", "day"])
async def test_the_published_solar_is_the_panels_not_the_battery(
    hass, solar_w, topology
):
    """Off-grid the output is the site's supply on either wiring - solar plus
    the battery's flow - so the derived solar is the output less the battery
    power (``fleet.member_solar``). On the parallel wiring it was the whole
    output: at night, with the car settled, 5992 W of "solar" that was all
    battery. Checked with the car off (the house alone; by day the battery
    charging on the rest) and settled (the battery covering what the panels
    do not):

    =====================  ==========  ===========  ==========  ===========
    published solar        night, off  night, car   day, off    day, car
    =====================  ==========  ===========  ==========  ===========
    parallel, before       1000 W      5992 W       1000 W      5992 W
    parallel, after        0 W         0 W          3000 W      3000 W
    series (unchanged)     0 W         0 W          3000 W      3000 W
    =====================  ==========  ===========  ==========  ===========
    """
    trace = await _run(
        hass, f"solar_{topology}{solar_w:g}", topology, 100.0, solar_w=solar_w
    )

    for label, row in (("car off", trace[START - 1]), ("car settled", trace[-1])):
        published = row["result"]["solar_power"]
        assert published == pytest.approx(solar_w, abs=5), (
            f"{topology}, {label}: solar published as {published:.0f} W "
            f"with {solar_w:.0f} W from the panels and the battery at "
            f"{(row['supply'] * V) - solar_w:.0f} W"
        )


@pytest.mark.parametrize("solar_w", [0.0, 3000.0], ids=["night", "day"])
async def test_a_solar_only_car_gets_the_same_on_either_wiring(hass, solar_w):
    """The solar pool bounds the battery's surplus above its SOC target (80 %
    against 50 % here) by the inverter's headroom, rating less solar less the
    discharge in flight (target_calculator._calculate_solar_surplus). With
    the parallel "solar" the whole output, the discharge was in it twice: the
    output was set against the rating twice, past half the rating the battery
    offered nothing more, and a Solar Only car was held at what it drew, far
    short of the series one. Off-grid the wiring does not change
    what the output contains, so the two must agree cycle for cycle.

    ================  =================  ===============  =============
    Solar Only car    parallel, before   series, before   after, either
    ================  =================  ===============  =============
    night             12.4 A (-2139 W)   21.7 A           21.7 A
    day, 3 kW panels  18.0 A (-851 W)    21.7 A           21.7 A
    ================  =================  ===============  =============
    """
    mode = EVSE_MODE_SOLAR_ONLY.key
    parallel = await _run(
        hass, f"so_p{solar_w:g}", WIRING_TOPOLOGY_PARALLEL, 100.0,
        solar_w=solar_w, mode=mode)
    series = await _run(
        hass, f"so_s{solar_w:g}", WIRING_TOPOLOGY_SERIES, 100.0,
        solar_w=solar_w, mode=mode)

    assert parallel[-1]["draw"] >= series[-1]["draw"] - 0.1, (
        f"Solar Only car settled at {parallel[-1]['draw']:.1f} A on parallel "
        f"against {series[-1]['draw']:.1f} A on series "
        f"({(series[-1]['draw'] - parallel[-1]['draw']) * V:.0f} W short)"
    )
    differing = [
        (p["i"], p["permit"], s["permit"])
        for p, s in zip(parallel, series)
        if (p["permit"], p["draw"]) != (s["permit"], s["draw"])
    ]
    assert not differing, (
        f"{len(differing)} cycles where parallel and series disagree, first "
        f"(cycle, parallel permit, series permit): {differing[0]}"
    )


@pytest.mark.parametrize("topology", [WIRING_TOPOLOGY_PARALLEL, WIRING_TOPOLOGY_SERIES])
@pytest.mark.parametrize("solar_w", [0.0, 3000.0], ids=["night", "day"])
async def test_the_published_solar_remaining_is_the_solar_less_the_house(
    hass, solar_w, topology
):
    """Solar Remaining Power is what solar offers the managed loads: the
    production less the house, as grid-tied with a production sensor
    (``solar - household``, the household the inverters serve - off-grid that
    is all of it), and what the engine's off-grid solar pool is made of short
    of the battery's surplus: the draw our loads hold plus the battery's
    charge less its discharge, which by the bus balance is the same figure.
    Off-grid with output sensors the household total is never built, so the
    figure fell through to the whole production - the house taken off
    nothing. Read through the real sensors, car off and settled alike:

    =====================  ==========  ===========  ==========  ===========
    Solar Remaining Power  night, off  night, car   day, off    day, car
    =====================  ==========  ===========  ==========  ===========
    either wiring, before  0 W         0 W          3000 W      3000 W
    either wiring, after   0 W         0 W          2000 W      2000 W
    =====================  ==========  ===========  ==========  ===========
    """
    from custom_components.dynamic_ocpp_evse.entities.hub import publish_hub_data
    from custom_components.dynamic_ocpp_evse.sensor import (
        LoadJugglerHubDataSensor,
        HUB_SENSOR_DEFINITIONS,
    )

    slug = f"remaining_{topology}{solar_w:g}"
    trace = await _run(hass, slug, topology, 100.0, solar_w=solar_w)
    hub = next(
        e for e in hass.config_entries.async_entries(DOMAIN)
        if e.data.get(CONF_ENTITY_ID) == f"ogp_hub_{slug}"
    )
    sensors = {
        d.data_key: LoadJugglerHubDataSensor(hass, hub, "Hub", slug, d)
        for d in HUB_SENSOR_DEFINITIONS
        if d.data_key in ("available_solar_power", "available_solar_current")
    }

    expected_w = max(0.0, solar_w - HOUSE_A * V)
    for label, row in (("car off", trace[START - 1]), ("car settled", trace[-1])):
        publish_hub_data(hass, hub.entry_id, row["result"])
        for sensor in sensors.values():
            await sensor.async_update()
        power = sensors["available_solar_power"].native_value
        current = sensors["available_solar_current"].native_value
        assert power == pytest.approx(expected_w, abs=0.1 * V), (
            f"{topology}, {label}: Solar Remaining Power {power:.0f} W with "
            f"{solar_w:.0f} W of solar and a {HOUSE_A * V:.0f} W house"
        )
        assert current == pytest.approx(expected_w / V, abs=0.1)
