"""The off-grid household when no solar production is being counted.

Machine-authored tests - not yet human-reviewed.

An off-grid site with no inverter output sensors has one measure of what it
draws: the supply on its AC bus, solar production plus battery power
(discharging positive). The house is that supply less our own managed draws,
and the inverter's allowance for the managed loads is its rating less the
house (``target_calculator._calculate_inverter_limit``). 5d53dbf takes the
managed draw off when a solar sensor reads production; this file covers the
two sites where the house total was not built at all, because of a
``solar > 0`` guard in ``_apply_household_figures``:

* **battery only** - no solar sensor, so solar is derived (from battery
  charging) and the guard, which wants a measured production figure, never
  passes;
* **a solar sensor reading 0 W** - at night, which is a real reading, not an
  absent one.

A third site cannot know its supply at all: a battery whose power is not read
(no power sensor, or one unavailable past the stale timeout). There the
inverter now hands out nothing rather than the whole rating.

With no house total and no output sensors, the household falls back to the
site's grid consumption, which off-grid is a synthetic 0 A. The whole rating
was handed out as headroom, the car took it, and the inverter ran over its
rating by the house. At dusk the same site changed its mind the cycle the
smoothed solar reached 0.00 W: from rating less the house to the whole rating.

The rig is the one in test_offgrid_solar_household.py: ``run_hub_calculation``
and the real permit pipeline (``control.smoothing.apply_smoothing``) closed
against a plant - a 6 kW inverter carrying a steady 1 kW house, the battery
balancing the bus (it reads ``house + car - solar``) and a charger that slews
toward its command. The right permit is the rating less the house, 21.74 A,
on every cycle, day or night.
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
    CONF_LOAD_PRIORITY,
    CONF_MAIN_BREAKER_RATING,
    CONF_NAME,
    CONF_PHASES,
    CONF_PHASE_VOLTAGE,
    CONF_SOLAR_PRODUCTION_ENTITY_ID,
    CONF_WIRING_TOPOLOGY,
    DOMAIN,
    ENTRY_TYPE,
    ENTRY_TYPE_HUB,
    ENTRY_TYPE_LOAD,
    WIRING_TOPOLOGY_PARALLEL,
    WIRING_TOPOLOGY_SERIES,
)

V = 230.0
RATING_W = 6000.0
RATING_A = RATING_W / V               # 26.09 A: the inverter carries all of it
HOUSE_A = 1000.0 / V                  # a steady 1 kW house
ALLOWANCE_A = RATING_A - HOUSE_A      # 21.74 A: all the charger may take
DAY_SOLAR_W = 2000.0                  # what the array makes before dusk
DT = 2                                # the default site cycle, seconds
START = 30                            # cycles on the household alone first
# One step of the permit's own 0.1 A rounding (entities/load.py).
ROUNDING_W = 0.1 * V
SOLAR = "sensor.ogb_solar_power"
BATTERY = "sensor.ogb_battery_power"
SOC = "sensor.ogb_battery_soc"
CAR = "sensor.ogb_evse_current"
STATUS = "sensor.ogb_evse_status_connector"


def _hub(slug, topology, solar_sensor, battery_sensor=True):
    """No grid CT and no inverter output sensor: the battery's SOC, its power
    unless not ``battery_sensor``, and a solar production sensor only when
    ``solar_sensor``."""
    options = {
        CONF_PHASE_VOLTAGE: int(V),
        CONF_MAIN_BREAKER_RATING: 40,
        CONF_INVERTER_MAX_POWER: int(RATING_W),
        CONF_WIRING_TOPOLOGY: topology,
        CONF_BATTERY_SOC_ENTITY_ID: SOC,
        # A large pack, so the inverter's rating binds and not the battery.
        CONF_BATTERY_MAX_DISCHARGE_POWER: 20000,
        CONF_BATTERY_MAX_CHARGE_POWER: 5000,
    }
    if battery_sensor:
        options[CONF_BATTERY_POWER_ENTITY_ID] = BATTERY
    if solar_sensor:
        options[CONF_SOLAR_PRODUCTION_ENTITY_ID] = SOLAR
    return MockConfigEntry(
        domain=DOMAIN, version=2, minor_version=4, title=f"Off-grid Hub {slug}",
        data={CONF_NAME: f"Off-grid Hub {slug}",
              CONF_ENTITY_ID: f"ogb_hub_{slug}",
              ENTRY_TYPE: ENTRY_TYPE_HUB},
        options=options,
    )


def _evse(hub):
    """A 1-phase 6→32 A Standard EVSE: the inverter binds, not the car."""
    return MockConfigEntry(
        domain=DOMAIN, version=2, minor_version=4, title="Off-grid EVSE",
        data={CONF_NAME: "Off-grid EVSE",
              CONF_ENTITY_ID: "ogb_evse",
              ENTRY_TYPE: ENTRY_TYPE_LOAD,
              CONF_CHARGER_ID: "ogb_evse",
              CONF_EVSE_CURRENT_IMPORT_ENTITY_ID: CAR,
              CONF_HUB_ENTRY_ID: hub.entry_id},
        options={
            CONF_LOAD_PRIORITY: 1,
            CONF_EVSE_MINIMUM_CHARGE_CURRENT: 6,
            CONF_EVSE_MAXIMUM_CHARGE_CURRENT: 32,
            CONF_PHASES: 1,
        },
    )


def _watts(value):
    return {"device_class": "power", "unit_of_measurement": "W"}, f"{value:.1f}"


async def _run(hass, slug, topology, car_ramp_a_s, solar_w, solar_sensor,
               cycles=200, battery_sensor=True, battery_dies_at=None):
    """Close the loop: engine permit → permit pipeline → car → battery → engine.

    ``solar_w(i)`` is what the array makes on cycle ``i``; the solar sensor
    (when there is one) reads it and the battery balances the rest - until
    cycle ``battery_dies_at``, from which the battery power sensor reads
    unavailable. The
    connector reads Available for ``START`` cycles, so every input EMA is
    settled on the household alone; then the car plugs in and slews toward its
    last command at ``car_ramp_a_s``. One row per cycle.
    """
    from freezegun import freeze_time
    from custom_components.dynamic_ocpp_evse.control.smoothing import (
        apply_smoothing,
    )
    from custom_components.dynamic_ocpp_evse.engine.hub_calculation import (
        run_hub_calculation,
    )

    hub = _hub(slug, topology, solar_sensor, battery_sensor)
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
            "dynamic_control": True,
        }},
        "load_allocations": {evse.entry_id: 0},
        "inverters": {},
    }
    hass.states.async_set(STATUS, "Available")
    hass.states.async_set(
        SOC, "80", {"device_class": "battery", "unit_of_measurement": "%"})
    # apply_smoothing keeps its state on the load entity and touches only these.
    permit_state = SimpleNamespace(
        _attr_name="ogb_evse", _ema_current=None, _schmitt_current=None,
        _schmitt_state="rising", _rate_limited_current=0.0,
    )

    trace = []
    draw = command = 0.0
    step = car_ramp_a_s * DT
    with freeze_time("2026-09-24 20:00:00+00:00") as frozen:
        for i in range(cycles):
            frozen.tick(DT)
            if i == START:
                hass.states.async_set(STATUS, "Charging")
            if i >= START:
                draw += max(-step, min(step, command - draw))
            solar = solar_w(i)
            supply = HOUSE_A + draw
            if solar_sensor:
                attrs, state = _watts(solar)
                hass.states.async_set(SOLAR, state, attrs)
            # Off-grid the battery balances the bus: + discharging, − charging.
            attrs, state = _watts(supply * V - solar)
            if battery_dies_at is not None and i >= battery_dies_at:
                attrs, state = {}, "unavailable"
            hass.states.async_set(BATTERY, state, attrs)
            hass.states.async_set(
                CAR, f"{draw:.3f}",
                {"device_class": "current", "unit_of_measurement": "A"})
            result = run_hub_calculation(hass, hub)
            # The load processor's own rounding (entities/load.py).
            permit = round(result["load_available"][evse.entry_id], 1)
            command = apply_smoothing(permit_state, permit, False, hub)
            trace.append({"i": i, "draw": draw, "permit": permit,
                          "command": command, "supply": supply,
                          "solar": solar})
    return trace


def _over_w(trace, key, limit_a):
    return max(0.0, max(row[key] - limit_a for row in trace)) * V


def _assert_rating_less_the_house(trace, what):
    """The charger settles at the rating less the house - and the permit and
    the inverter's output never go past it on the way."""
    settled = trace[-1]["draw"]
    supply_over = _over_w(trace, "supply", RATING_A)
    permit_over = _over_w(trace, "permit", ALLOWANCE_A)
    peak = max(row["permit"] for row in trace)
    assert supply_over <= ROUNDING_W, (
        f"{what}: inverter output {supply_over:.0f} W over its "
        f"{RATING_W:.0f} W rating - the car was permitted up to {peak:.1f} A "
        f"against an allowance of {ALLOWANCE_A:.1f} A (rating less the 1 kW "
        f"house), so the house was left out of the household"
    )
    assert permit_over <= ROUNDING_W, (
        f"{what}: permit up to {peak:.1f} A, {permit_over:.0f} W past the "
        f"{ALLOWANCE_A:.1f} A allowance"
    )
    assert settled >= ALLOWANCE_A - 0.1, (
        f"{what}: car settled at {settled:.1f} A against the "
        f"{ALLOWANCE_A:.1f} A allowance - "
        f"{(ALLOWANCE_A - settled) * V:.0f} W short"
    )


TOPOLOGIES = [WIRING_TOPOLOGY_SERIES, WIRING_TOPOLOGY_PARALLEL]
CARS = pytest.mark.parametrize(
    "car_ramp_a_s", [1.0, 100.0], ids=["car-1A/s", "step"])


@pytest.mark.parametrize("topology", TOPOLOGIES)
@CARS
async def test_battery_only_charger_gets_the_rating_less_the_house(
    hass, car_ramp_a_s, topology
):
    """No solar sensor: the battery's discharge is the whole supply.

    Measured against this rig (6 kW inverter, 1 kW house, allowance 21.74 A),
    either topology, either car:

    ================================  ==========  ====================
    household                         car gets    inverter past 6 kW
    ================================  ==========  ====================
    not built (before 2026-09-24)     26.1 A      1003 W - the house
    battery − managed draw            21.7 A      never
    ================================  ==========  ====================
    """
    trace = await _run(
        hass, f"bat{topology}{car_ramp_a_s:g}", topology, car_ramp_a_s,
        solar_w=lambda i: 0.0, solar_sensor=False,
    )
    _assert_rating_less_the_house(trace, "battery only")


@pytest.mark.parametrize("topology", TOPOLOGIES)
@CARS
async def test_solar_sensor_at_night_charger_gets_the_rating_less_the_house(
    hass, car_ramp_a_s, topology
):
    """A solar sensor reading 0 W is a reading: solar + battery is still the
    supply, and it is all battery.

    Same rig, same figures as battery only: before 2026-09-24 the car got
    26.1 A and the inverter ran 1003 W over its rating; now 21.7 A, never
    over.
    """
    trace = await _run(
        hass, f"night{topology}{car_ramp_a_s:g}", topology, car_ramp_a_s,
        solar_w=lambda i: 0.0, solar_sensor=True,
    )
    _assert_rating_less_the_house(trace, "solar sensor at 0 W")


DUSK_FROM = START + 50                # the car has long settled on daylight
DUSK_CYCLES = 30                      # the array fades to 0 W over a minute


def _dusk(i):
    if i < DUSK_FROM:
        return DAY_SOLAR_W
    return max(0.0, DAY_SOLAR_W * (1 - (i - DUSK_FROM) / DUSK_CYCLES))


@pytest.mark.parametrize("topology", TOPOLOGIES)
async def test_dusk_does_not_hand_the_house_to_the_charger(hass, topology):
    """The permit does not move when the array fades out.

    The house is the same 1 kW before and after dusk, so the allowance is the
    same 21.74 A. Before 2026-09-24 the house total stopped being built the
    cycle the smoothed solar reading reached 0.00 W - 58 s after the array
    itself - and the permit stepped from 21.7 A to 26.1 A in one cycle: the
    car followed and the inverter ran 1003 W over its rating for the rest of
    the night. Now the permit holds at 21.7 A through dusk.
    """
    trace = await _run(
        hass, f"dusk{topology}", topology, 1.0,
        solar_w=_dusk, solar_sensor=True, cycles=260,
    )
    assert trace[-1]["solar"] == 0.0 and trace[DUSK_FROM - 1]["solar"] > 0
    _assert_rating_less_the_house(trace, "through dusk")
    daylight = [row["permit"] for row in trace[START + 30:DUSK_FROM]]
    night = [row["permit"] for row in trace[-30:]]
    assert max(night) - min(daylight) <= 0.1, (
        f"the permit moved from {min(daylight):.1f} A in daylight to "
        f"{max(night):.1f} A after dusk with the same 1 kW house"
    )


@pytest.mark.parametrize(
    "solar_w", [0.0, DAY_SOLAR_W], ids=["night", "day"])
async def test_an_unread_battery_hands_out_nothing(hass, solar_w):
    """With the battery's power not read, the supply is unknowable.

    A solar sensor and a battery SOC sensor but no battery power sensor (the
    hub already reports it as *Setup incomplete*): solar + battery is the
    supply, and the battery half of it is not measured - nothing on the site
    says what the inverter is carrying. Before 2026-09-24 the household read
    ``solar - managed`` by day (the battery's share of the house and the car
    left out) and nothing at all by night; either way it reached 0, the car
    was given the whole 26.1 A and the inverter ran 1003 W over its rating.
    No figure measured from the data can stand in for the house, so the
    inverter hands out nothing - as it already did on a site that reads
    neither solar nor battery.
    """
    trace = await _run(
        hass, f"unread{solar_w:g}", WIRING_TOPOLOGY_SERIES, 1.0,
        solar_w=lambda i: solar_w, solar_sensor=True, battery_sensor=False,
    )
    supply_over = _over_w(trace, "supply", RATING_A)
    peak = max(row["permit"] for row in trace)
    assert supply_over <= ROUNDING_W, (
        f"battery power unread: inverter output {supply_over:.0f} W over its "
        f"{RATING_W:.0f} W rating - the car was permitted up to {peak:.1f} A "
        f"on a household nothing measured"
    )
    assert peak == 0.0, (
        f"battery power unread: the car was permitted {peak:.1f} A with "
        f"nothing measuring what the inverter carries"
    )


async def test_a_battery_sensor_that_dies_hands_out_nothing(hass):
    """A battery power sensor that goes unavailable at night, mid-charge.

    Its last reading is held for the stale timeout (``INPUT_STALE_TIMEOUT``,
    60 s - 30 cycles here) and the permit with it; after that the reading is
    dropped, nothing measures the supply any more, and the inverter hands out
    nothing - where before 2026-09-24 the car had had the whole 26.1 A all
    night, 1003 W over the rating.
    """
    dies = START + 90
    trace = await _run(
        hass, "dies", WIRING_TOPOLOGY_SERIES, 1.0,
        solar_w=lambda i: 0.0, solar_sensor=True, battery_dies_at=dies,
    )
    supply_over = _over_w(trace, "supply", RATING_A)
    peak = max(row["permit"] for row in trace)
    assert supply_over <= ROUNDING_W, (
        f"battery sensor dying at night: inverter output {supply_over:.0f} W "
        f"over its {RATING_W:.0f} W rating - the car was permitted up to "
        f"{peak:.1f} A against an allowance of {ALLOWANCE_A:.1f} A"
    )
    held = [row["permit"] for row in trace[dies:dies + 30]]
    assert min(held) >= ALLOWANCE_A - 0.1, (
        f"the last battery reading is held for the stale timeout, and the "
        f"permit with it: {min(held):.1f} A"
    )
    dropped = [row["permit"] for row in trace[dies + 31:]]
    assert max(dropped) == 0.0, (
        f"the battery's power has not been read for over a minute and the "
        f"car was still permitted {max(dropped):.1f} A"
    )
