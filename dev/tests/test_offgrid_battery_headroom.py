"""Off-grid, battery-limited: a charger keeps the supply it already holds.

Machine-authored tests - not yet human-reviewed.

Found 2026-09-24 while testing the stuck-readout fix (9660cfc), with a
CORRECT meter reading: a 3 kW car behind a 1 kW house on a 5 kW battery was
permitted 0. Off-grid the battery's discharge in flight carries the car's own
draw, and the engine's pool was ``solar + (discharge rating - discharge in
flight)`` - so the 3 kW the car already held was booked as spent and 1 kW
(4.3 A, under its 6 A minimum) was left. Cut to 0 the discharge falls back to
the house, the 4 kW returns, the car is allowed again: it hunts. The pool the
car should see is the rating less the house - 4 kW.

Everything here runs the REAL site cycle - engine, the load processor with its
smoothing, command-interval gate and charge pause, and the OCPP command it
sends - against a small off-grid world:

  * a series hybrid (the hub's own inverter fields) and a 5 kW battery behind
    it, covering whatever the site draws beyond solar;
  * three sites. At night (1 kW house, no sun) read through the inverter's AC
    output sensor, and by day (2.6 kW house, 1.5 kW sun) read through a
    dedicated solar production sensor and no output sensor at all - on both
    the inverter is rated far above the battery, so the battery's discharge
    rating binds. And the same solar-sensor site with a 4 kW inverter and a
    1 kW house, where the INVERTER binds: that configuration's household is
    solar + battery, which off-grid is the whole site - our car included -
    until the managed draws are taken off it, and the inverter's capacity is
    sized on that household;
  * a 1-phase car that draws whatever limit the charger last accepted, behind a
    healthy Current Import reading.

The clock is simulated (every module's ``time.monotonic``), so ten minutes of
site cycles run in well under a second.
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
DISCHARGE_W = 5000.0          # the battery's discharge rating
MIN_A = 6.0
CYCLE_S = float(DEFAULT_SITE_UPDATE_FREQUENCY)
# How far a settled charger may sit from its target: the permit's Schmitt
# trigger holds a command within DEAD_BAND of it.
DEADBAND_W = DEAD_BAND * V
# (how the site is read, house W, solar W, inverter rating W)
SITES = pytest.mark.parametrize(
    "site",
    [
        ("output", 1000.0, 0.0, 12000.0),
        ("solar", 2600.0, 1500.0, 12000.0),
        ("solar", 1000.0, 1500.0, 4000.0),
    ],
    ids=[
        "night-output-sensor-battery-binds",
        "day-solar-sensor-battery-binds",
        "day-solar-sensor-inverter-binds",
    ],
    indirect=True,
)


def _binding(site):
    """``(the car's headroom in A, the rating that binds in W, which world
    figure that rating bounds)`` - the battery's (solar + rating − house) or
    the inverter's (rating − house), whichever is less."""
    battery_room = site.solar_w + DISCHARGE_W - site.house_w
    inverter_room = site.inverter_w - site.house_w
    if battery_room <= inverter_room:
        return battery_room / V, DISCHARGE_W, "battery_w"
    return inverter_room / V, site.inverter_w, "site_w"


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
    """The off-grid site, and the car behind the charger."""

    def __init__(self, hass, site):
        self.hass = hass
        self.site = site
        self.limit = None          # the last limit the charger accepted (A)

    @property
    def draw(self):
        if self.limit is None:
            return 0.0
        return self.limit if self.limit >= MIN_A else 0.0

    @property
    def site_w(self):
        return self.site.house_w + self.draw * V

    @property
    def battery_w(self):
        # No grid: the battery supplies everything the sun does not.
        return self.site_w - self.site.solar_w

    def charger_status(self):
        if self.limit is None:
            return "Preparing"
        return "Charging" if self.limit >= MIN_A else "SuspendedEVSE"

    def publish(self):
        set_state = self.hass.states.async_set
        if self.site.sensors == "output":
            # A series hybrid's AC output is everything the site draws.
            set_state("sensor.inverter_out_a", str(round(self.site_w / V, 3)),
                      {"device_class": "current", "unit_of_measurement": "A"})
        else:
            set_state("sensor.solar_production", str(self.site.solar_w),
                      {"device_class": "power", "unit_of_measurement": "W"})
        set_state("sensor.battery_power", str(round(self.battery_w, 1)),
                  {"device_class": "power", "unit_of_measurement": "W"})
        set_state("sensor.battery_soc", "80",
                  {"device_class": "battery", "unit_of_measurement": "%"})
        set_state("sensor.evse_status_connector", self.charger_status())
        set_state("switch.evse_charge_control", "on")
        # A healthy meter: the car's real draw.
        set_state("sensor.evse_current_import", str(round(self.draw, 2)),
                  {"device_class": "current", "unit_of_measurement": "A"})

    async def accept(self, domain, service, data=None, *args, **kwargs):
        """The mocked service registry: the charger accepts every profile."""
        if domain == "ocpp" and service == "set_charge_rate":
            period = data["custom_profile"]["chargingSchedule"]["chargingSchedulePeriod"]
            self.limit = float(period[0]["limit"])


@pytest.fixture
def site(hass, request):
    sensors, house_w, solar_w, inverter_w = request.param
    reading = (
        {CONF_INVERTER_OUTPUT_PHASE_A_ENTITY_ID: "sensor.inverter_out_a"}
        if sensors == "output"
        else {CONF_SOLAR_PRODUCTION_ENTITY_ID: "sensor.solar_production"}
    )
    hub_entry = MockConfigEntry(
        domain=DOMAIN, version=2, minor_version=2, title="Hub",
        data={CONF_NAME: "Hub", CONF_ENTITY_ID: "hub", ENTRY_TYPE: ENTRY_TYPE_HUB},
        options={
            CONF_PHASE_VOLTAGE: V,
            CONF_MAIN_BREAKER_RATING: 40,
            **reading,
            CONF_INVERTER_MAX_POWER: inverter_w,
            CONF_WIRING_TOPOLOGY: WIRING_TOPOLOGY_SERIES,
            CONF_BATTERY_SOC_ENTITY_ID: "sensor.battery_soc",
            CONF_BATTERY_POWER_ENTITY_ID: "sensor.battery_power",
            CONF_BATTERY_MAX_DISCHARGE_POWER: DISCHARGE_W,
            CONF_BATTERY_MAX_CHARGE_POWER: 5000,
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
        hub=hub_entry, evse=evse, sensors=sensors,
        house_w=house_w, solar_w=solar_w, inverter_w=inverter_w,
    )


async def _session(hass, site, minutes):
    """``minutes`` of real site cycles against the world, one CYCLE_S apart.
    Returns [(seconds, accepted limit, {world figure: W})] per cycle."""
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
            world.publish()
            await sensor_platform.async_run_hub_cycle(hass, site.hub)
            log.append((
                clock.now, world.limit,
                {"battery_w": world.battery_w, "site_w": world.site_w},
            ))
            clock.now += CYCLE_S
    finally:
        for p in reversed(patches):
            p.stop()
    return log


def _stops(log, since):
    """How many times the charger was cut from running to 0 A."""
    limits = [limit for t, limit, _ in log if t >= since and limit is not None]
    return sum(1 for a, b in zip(limits, limits[1:]) if a >= MIN_A and b < MIN_A)


@SITES
async def test_a_limited_car_charges_through_at_the_limit(hass, site):
    """The car starts, rises to what the binding rating leaves after the house,
    and stays there: not one stop after the first minute, and the battery (or
    inverter) never asked for more than its rating once settled.

    Before the fix, measured: at night on the output sensor the car hunted -
    17.4 A for one command interval, cut to 0 A when its own 4 kW came off the
    discharge headroom, paused, restarted: 2 stops after the first minute of
    ten. On the solar sensor the error ran the other way, the pool being solar
    + the FULL rating with the house never taken off: the battery settled at
    5.8 kW against its 5 kW. And with the inverter binding, its capacity was
    sized on a household that contained the car, and the car hunted again
    (2 stops) - which it still does with only the pool's formula fixed, so
    this case is the one that pins the household figure."""
    log = await _session(hass, site, minutes=10)
    start = log[0][0]
    headroom, rating, figure = _binding(site)

    stops = _stops(log, since=start + 60)
    assert stops == 0, f"the car hunted: {stops} stops after the first minute"

    tail = [(limit, w[figure]) for t, limit, w in log if t > start + 180]
    lowest = min(limit for limit, _ in tail)
    assert lowest >= headroom - 1.0, (lowest, headroom)
    highest_w = max(w for _, w in tail)
    assert highest_w <= rating + DEADBAND_W, (highest_w, rating)
    lowest_w = min(w for _, w in tail)
    assert lowest_w >= rating - DEADBAND_W - V, (lowest_w, rating)


@SITES
async def test_the_start_does_not_push_past_the_limit(hass, site):
    """Across the whole session, the start included, the battery (or
    inverter) is never asked for more than its rating plus the permit's
    deadband.

    The pool now hands the car's own draw back to it, and a RAW draw would lead
    the smoothed battery reading on the start: measured on this rig at night,
    a raw draw handed the car a 6.8 kW pool the cycle after it started at
    4 kW, commanded it up to 20 A and asked 5.6 kW of the 5 kW battery. The
    engine's smoothed managed draw holds it at 17.4 A and 5.0 kW from the first
    command. (Before the fix: 6.4 kW of the battery at night, on a restart
    after a cut; 7.6 kW of it by day on the solar sensor; 5.4 kW of the 4 kW
    inverter.)"""
    log = await _session(hass, site, minutes=5)
    _, rating, figure = _binding(site)
    worst = max(w[figure] for *_, w in log)
    assert worst <= rating + DEADBAND_W, f"asked for {worst:.0f} W against {rating:.0f} W"
