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
# allowance W)
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
        charges from any surplus up to 5 kW. Positive = discharging."""
        if self.site.discharge_w is None:
            return 0.0
        deficit = self.demand_w - self.site.solar_w
        return max(-5000.0, min(deficit, self.site.discharge_w))

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
            set_state("sensor.battery_soc", "80",
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
    sensors, solar_w, discharge_w, allowance_w = request.param
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
        discharge_w=discharge_w, allowance_w=allowance_w,
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
