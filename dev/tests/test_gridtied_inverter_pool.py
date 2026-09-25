"""Grid-tied, under a binding import allowance: the inverter's output counted once.

Machine-authored tests - not yet human-reviewed.

Reported with 8d3553e (the off-grid battery headroom fix), 2026-09-24. What
our managed loads may be offered on ANY site is the supply it can reach less
the household, within each limit:

    import allowance + solar + usable battery discharge - household

The grid pool reached its half from the meter with our loads taken off it
(allowance - that import). The inverter pool was built from GROSS production,
solar + the discharge rating less the discharge in flight, which only lands on
the identity when "solar" is the export-derived figure (no output sensor, no
solar sensor). Anywhere else:

  * a series hybrid read through its AC output sensor, a car running on the
    battery: the discharge in flight carries the car, so its own draw was
    booked as spent - at a 0 W allowance the car was permitted nothing and
    hunted, at 2 kW the pool shrank one-for-one with the draw;
  * the same hybrid with its battery at its rating: the grid carries the rest
    THROUGH the inverter, its output sensor reads that import too, and solar
    derived as output - battery was the import - offered by the grid pool and
    again by the inverter pool;
  * a dedicated solar sensor, or a PV inverter's own output sensor: the full
    production was offered although the house already takes part of it - a
    5 kW pool where 4 kW is right, 1 kW over the allowance.

Everything here runs the REAL site cycle - engine, the load processor with its
smoothing, command-interval gate and charge pause, and the OCPP command it
sends - against a small grid-tied world: a 1 kW house on one phase, a
self-consumption battery that covers whatever the site draws beyond the sun up
to its rating, the grid covering the rest, and a car that draws whatever limit
the charger last accepted. The clock is simulated (every module's
``time.monotonic``), so ten minutes of site cycles run in well under a second.
"""

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.dynamic_ocpp_evse import sensor as sensor_platform
from custom_components.dynamic_ocpp_evse.const import (
    CONF_BATTERY_MAX_CHARGE_POWER,
    CONF_BATTERY_MAX_DISCHARGE_POWER,
    CONF_BATTERY_POWER_ENTITY_ID,
    CONF_BATTERY_SOC_ENTITY_ID,
    CONF_CHARGE_PAUSE_DURATION,
    CONF_CHARGE_RATE_UNIT,
    CONF_CHARGER_ID,
    CONF_CHARGER_L1_PHASE,
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
    CONF_OCPP_DEVICE_ID,
    CONF_PHASE_A_CURRENT_ENTITY_ID,
    CONF_PHASE_VOLTAGE,
    CONF_PHASES,
    CONF_PROFILE_VALIDITY_MODE,
    CONF_SOLAR_PRODUCTION_ENTITY_ID,
    CONF_UPDATE_FREQUENCY,
    CONF_WIRING_TOPOLOGY,
    DEAD_BAND,
    DEFAULT_SITE_UPDATE_FREQUENCY,
    DOMAIN,
    ENTRY_TYPE,
    ENTRY_TYPE_HUB,
    ENTRY_TYPE_LOAD,
    WIRING_TOPOLOGY_PARALLEL,
    WIRING_TOPOLOGY_SERIES,
)
from custom_components.dynamic_ocpp_evse.engine import (
    auto_detect,
    hub_calculation,
    hub_result,
    load_builders,
    readers,
)
from custom_components.dynamic_ocpp_evse.control import compliance, status
from custom_components.dynamic_ocpp_evse.entities import load as load_entity

V = 230.0
HOUSE_W = 1000.0
MIN_A = 6.0
CYCLE_S = float(DEFAULT_SITE_UPDATE_FREQUENCY)
# How far a settled charger may sit from its target: the permit's Schmitt
# trigger holds a command within DEAD_BAND of it.
DEADBAND_W = DEAD_BAND * V
# (how the site is read, solar W, battery discharge rating W or None, import
# allowance W[, Inverter Max Power W])
SITES = pytest.mark.parametrize(
    "site",
    [
        ("series-output", 0.0, 5000.0, 0.0),
        ("series-output", 0.0, 5000.0, 2000.0),
        ("series-output", 0.0, 2000.0, 2000.0),
        ("solar-sensor", 5000.0, None, 0.0),
        ("parallel-output", 5000.0, None, 0.0),
    ],
    ids=[
        "series-output-battery-0w",
        "series-output-battery-2kw",
        "series-output-battery-at-rating-2kw",
        "solar-sensor-0w",
        "parallel-output-0w",
    ],
    indirect=True,
)

_CLOCKED = (
    load_builders, hub_calculation, readers, hub_result, auto_detect,
    load_entity, status, compliance,
)


class _Clock:
    """``time`` as the modules above see it: monotonic() is ours to advance,
    everything else is the real module."""

    def __init__(self, start):
        self.now = start

    def monotonic(self):
        return self.now

    def __getattr__(self, name):
        return getattr(time, name)


class World:
    """The grid-tied site, and the car behind the charger."""

    def __init__(self, hass, site):
        self.hass = hass
        self.site = site
        self.limit = None          # the last limit the charger accepted (A)
        self.battery_read = True   # False: its power sensor is unavailable
        self.soc = 80.0
        # What the battery's own logic does that Load Juggler cannot see: the
        # most it will discharge (its BMS, its own reserve), the import its
        # control aims for (a grid setpoint), and a flow it holds whatever the
        # site draws (a forced charge, a slow control loop between its steps)
        # - None follows the site.
        self.battery_cap_w = site.discharge_w
        self.battery_setpoint_w = 0.0
        self.battery_held_w = None

    @property
    def draw(self):
        if self.limit is None:
            return 0.0
        return self.limit if self.limit >= MIN_A else 0.0

    @property
    def demand_w(self):
        return HOUSE_W + self.draw * V

    @property
    def battery_w(self):
        """Self-consumption: discharges into any deficit up to its rating,
        charges from any surplus up to 5 kW. Positive = discharging. A hybrid
        that saturates puts out the sun plus the battery and never more than
        its rating, so the battery adds no more than the rating leaves beside
        the sun, and the grid carries the rest."""
        if self.site.discharge_w is None:
            return 0.0
        if self.battery_held_w is not None:
            return self.battery_held_w
        deficit = self.demand_w - self.site.solar_w - self.battery_setpoint_w
        cap = self.battery_cap_w
        if self.site.saturates:
            cap = min(cap, self.site.rating_w - self.site.solar_w)
        return max(-5000.0, min(deficit, cap))

    @property
    def import_w(self):
        return self.demand_w - self.site.solar_w - self.battery_w

    def charger_status(self):
        if self.limit is None:
            return "Preparing"
        return "Charging" if self.limit >= MIN_A else "SuspendedEVSE"

    def publish(self):
        set_state = self.hass.states.async_set
        current = {"device_class": "current", "unit_of_measurement": "A"}
        power = {"device_class": "power", "unit_of_measurement": "W"}
        set_state("sensor.grid_a", str(round(self.import_w / V, 3)), current)
        if self.site.sensors == "series-output":
            # A series hybrid's AC output feeds everything behind it.
            set_state("sensor.inverter_out_a", str(round(self.demand_w / V, 3)), current)
        elif self.site.sensors == "parallel-output":
            # A PV inverter beside the grid puts out what the array makes.
            set_state("sensor.inverter_out_a", str(round(self.site.solar_w / V, 3)), current)
        elif self.site.sensors == "solar-sensor":
            set_state("sensor.solar_production", str(self.site.solar_w), power)
        # "meter-only": no output and no solar sensor - solar from the meter.
        if self.site.discharge_w is not None:
            set_state(
                "sensor.battery_power",
                str(round(self.battery_w, 1)) if self.battery_read else "unavailable",
                power,
            )
            set_state("sensor.battery_soc", str(self.soc),
                      {"device_class": "battery", "unit_of_measurement": "%"})
        set_state("sensor.evse_status_connector", self.charger_status())
        set_state("switch.evse_charge_control", "on")
        # A healthy meter: the car's real draw.
        set_state("sensor.evse_current_import", str(round(self.draw, 2)), current)

    async def accept(self, domain, service, data=None, *args, **kwargs):
        """The mocked service registry: the charger accepts every profile."""
        if domain == "ocpp" and service == "set_charge_rate":
            period = data["custom_profile"]["chargingSchedule"]["chargingSchedulePeriod"]
            self.limit = float(period[0]["limit"])


@pytest.fixture
def site(hass, request):
    sensors, solar_w, discharge_w, allowance_w, *rating = request.param
    rating_w = rating[0] if rating else None
    saturates = len(rating) > 1 and rating[1]
    if sensors == "solar-sensor":
        reading = {CONF_SOLAR_PRODUCTION_ENTITY_ID: "sensor.solar_production"}
    elif sensors == "meter-only":
        reading = {}
    else:
        reading = {
            CONF_INVERTER_OUTPUT_PHASE_A_ENTITY_ID: "sensor.inverter_out_a",
            CONF_WIRING_TOPOLOGY: (
                WIRING_TOPOLOGY_SERIES
                if sensors == "series-output"
                else WIRING_TOPOLOGY_PARALLEL
            ),
        }
    battery = (
        {
            CONF_BATTERY_SOC_ENTITY_ID: "sensor.battery_soc",
            CONF_BATTERY_POWER_ENTITY_ID: "sensor.battery_power",
            CONF_BATTERY_MAX_DISCHARGE_POWER: discharge_w,
            CONF_BATTERY_MAX_CHARGE_POWER: 5000,
        }
        if discharge_w is not None
        else {}
    )
    hub_entry = MockConfigEntry(
        domain=DOMAIN, version=2, minor_version=2, title="Hub",
        data={CONF_NAME: "Hub", CONF_ENTITY_ID: "hub", ENTRY_TYPE: ENTRY_TYPE_HUB},
        options={
            CONF_PHASE_VOLTAGE: V,
            CONF_MAIN_BREAKER_RATING: 40,
            CONF_PHASE_A_CURRENT_ENTITY_ID: "sensor.grid_a",
            **reading,
            **battery,
            **({CONF_INVERTER_MAX_POWER: rating_w} if rating_w else {}),
        },
    )
    evse = MockConfigEntry(
        domain=DOMAIN, version=2, minor_version=2, title="evse",
        data={
            CONF_ENTITY_ID: "evse",
            CONF_NAME: "evse",
            ENTRY_TYPE: ENTRY_TYPE_LOAD,
            CONF_CHARGER_ID: "evse",
            CONF_OCPP_DEVICE_ID: "evse_cp",
            CONF_EVSE_CURRENT_IMPORT_ENTITY_ID: "sensor.evse_current_import",
            CONF_HUB_ENTRY_ID: hub_entry.entry_id,
        },
        options={
            CONF_PHASES: 1,
            CONF_CHARGER_L1_PHASE: "A",
            CONF_LOAD_PRIORITY: 1,
            CONF_EVSE_MINIMUM_CHARGE_CURRENT: MIN_A,
            CONF_EVSE_MAXIMUM_CHARGE_CURRENT: 32,
            CONF_CHARGE_RATE_UNIT: "A",
            CONF_PROFILE_VALIDITY_MODE: "relative",
            CONF_UPDATE_FREQUENCY: 15,
            CONF_CHARGE_PAUSE_DURATION: 3,
        },
    )
    hass.data[DOMAIN] = {
        "hubs": {
            hub_entry.entry_id: {
                "entry": hub_entry,
                "loads": [evse.entry_id],
                "distribution_mode": "Priority",
                "allow_grid_charging": True,
                "power_buffer": 0,
                "battery_soc_min": 20,
                "battery_soc_target": 50,
                "max_import_power": allowance_w,
            }
        },
        "loads": {
            evse.entry_id: {
                "entry": evse,
                "hub_entry_id": hub_entry.entry_id,
                "dynamic_control": True,
            }
        },
    }
    return SimpleNamespace(
        hub=hub_entry, evse=evse, sensors=sensors, solar_w=solar_w,
        discharge_w=discharge_w, allowance_w=allowance_w, rating_w=rating_w,
        saturates=saturates,
    )


def _headroom_w(site):
    """The identity: allowance + solar + discharge rating - house."""
    return site.allowance_w + site.solar_w + (site.discharge_w or 0.0) - HOUSE_W


async def _session(hass, site, minutes, published=None, on_cycle=None):
    """``minutes`` of real site cycles against the world, one CYCLE_S apart.
    Returns [(seconds, accepted limit, import W, battery W)] per cycle, and
    appends each cycle's published hub data to ``published`` when given.
    ``on_cycle(world, seconds)`` runs before each cycle's readings go out."""
    world = World(hass, site)
    clock = _Clock(10_000.0)
    hass.data[DOMAIN].setdefault("load_processors", {}).setdefault(
        site.hub.entry_id, {}
    )[site.evse.entry_id] = sensor_platform.LoadJugglerDeviceSensor(
        hass, site.evse, site.hub, "evse", "evse"
    )
    patches = [patch.object(module, "time", clock) for module in _CLOCKED]
    patches.append(patch(
        "homeassistant.core.ServiceRegistry.async_call",
        new_callable=AsyncMock, side_effect=world.accept,
    ))
    for p in patches:
        p.start()
    log = []
    try:
        for _ in range(int(minutes * 60 / CYCLE_S)):
            if on_cycle is not None:
                on_cycle(world, clock.now - 10_000.0)
            world.publish()
            await sensor_platform.async_run_hub_cycle(hass, site.hub)
            log.append((clock.now, world.limit, world.import_w, world.battery_w))
            if published is not None:
                published.append(dict(hass.data[DOMAIN]["hub_data"][site.hub.entry_id]))
            clock.now += CYCLE_S
    finally:
        for p in reversed(patches):
            p.stop()
    return log


def _stops(log, since):
    """How many times the charger was cut from running to 0 A."""
    limits = [limit for t, limit, *_ in log if t >= since and limit is not None]
    return sum(1 for a, b in zip(limits, limits[1:]) if a >= MIN_A and b < MIN_A)


@SITES
async def test_the_car_settles_on_the_supply_less_the_house(hass, site):
    """The car rises to allowance + solar + discharge rating - house, and stays
    there: not one stop after the first minute.

    Before the fix, measured: on the series hybrid's output sensor at a 0 W
    allowance the car was cut after every start (its own draw came off the
    discharge headroom it was running on); at 2 kW it settled 3.0 kW short,
    at 13.1 A, where the pool left after its own draw met its draw. On the solar
    sensor and on the PV inverter's output sensor it settled 1.0 kW over, at
    21.7 A, on the 1 kW of sun the house was already using."""
    log = await _session(hass, site, minutes=10)
    start = log[0][0]
    target_w = _headroom_w(site)

    stops = _stops(log, since=start + 60)
    assert stops == 0, f"the car hunted: {stops} stops after the first minute"

    tail_w = [limit * V for t, limit, *_ in log if t > start + 180]
    low_w, high_w = min(tail_w), max(tail_w)
    assert low_w >= target_w - DEADBAND_W - V, (
        f"settled at {low_w:.0f} W, {target_w - low_w:.0f} W short of the "
        f"{target_w:.0f} W the site can give it"
    )
    assert high_w <= target_w + DEADBAND_W, (
        f"settled at {high_w:.0f} W, {high_w - target_w:.0f} W over the "
        f"{target_w:.0f} W the site can give it"
    )


@SITES
async def test_the_site_stays_inside_its_import_allowance(hass, site):
    """Across the whole session, the start included, the grid never carries
    more than the allowance plus the permit's deadband. (The world's battery
    stops at its rating, so a pack asked for more shows up here, as import.)

    Before the fix, measured: on the solar sensor and on the PV inverter's
    output sensor the site imported up to 992 W against a 0 W allowance, for as
    long as the car ran."""
    log = await _session(hass, site, minutes=10)
    worst_import = max(imp for *_, imp, _ in log)
    assert worst_import <= site.allowance_w + DEADBAND_W, (
        f"imported {worst_import:.0f} W against a {site.allowance_w:.0f} W "
        f"allowance - {worst_import - site.allowance_w:.0f} W over"
    )


@SITES
async def test_site_remaining_power_is_the_pool_the_car_is_offered(hass, site):
    """Every cycle, the published Site Remaining Power is the physical pool the
    distribution sized the car from (the Overview's pool detail), and its grid
    and inverter figures are that pool's two halves.

    Before the fix, measured (worst cycle): the series hybrid published 2 W at
    a 0 W allowance where the car was offered 4000 W, and 4003 W against
    6003 W at 2 kW - the discharge carrying the car was booked as spent; with
    its battery at its rating, 3990 W against 2999 W; the PV inverter's output
    sensor 5000 W against 4000 W, the sun the house uses counted as spare. The
    solar sensor agreed, and none published the pool's halves."""
    published = []
    await _session(hass, site, minutes=3, published=published)
    worst = max(
        (
            abs(p["total_site_available_power"] - p["pool_detail"]["physical"]["start"]["ABC"] * V),
            i,
            p["total_site_available_power"],
            p["pool_detail"]["physical"]["start"]["ABC"] * V,
        )
        for i, p in enumerate(published)
    )
    assert worst[0] <= 2.0, (
        f"cycle {worst[1]}: Site Remaining Power {worst[2]:.0f} W, the pool the "
        f"car was offered {worst[3]:.0f} W ({worst[2] - worst[3]:+.0f} W)"
    )
    last = published[-1]
    pools = last["pool_detail"]
    assert abs(last["available_grid_power"] - pools["grid"]["start"]["ABC"] * V) <= 2.0
    assert abs(last["available_inverter_current"] - pools["inverter"]["start"]["ABC"]) <= 0.051


UNREAD_FROM_S, READ_AGAIN_S = 240.0, 480.0


def _battery_reading_drops_out(world, seconds):
    world.battery_read = not UNREAD_FROM_S <= seconds < READ_AGAIN_S


@pytest.mark.parametrize(
    "site",
    [("solar-sensor", 0.0, 5000.0, 0.0), ("solar-sensor", 0.0, 5000.0, 2000.0)],
    ids=["solar-sensor-battery-0w", "solar-sensor-battery-2kw"],
    indirect=True,
)
async def test_an_unread_battery_power_offers_nothing_on_the_batterys_word(hass, site):
    """Night, a solar production sensor, a car the battery carries: the
    battery's power sensor goes unavailable for four minutes and comes back.

    Held for INPUT_STALE_TIMEOUT, the reading then counts as unread, and its
    flow is unknown in both directions - so no headroom is offered on it, only
    what the meter measures. The car the battery is carrying reads as export
    once its draw is handed back, so it is neither cut nor pushed; when the
    reading returns, nothing has moved.

    Before the fix, measured: with the flow taken as 0 the whole discharge
    rating came back on top of that export, the car rose to 32 A, the battery
    stopped at its 5 kW and the grid carried 3360 W past a 0 W allowance
    (1360 W past 2 kW) until the reading returned."""
    log = await _session(
        hass, site, minutes=12, on_cycle=_battery_reading_drops_out
    )
    start = log[0][0]
    target_w = _headroom_w(site)
    worst = max(log, key=lambda row: row[2])
    assert worst[2] <= site.allowance_w + DEADBAND_W, (
        f"imported {worst[2]:.0f} W against a {site.allowance_w:.0f} W allowance "
        f"at {worst[0] - start:.0f} s - {worst[2] - site.allowance_w:.0f} W over "
        f"(car at {worst[1]:.1f} A)"
    )
    stops = _stops(log, since=start + 60)
    assert stops == 0, f"the car was cut {stops} times"
    for label, since, until in (
        ("unread", start + UNREAD_FROM_S + 120, start + READ_AGAIN_S),
        ("read again", start + READ_AGAIN_S + 120, start + 1e9),
    ):
        drawn_w = [limit * V for t, limit, *_ in log if since <= t < until]
        assert min(drawn_w) >= target_w - DEADBAND_W - V, (
            f"{label}: the car fell to {min(drawn_w):.0f} W of the "
            f"{target_w:.0f} W the site can give it"
        )


# (how the site is read, solar W, battery discharge rating W or None, import
# allowance W): 3 kW of sun and the 1 kW house by day, 2 kW of it spare; the
# battery carrying the house and the car at night, no sun spare at all.
SOLAR_REMAINING_SITES = pytest.mark.parametrize(
    "site",
    [
        ("parallel-output", 3000.0, None, 0.0),
        ("parallel-output", 3000.0, 5000.0, 0.0),
        ("series-output", 3000.0, 5000.0, 0.0),
        ("series-output", 0.0, 5000.0, 0.0),
        ("solar-sensor", 3000.0, None, 0.0),
        ("solar-sensor", 3000.0, 5000.0, 0.0),
        ("meter-only", 3000.0, 5000.0, 0.0),
        ("meter-only", 0.0, 5000.0, 0.0),
    ],
    ids=[
        "parallel-output-day",
        "parallel-output-battery-day",
        "series-output-battery-day",
        "series-output-battery-night",
        "solar-sensor-day",
        "solar-sensor-battery-day",
        "meter-only-battery-day",
        "meter-only-battery-night",
    ],
    indirect=True,
)


@SOLAR_REMAINING_SITES
async def test_solar_remaining_is_the_sun_the_house_leaves(hass, site):
    """Solar Remaining Power / Current, read through the real hub sensors once
    the car has settled, is the sun's share of the solar pool the Solar modes
    are offered: the export with our loads off, plus the battery's charge, less
    the discharge the export carries - the sun less the house, 2000 W by day
    and nothing at night, whichever way the site is read.

    Before the fix it was the whole solar figure wherever no production sensor
    feeds the household total, measured:

    ==============================  ==========  ==========
    Solar Remaining Power           before      after
    ==============================  ==========  ==========
    parallel output, day            2999 W      2001 W
    parallel output + battery, day  2999 W      2001 W
    series output + battery, day    3000 W      2001 W
    meter only + battery, day       6992 W      2001 W
    meter only + battery, night     4000 W      0 W
    series output + battery, night  2 W         0 W
    solar sensor (+ battery), day   2001 W      2001 W
    ==============================  ==========  ==========

    (Solar Remaining Current 13.0 A before on the output sensors, 30.4 A and
    17.4 A on the meter-only site; 8.7 A and 0 A after.)

    On the meter-only site the "solar" is the export plus the charge, so the
    battery's discharge carrying the car - handed back as export - was
    published as spare sun.
    """
    from custom_components.dynamic_ocpp_evse.sensor import (
        DynamicOcppEvseHubDataSensor,
        HUB_SENSOR_DEFINITIONS,
    )

    await _session(hass, site, minutes=3)
    sensors = {
        d["hub_data_key"]: DynamicOcppEvseHubDataSensor(hass, site.hub, "Hub", "hub", d)
        for d in HUB_SENSOR_DEFINITIONS
        if d["hub_data_key"] in ("available_solar_power", "available_solar_current")
    }
    for sensor in sensors.values():
        await sensor.async_update()

    expected_w = max(0.0, site.solar_w - HOUSE_W)
    power = sensors["available_solar_power"].native_value
    current = sensors["available_solar_current"].native_value
    assert power == pytest.approx(expected_w, abs=5.0), (
        f"Solar Remaining Power {power:.0f} W with {site.solar_w:.0f} W of sun "
        f"and a {HOUSE_W:.0f} W house"
    )
    assert current == pytest.approx(expected_w / V, abs=0.1)


# A meter-only site - no solar sensor, no inverter output sensor - its solar
# worked out from the meter; (site as above, whether the battery's power
# sensor reads). The unread night runs at a 2 kW allowance, so the car starts
# and the battery covers it.
METER_ONLY_SITES = pytest.mark.parametrize(
    "site,battery_read",
    [
        (("meter-only", 3000.0, 5000.0, 0.0), True),
        (("meter-only", 0.0, 5000.0, 0.0), True),
        (("meter-only", 3000.0, None, 0.0), True),
        (("meter-only", 3000.0, 5000.0, 0.0), False),
        (("meter-only", 0.0, 5000.0, 2000.0), False),
    ],
    ids=[
        "battery-day",
        "battery-night",
        "no-battery-day",
        "battery-unread-day",
        "battery-unread-night-2kw",
    ],
    indirect=["site"],
)


@METER_ONLY_SITES
async def test_meter_only_solar_power_is_the_sun_not_the_battery(
    hass, site, battery_read
):
    """Current Solar Power and Household Power, read through the real hub
    sensors once the car has settled.

    From the meter alone the solar is the export with our loads handed back
    plus the battery's charge - and that export carries the battery's
    discharge too: the car the battery covers comes back as export. With the
    battery's power read the discharge is known and comes back off (+ is
    discharging), which leaves the sun the house does not use itself - the
    rest of it reaches no meter - so 2000 W by day, nothing at night; the
    household identity built on it is the house the battery and the grid
    carry. With the power unread nothing takes the discharge off, and both
    read unknown. Measured:

    ========================  ===========================  ====================
    meter only                before: solar, household     after
    ========================  ===========================  ====================
    battery, day              6992 W, 4992 W               2000 W, 0 W
    battery, night            4000 W, 5000 W               0 W, 1000 W
    no battery, day           2001 W, 1 W                  unchanged
    battery unread, day       0 W, 0 W (car 0 A)           unknown, unknown
    battery unread, night     4000 W, 0 W (2 kW, 26.1 A)   unknown, unknown
    ========================  ===========================  ====================

    The forecast observers take no part: they learn from fleet.solar_total,
    which is None wherever no member knows its production.
    """
    from custom_components.dynamic_ocpp_evse.sensor import (
        DynamicOcppEvseHubDataSensor,
        HUB_SENSOR_DEFINITIONS,
    )

    def _read(world, seconds):
        world.battery_read = battery_read

    await _session(hass, site, minutes=3, on_cycle=_read)
    published = {}
    for d in HUB_SENSOR_DEFINITIONS:
        if d["hub_data_key"] in ("solar_power", "household_power"):
            sensor = DynamicOcppEvseHubDataSensor(hass, site.hub, "Hub", "hub", d)
            await sensor.async_update()
            published[d["hub_data_key"]] = (sensor.native_value, sensor.available)

    if not battery_read:
        assert published == {
            "solar_power": (None, True), "household_power": (None, True),
        }, f"published (value, available): {published}, the battery unread"
        return
    solar, available = published["solar_power"]
    assert available and solar == pytest.approx(
        max(0.0, site.solar_w - HOUSE_W), abs=5.0
    ), f"Current Solar Power {solar} W with {site.solar_w:.0f} W of sun"
    house, available = published["household_power"]
    assert available and house == pytest.approx(
        max(0.0, HOUSE_W - site.solar_w), abs=5.0
    ), f"Household Power {house} W with a {HOUSE_W:.0f} W house"


@pytest.mark.parametrize(
    "site",
    [
        ("solar-sensor", 0.0, 5000.0, 2000.0),
        ("solar-sensor", 3000.0, 5000.0, 0.0),
        ("meter-only", 0.0, 5000.0, 2000.0),
    ],
    ids=["solar-sensor-night-2kw", "solar-sensor-day", "meter-only-night-2kw"],
    indirect=True,
)
async def test_solar_remaining_is_unknown_with_the_battery_unread(hass, site):
    """Grid-tied, the battery's power sensor unavailable from the start.

    Solar Remaining Power / Current is the sun's share of the solar pool: the
    export with our loads handed back, plus the battery's charge, less the
    discharge the export carries. With the battery's power unread neither
    battery term is known, so the "sun" is the bare export - at night, with
    the 2 kW allowance starting the car and the battery covering it, every
    watt of it the battery's. Nothing tells the sun from the battery there,
    and both sensors read unknown (and stay available), as Current Solar
    Power does where nothing splits them. Measured, through the real hub
    sensors once the car has settled:

    ======================  ==============  ==================
    site                    before          after
    ======================  ==============  ==================
    solar sensor, night     4000 W, 17.4 A  unknown, available
    solar sensor, day       0 W, 0.0 A      unknown, available
    meter only, night       4000 W, 17.4 A  unknown, available
    ======================  ==============  ==================

    (By day the battery takes the sun's 2 kW the house leaves; unseen, the
    meter shows no export and the figure read 0 W.) What the car is offered
    is unchanged.
    """
    from custom_components.dynamic_ocpp_evse.sensor import (
        DynamicOcppEvseHubDataSensor,
        HUB_SENSOR_DEFINITIONS,
    )

    def _unread(world, seconds):
        world.battery_read = False

    await _session(hass, site, minutes=3, on_cycle=_unread)
    published = {}
    for d in HUB_SENSOR_DEFINITIONS:
        if d["hub_data_key"] in ("available_solar_power", "available_solar_current"):
            sensor = DynamicOcppEvseHubDataSensor(hass, site.hub, "Hub", "hub", d)
            await sensor.async_update()
            published[d["hub_data_key"]] = (sensor.native_value, sensor.available)

    assert published == {
        "available_solar_power": (None, True),
        "available_solar_current": (None, True),
    }, f"published (value, available): {published}, the battery unread"


# A meter-only site with an Inverter Max Power (site as above, the rating
# last): the battery carries the house and the car at night; by day the sun
# covers the house and the battery the rest of the car.
@pytest.mark.parametrize(
    "site",
    [
        ("meter-only", 0.0, 5000.0, 0.0, 6000.0),
        ("meter-only", 3000.0, 5000.0, 0.0, 8000.0),
        ("meter-only", 0.0, 5000.0, 0.0, 4000.0),
        ("meter-only", 0.0, 5000.0, 2000.0, 6000.0),
    ],
    ids=["night-6kw", "day-8kw", "night-4kw-rating-binds", "night-6kw-2kw-allowance"],
    indirect=True,
)
async def test_meter_only_rating_cap_leaves_the_car_its_own_supply(hass, site):
    """The rating caps the inverters at the rating less the house they
    already serve - and the car's own supply is not house.

    From the meter alone the engine's solar is the export with our loads
    handed back plus the battery's charge, and that export carries the
    discharge feeding the car; read as solar + battery flow - export, the
    house on the inverters came out as the whole discharge, the car's share
    included. Measured before the fix (0 W allowance unless stated):

    ===========================  ================================  =========
    site                         before                            after
    ===========================  ================================  =========
    night, 6 kW rating           17.4 A, cut to 0 A at 16, 224     17.4 A
                                 and 432 s, restarted each time
    day, 8 kW rating             21.6 A where 30.4 A fits          30.4 A
    night, 4 kW rating           13.0 A, cut the same way          13.0 A
    night, 6 kW, 2 kW allowance  15.3 A where 26.1 A fits          26.1 A
    ===========================  ================================  =========

    The car settles on the supply less the house, within the rating less the
    house, never cut after the first minute; the inverters (the sun plus the
    battery - this world's battery has no rating of its own beyond its
    discharge) never put out more than their rating.
    """
    log = await _session(hass, site, minutes=10)
    start = log[0][0]
    target_w = min(
        _headroom_w(site), site.allowance_w + site.rating_w - HOUSE_W
    )

    stops = _stops(log, since=start + 60)
    ran = [(t - start, limit) for t, limit, *_ in log if limit is not None]
    cuts = [t for (_, a), (t, b) in zip(ran, ran[1:]) if a >= MIN_A > b]
    assert stops == 0, (
        f"the car hunted: {stops} stops after the first minute, from "
        f"{max(limit for _, limit in ran):.1f} A to 0 A at {cuts} s"
    )

    tail_w = [limit * V for t, limit, *_ in log if t > start + 180]
    low_w, high_w = min(tail_w), max(tail_w)
    assert low_w >= target_w - DEADBAND_W - V, (
        f"settled at {low_w / V:.1f} A, {target_w - low_w:.0f} W short of the "
        f"{target_w / V:.1f} A the site and the rating can give it"
    )
    assert high_w <= target_w + DEADBAND_W, (
        f"settled at {high_w / V:.1f} A, {high_w - target_w:.0f} W over the "
        f"{target_w / V:.1f} A the site and the rating can give it"
    )

    worst_output_w = max(site.solar_w + battery for *_, battery in log)
    assert worst_output_w <= site.rating_w + DEADBAND_W, (
        f"the inverters put out {worst_output_w:.0f} W on a "
        f"{site.rating_w:.0f} W rating"
    )


# A HYBRID that saturates at its Inverter Max Power (site as above: the
# rating, then True): its AC output is the sun plus the battery and never more
# than the rating, and the grid carries whatever the site draws beyond it.
# 3 kW of sun and the 1 kW house on a 7 kW inverter with a 5 kW battery: the
# car can have the rating less the house, 6 kW (26.1 A) - not the
# 3 + 5 - 1 = 7 kW (30.4 A) the sun and the battery would give past it.
SATURATED_DAY = ("meter-only", 3000.0, 5000.0, 0.0, 7000.0, True)
# How far past the allowance a settled site may sit: the permit's deadband and
# the 0.1 A a command is rounded to - a charger cut from above holds up to
# that far over its target.
SETTLED_W = DEADBAND_W + 0.05 * V
# The longest the grid may carry the car past the allowance while a verdict is
# reached: two site cycles for the filtered import to show it, the
# confirmation, then up to two commands (the charger's 15 s interval, 16 s on
# 2 s cycles) to bring the car back down - 80 to 96 s measured at each offer
# of the rating. At the car's fast start into the limit the battery reading
# first has to settle within the deadband of the 6 kW it swung, 36 s on the
# input filter: 96 to 128 s measured.
OFFER_S = 2 * CYCLE_S + hub_calculation.SATURATION_CONFIRM_S + 2 * 16.0
START_S = OFFER_S + 36.0


def _overruns(log, allowance_w):
    """(start s, seconds) of each stretch the grid carried more than the
    allowance, past what a settled site may sit at."""
    start, runs, run = log[0][0], [], None
    for t, _limit, imp, _battery in log:
        if imp > allowance_w + SETTLED_W:
            run = (run[0], t) if run else (t, t)
        elif run:
            runs.append(run)
            run = None
    if run:
        runs.append(run)
    return [(a - start, b - a + CYCLE_S) for a, b in runs]


def _settled(log, runs):
    """The car's limit (W) on every cycle outside those stretches."""
    start = log[0][0]
    over = {round(at + k * CYCLE_S) for at, secs in runs for k in range(int(secs / CYCLE_S))}
    return [limit * V for t, limit, *_ in log if round(t - start) not in over]


def _watching(verdicts, then=None):
    """An ``on_cycle`` that records, before each cycle, the battery ceiling the
    saturation latch held on the cycle before (None: no verdict) - then runs
    ``then``."""
    def hook(world, seconds):
        runtime = world.hass.data[DOMAIN]["hubs"][world.site.hub.entry_id]
        verdicts.append(runtime.get("_saturation", {}).get("ceiling"))
        if then is not None:
            then(world, seconds)
    return hook


def _holding(**battery):
    """An ``on_cycle`` that sets the world's battery behaviour every cycle."""
    def hook(world, seconds):
        for name, value in battery.items():
            setattr(world, name, value)
    return hook


def _answering_every(period_s):
    """A battery whose control loop moves to where the site needs it only
    every ``period_s`` seconds, holding its flow in between."""
    def hook(world, seconds):
        if seconds % period_s == 0:
            world.battery_held_w = None
            world.battery_held_w = world.battery_w
    return hook


@pytest.mark.parametrize("site", [SATURATED_DAY], ids=["meter-only-day"], indirect=True)
async def test_a_saturated_meter_only_hybrid_is_held_to_its_rating(hass, site):
    """No solar and no output sensor, 3 kW of sun, the 1 kW house, a 7 kW
    hybrid, a 5 kW battery, a 0 W allowance, an hour of real site cycles.

    From the meter the house the sun serves is invisible, so the rating cap
    took off none of it and the pool offered the battery's 1 kW of rating the
    inverter cannot pass. Measured before: the car at 30.4 A for the whole
    hour, the battery stuck at 4 kW, 1 kW below its rating, the grid carrying
    992 W past the allowance throughout.

    After: the grid carrying the car's growth while the battery stays flat
    below its rating is the verdict (hub_calculation._apply_saturation_latch).
    The car comes down to 26.4 A (26.1 A fits; a charger cut from above holds
    within the permit's deadband) and stays there; the grid carries more than
    the allowance only while a verdict is being reached - 128 s at the start,
    80 s at each offer of the rating, 15 min after the verdict and then 30 -
    288 s of the hour where it was all 3600. While the battery is held,
    Battery Remaining Power reads 0 W, as the pool offers (1000 W before).
    """
    verdicts, published = [], []
    log = await _session(
        hass, site, minutes=60, published=published, on_cycle=_watching(verdicts)
    )
    start = log[0][0]
    target_w = site.allowance_w + site.rating_w - HOUSE_W

    stops = _stops(log, since=start + 60)
    assert stops == 0, f"the car was cut {stops} times"

    runs = _overruns(log, site.allowance_w)
    assert len(runs) == 3, f"stretches past the allowance: {runs} (s, seconds)"
    assert runs[0][1] <= START_S and all(secs <= OFFER_S for _, secs in runs[1:]), (
        f"past the allowance for {runs} (s, seconds) - longer than a verdict "
        f"takes ({START_S:.0f} s at the start, {OFFER_S:.0f} s at an offer)"
    )

    # Outside those stretches the car sits at the rating less the house.
    settled = _settled(log, runs)
    assert min(settled) >= target_w - DEADBAND_W - V, (
        f"the car fell to {min(settled) / V:.1f} A of {target_w / V:.1f} A"
    )
    assert max(settled) <= target_w + SETTLED_W, (
        f"the car sat at {max(settled) / V:.1f} A where {target_w / V:.1f} A fits"
    )
    assert verdicts[-1] is not None
    assert published[-1]["available_battery_power"] == 0


@pytest.mark.parametrize(
    "site,battery,target_w",
    [
        # SOC below the hub's minimum, at night: its rating is 0, the battery
        # idles, the house imports 1 kW and the car gets the 2 kW the 3 kW
        # allowance leaves.
        (("meter-only", 0.0, 5000.0, 3000.0, 6000.0, True),
         _holding(soc=15.0, battery_held_w=0.0), 2000.0),
        # Allow Grid Charging, a 2 kW allowance: the hybrid saturates, but the
        # 1.36 kW the grid carries for the 32 A car is inside the grant.
        (("meter-only", 3000.0, 5000.0, 2000.0, 7000.0, True), None, 7360.0),
        # A battery that answers the site only every 30 s: the car starts at
        # 17.4 A on the battery's word and the grid carries it for up to 30 s.
        (("meter-only", 0.0, 5000.0, 0.0, 6000.0, True),
         _answering_every(30.0), 4000.0),
        # At night the house alone imports: 1 kW against an 800 W battery at
        # its rating; the car gets the 2 kW allowance less the 200 W.
        (("meter-only", 0.0, 800.0, 2000.0, 6000.0, True), None, 1800.0),
        # The battery's own control aims for a 150 W import (a grid setpoint),
        # past the 0 W allowance whenever the car runs: it answers every step.
        (("meter-only", 0.0, 5000.0, 0.0, 6000.0, True),
         _holding(battery_setpoint_w=150.0), 4000.0),
    ],
    ids=["soc-below-minimum-night", "grid-charging-within-allowance",
         "battery-answering-every-30s", "house-alone-imports-night",
         "battery-grid-setpoint"],
    indirect=["site"],
)
async def test_b_no_verdict_where_the_battery_answers(hass, site, battery, target_w):
    """Every site here shows some part of the sign - a battery below its
    rating, an import, a charger asking for more - and none of it is an
    inverter at its limit. Over twenty minutes the latch never holds the
    battery, the car is never cut after the first minute, and it settles on
    what the site gives it - 8.7, 32.0, 17.4, 7.8 and 17.7 A, measured the
    same before the change."""
    verdicts = []
    log = await _session(hass, site, minutes=20, on_cycle=_watching(verdicts, battery))
    start = log[0][0]

    held = [v for v in verdicts if v is not None]
    assert not held, f"the battery was held at {held[0]:.0f} W"
    assert _stops(log, since=start + 60) == 0
    tail_w = [limit * V for t, limit, *_ in log if t > start + 180]
    assert min(tail_w) >= target_w - DEADBAND_W - V, (
        f"settled at {min(tail_w) / V:.1f} A of {target_w / V:.1f} A"
    )
    assert max(tail_w) <= target_w + SETTLED_W, (
        f"settled at {max(tail_w) / V:.1f} A where {target_w / V:.1f} A fits"
    )


@pytest.mark.parametrize(
    "site,battery,target_w,stops",
    [
        # Its BMS holds the discharge at 2 kW: the inverter is not at its
        # rating, the battery is at its own limit, and 3 + 2 - 1 = 4 kW fits.
        (SATURATED_DAY, _holding(battery_cap_w=2000.0), 4000.0, 0),
        # Charging from the grid on its own schedule, 2 kW, at night under a
        # 5 kW allowance: the car gets the 2 kW the house and the charge leave.
        (("meter-only", 0.0, 5000.0, 5000.0, 7000.0, True),
         _holding(battery_held_w=-2000.0), 2000.0, 0),
        # Charging 1.5 kW from the sun toward its own target, yielding none of
        # it: 3 - 1 - 1.5 = 0.5 kW is left, under the car's 6 A minimum.
        (SATURATED_DAY, _holding(battery_held_w=-1500.0), 0.0, 3),
    ],
    ids=["bms-holds-discharge-2kw", "forced-grid-charge-night",
         "charge-priority-day"],
    indirect=["site"],
)
async def test_b_a_battery_its_own_logic_holds_is_kept_to_the_allowance(
    hass, site, battery, target_w, stops
):
    """A battery that will not give the car what its rating says - its BMS,
    a forced charge, its own charge priority - shows the same sign as an
    inverter at its limit, and on a meter-only site nothing tells them apart.
    Holding it at its flow is the truth here too: it gives no more.

    Measured before, for the hour: the car at 30.4, 32.0 and 30.4 A, the grid
    carrying 2992, 5360 and 6492 W past the allowance throughout. After: the
    car settles on what the site gives it - 17.6, 8.9 and 0 A - and the grid
    carries more than the allowance only while a verdict is reached, at the
    start and at each offer of the rating. Where what fits is under the car's
    minimum it stops, and each offer restarts it: 3 stops in the hour (the
    first session, then 16 and 47 min in), where it ran throughout past the
    allowance before.
    """
    log = await _session(hass, site, minutes=60, on_cycle=battery)
    start = log[0][0]

    runs = _overruns(log, site.allowance_w)
    assert len(runs) == 3, f"stretches past the allowance: {runs} (s, seconds)"
    assert runs[0][1] <= START_S and all(secs <= OFFER_S for _, secs in runs[1:]), (
        f"past the allowance for {runs} (s, seconds)"
    )
    assert _stops(log, since=start) == stops
    settled = _settled(log, runs)
    assert max(settled) <= target_w + SETTLED_W, (
        f"the car sat at {max(settled) / V:.1f} A where {target_w / V:.1f} A fits"
    )
    if target_w:
        assert min(settled) >= target_w - DEADBAND_W - V


@pytest.mark.parametrize(
    "site",
    [
        ("solar-sensor", 3000.0, 5000.0, 0.0, 7000.0, True),
        ("series-output", 3000.0, 5000.0, 0.0, 7000.0, True),
    ],
    ids=["solar-sensor", "series-output"],
    indirect=True,
)
async def test_c_a_site_that_sees_its_sun_needs_no_verdict(hass, site):
    """The same saturating hybrid read through a solar production sensor, or
    a series hybrid's output sensor: the rating cap sees the house the sun
    serves, the car gets the 26.1 A that fits from the start, the grid never
    carries it past the allowance, and the latch - meter-only sites alone -
    never holds the battery. Measured the same before the change."""
    verdicts = []
    log = await _session(hass, site, minutes=20, on_cycle=_watching(verdicts))
    start = log[0][0]
    target_w = site.allowance_w + site.rating_w - HOUSE_W

    assert all(v is None for v in verdicts)
    assert _overruns(log, site.allowance_w) == []
    tail_w = [limit * V for t, limit, *_ in log if t > start + 60]
    assert target_w - DEADBAND_W - V <= min(tail_w) <= max(tail_w) <= target_w + DEADBAND_W
