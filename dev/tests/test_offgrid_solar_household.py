"""The off-grid household on a site metered by a SOLAR sensor alone.

Machine-authored tests - not yet human-reviewed.

An off-grid site with a solar production sensor and no inverter output
sensors has one figure for everything the site draws: solar + battery power
(discharging positive). With no grid, that is the inverters' whole supply -
the household AND our own managed loads. ``_apply_household_figures`` turns it
into ``household_consumption_total`` as ``solar + battery - export``, and
grid-tied that is right, because the feedback loop has already added the
managed draws back onto the export. Off-grid the feedback loop leaves the
synthetic zero phases alone (``_apply_feedback_loop``), so until 2026-09-24
nothing took the draws off at all: a charger's own draw was read as house
load, the inverter's allowance for it (rating - household) shrank by that
draw, and the car settled at about half of what it was meant to have.

This rig closes the loop through ``run_hub_calculation`` and the real permit
pipeline (``control.smoothing.apply_smoothing``) against a plant: a 6 kW
inverter with a steady 1 kW house, 2 kW of solar and the battery covering the
rest, and a charger that slews toward its command. The solar sensor reads the
array, the battery sensor reads ``house + car - solar``, the charger reads the
car. The house never moves, so the right permit is the inverter's rating less
the house - 21.74 A - on every cycle.
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
SOLAR_W = 2000.0                      # daytime: the solar sensor reads > 0
DT = 2                                # the default site cycle, seconds
START = 30                            # cycles on the household alone first
SOLAR = "sensor.ogs_solar_power"
BATTERY = "sensor.ogs_battery_power"
CAR = "sensor.ogs_evse_current"
STATUS = "sensor.ogs_evse_status_connector"


def _hub(slug, topology, *, solar=True, battery_power=True):
    """No grid CT and no inverter output sensor: solar + battery only.
    ``solar`` / ``battery_power`` False leave that sensor unconfigured."""
    options = {
        CONF_PHASE_VOLTAGE: int(V),
        CONF_MAIN_BREAKER_RATING: 40,
        CONF_SOLAR_PRODUCTION_ENTITY_ID: SOLAR,
        CONF_INVERTER_MAX_POWER: int(RATING_W),
        CONF_WIRING_TOPOLOGY: topology,
        CONF_BATTERY_SOC_ENTITY_ID: "sensor.ogs_battery_soc",
        CONF_BATTERY_POWER_ENTITY_ID: BATTERY,
        # A large pack, so the inverter's rating binds and not the battery.
        CONF_BATTERY_MAX_DISCHARGE_POWER: 20000,
        CONF_BATTERY_MAX_CHARGE_POWER: 5000,
    }
    if not solar:
        del options[CONF_SOLAR_PRODUCTION_ENTITY_ID]
    if not battery_power:
        del options[CONF_BATTERY_POWER_ENTITY_ID]
    return MockConfigEntry(
        domain=DOMAIN, version=2, minor_version=4, title=f"Off-grid Hub {slug}",
        data={CONF_NAME: f"Off-grid Hub {slug}",
              CONF_ENTITY_ID: f"ogs_hub_{slug}",
              ENTRY_TYPE: ENTRY_TYPE_HUB},
        options=options,
    )


def _evse(hub):
    """A 1-phase 6→32 A Standard EVSE: the inverter binds, not the car."""
    return MockConfigEntry(
        domain=DOMAIN, version=2, minor_version=4, title="Off-grid EVSE",
        data={CONF_NAME: "Off-grid EVSE",
              CONF_ENTITY_ID: "ogs_evse",
              ENTRY_TYPE: ENTRY_TYPE_LOAD,
              CONF_CHARGER_ID: "ogs_evse",
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


async def _run(hass, slug, topology, car_ramp_a_s, cycles=200):
    """Close the loop: engine permit → permit pipeline → car → battery → engine.

    The connector reads Available for ``START`` cycles, so every input EMA is
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

    hub = _hub(slug, topology)
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
        "sensor.ogs_battery_soc", "80",
        {"device_class": "battery", "unit_of_measurement": "%"})
    attrs, state = _watts(SOLAR_W)
    hass.states.async_set(SOLAR, state, attrs)
    # apply_smoothing keeps its state on the load entity and touches only these.
    permit_state = SimpleNamespace(
        _attr_name="ogs_evse", _ema_current=None, _schmitt_current=None,
        _schmitt_state="rising", _rate_limited_current=0.0,
    )

    trace = []
    draw = command = 0.0
    step = car_ramp_a_s * DT
    with freeze_time("2026-09-24 12:00:00+00:00") as frozen:
        for i in range(cycles):
            frozen.tick(DT)
            if i == START:
                hass.states.async_set(STATUS, "Charging")
            if i >= START:
                draw += max(-step, min(step, command - draw))
            supply = HOUSE_A + draw
            # Off-grid the battery balances the bus: + discharging, − charging.
            attrs, state = _watts(supply * V - SOLAR_W)
            hass.states.async_set(BATTERY, state, attrs)
            hass.states.async_set(
                CAR, f"{draw:.3f}",
                {"device_class": "current", "unit_of_measurement": "A"})
            result = run_hub_calculation(hass, hub)
            # The load processor's own rounding (entities/load.py).
            permit = round(result["load_available"][evse.entry_id], 1)
            command = apply_smoothing(permit_state, permit, False, hub)
            trace.append({"i": i, "draw": draw, "permit": permit,
                          "command": command, "supply": supply})
    return trace


def _over_w(trace, key, limit_a):
    return max(0.0, max(row[key] - limit_a for row in trace)) * V


@pytest.mark.parametrize("topology", [WIRING_TOPOLOGY_SERIES, WIRING_TOPOLOGY_PARALLEL])
@pytest.mark.parametrize("car_ramp_a_s", [1.0, 100.0], ids=["car-1A/s", "step"])
async def test_a_charger_gets_the_inverter_rating_less_the_house(
    hass, car_ramp_a_s, topology
):
    """The charger reaches the inverter's rating less the house, and no further.

    Measured against this rig (6 kW inverter, 1 kW house, allowance 21.74 A),
    the charger's settled draw, either topology, either car:

    ======================================  ==========
    managed draw in the off-grid total      car settles
    ======================================  ==========
    not taken off (before 2026-09-24)       10.9 A
    the smoothed draw, once a cycle         21.7 A
    ======================================  ==========

    10.9 A is the fixed point of the self-counting: permit = allowance − car,
    so the car meets its own draw halfway. The budget both ways is one step
    of the permit's own 0.1 A rounding.
    """
    trace = await _run(hass, f"{topology}{car_ramp_a_s:g}", topology, car_ramp_a_s)

    settled = trace[-1]["draw"]
    shortfall_w = (ALLOWANCE_A - settled) * V
    assert settled >= ALLOWANCE_A - 0.1, (
        f"car settled at {settled:.1f} A against the inverter's "
        f"{ALLOWANCE_A:.1f} A allowance - {shortfall_w:.0f} W short: its own "
        f"draw was counted as house load"
    )
    # And not by going past the inverter: the output never exceeds its rating.
    supply_over = _over_w(trace, "supply", RATING_A)
    assert supply_over <= 0.1 * V, (
        f"inverter output {supply_over:.0f} W over its {RATING_W:.0f} W rating"
    )


@pytest.mark.parametrize(
    "battery_power",
    ["unconfigured", "unavailable"],
)
@pytest.mark.parametrize("solar", [True, False], ids=["solar-sensor", "no-solar-sensor"])
async def test_solar_remaining_is_unknown_with_nothing_to_measure_the_house(
    hass, solar, battery_power
):
    """Off-grid with no inverter output sensors, solar + battery power is the
    site's one measure of what it draws. With the battery's power not read -
    no sensor, or one unreadable from the start - nothing measures the house
    (no household figure is built, and the engine hands the loads nothing on
    the inverter's word), so nothing says how much of the sun is spare.

    Solar Remaining Power / Current, read through the real hub sensors after
    a car has been charging for a minute, 3 kW on the solar sensor:

    ============  ==============  ==============  ==================
    solar sensor  whole solar     sun share       after
    ============  ==============  ==============  ==================
    3000 W        3000 W, 13.0 A  0 W, 0.0 A      unknown, available
    none          0 W, 0.0 A      0 W, 0.0 A      unknown, available
    ============  ==============  ==============  ==================

    "Whole solar" is what the figure was until it was read from the solar
    pool's sun share, which off-grid is built from the battery's flow and so
    is empty without it - a 0 that measures nothing. Either way, with the
    power sensor unconfigured or unavailable from the start. What the engine
    controls with is unchanged: the car's permit and Site Remaining Power
    stay 0, as the inverter pool has nothing to offer with no household
    figure.
    """
    from freezegun import freeze_time
    from custom_components.dynamic_ocpp_evse.engine.hub_calculation import (
        run_hub_calculation,
    )
    from custom_components.dynamic_ocpp_evse.entities.hub import publish_hub_data
    from custom_components.dynamic_ocpp_evse.sensor import (
        DynamicOcppEvseHubDataSensor,
        HUB_SENSOR_DEFINITIONS,
    )

    slug = f"unmeasured_{solar}_{battery_power}"
    hub = _hub(slug, WIRING_TOPOLOGY_SERIES, solar=solar,
               battery_power=battery_power != "unconfigured")
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
    hass.states.async_set(STATUS, "Charging")
    hass.states.async_set(
        CAR, "0.0", {"device_class": "current", "unit_of_measurement": "A"})
    hass.states.async_set(
        "sensor.ogs_battery_soc", "80",
        {"device_class": "battery", "unit_of_measurement": "%"})
    hass.states.async_set(BATTERY, "unavailable")
    attrs, state = _watts(3000.0)
    hass.states.async_set(SOLAR, state, attrs)

    with freeze_time("2026-09-24 12:00:00+00:00") as frozen:
        for _ in range(30):
            frozen.tick(DT)
            result = run_hub_calculation(hass, hub)
    publish_hub_data(hass, hub.entry_id, result)
    sensors = {
        d["hub_data_key"]: DynamicOcppEvseHubDataSensor(hass, hub, "Hub", slug, d)
        for d in HUB_SENSOR_DEFINITIONS
        if d["hub_data_key"] in ("available_solar_power", "available_solar_current")
    }
    published = {}
    for key, sensor in sensors.items():
        await sensor.async_update()
        published[key] = (sensor.native_value, sensor.available)

    assert published == {
        "available_solar_power": (None, True),
        "available_solar_current": (None, True),
    }, f"published (value, available): {published}, with nothing measuring the house"
    # Control untouched: nothing is offered on the inverter's word.
    assert result["total_site_available_power"] == 0
    assert result["load_available"][evse.entry_id] == 0
