"""Off-grid with NO battery: a solar car gets the sun the house leaves.

Machine-authored tests - not yet human-reviewed.

Off-grid with no pack the sun is the whole supply, and what a Solar load can
use is solar - household, within the inverter's rating and each leg. The
off-grid pools are sized from the supply our loads hold plus the battery's
headroom (target_calculator._off_grid_held_supply), and with no battery at
all that term was None - taken as "the flow is unread" when it is 0. The solar
pool was left with nothing (off-grid export is a synthetic 0): a Solar Only
car was offered 0 A in 3 kW of spare sun, and a Solar Priority or Excess car
sat at its 6 A minimum. The physical pool fell back to the gross solar figure,
the house never taken off, which is what handed those minimums out - past the
sun where the house took all of it.

Everything here runs the REAL site cycle - engine, the load processor with its
smoothing, command-interval gate and charge pause, and the OCPP command it
sends - against a small off-grid world: one inverter with no battery, its AC
output metered on A (it follows demand: house + car), a solar production
sensor reading the sun apart from it (the one configuration where unused sun
is measured - with the output sensor alone the solar figure IS house + car,
see dev/tests/scenarios/features/test_off_grid_no_battery.yaml), a steady
house, and a 1-phase car that draws whatever limit the charger last accepted.
The clock is simulated, so minutes of site cycles run in well under a second.
"""

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.dynamic_ocpp_evse import sensor as sensor_platform
from custom_components.dynamic_ocpp_evse.const import (
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
    EVSE_MODE_EXCESS,
    EVSE_MODE_SOLAR_ONLY,
    EVSE_MODE_SOLAR_PRIORITY,
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
MIN_A = 6.0
RATING_W = 12000.0            # far above the sun: the sun binds
CYCLE_S = float(DEFAULT_SITE_UPDATE_FREQUENCY)
DEADBAND_W = DEAD_BAND * V

_CLOCKED = (
    load_builders, hub_calculation, readers, hub_result, auto_detect,
    load_entity, status, compliance,
)


class _Clock:
    def __init__(self, start):
        self.now = start

    def monotonic(self):
        return self.now

    def __getattr__(self, name):
        return getattr(time, name)


class World:
    """The off-grid site with no battery, and the car behind the charger."""

    def __init__(self, hass, site):
        self.hass = hass
        self.site = site
        self.limit = None

    @property
    def draw(self):
        if self.limit is None:
            return 0.0
        return self.limit if self.limit >= MIN_A else 0.0

    @property
    def site_w(self):
        """What the site asks of the array: with no grid and no pack, all of
        it must come from the sun."""
        return self.site.house_w + self.draw * V

    def charger_status(self):
        if self.limit is None:
            return "Preparing"
        return "Charging" if self.limit >= MIN_A else "SuspendedEVSE"

    def publish(self):
        set_state = self.hass.states.async_set
        # The output follows demand: house + car.
        set_state("sensor.inverter_out_a", str(round(self.site_w / V, 3)),
                  {"device_class": "current", "unit_of_measurement": "A"})
        set_state("sensor.solar_production", str(self.site.sun_w),
                  {"device_class": "power", "unit_of_measurement": "W"})
        set_state("sensor.evse_status_connector", self.charger_status())
        set_state("switch.evse_charge_control", "on")
        set_state("sensor.evse_current_import", str(round(self.draw, 2)),
                  {"device_class": "current", "unit_of_measurement": "A"})

    async def accept(self, domain, service, data=None, *args, **kwargs):
        if domain == "ocpp" and service == "set_charge_rate":
            period = data["custom_profile"]["chargingSchedule"]["chargingSchedulePeriod"]
            self.limit = float(period[0]["limit"])


# (wiring, mode, sun W, house W)
SITES = pytest.mark.parametrize(
    "site",
    [
        (WIRING_TOPOLOGY_SERIES, EVSE_MODE_SOLAR_ONLY.key, 4000.0, 1000.0),
        (WIRING_TOPOLOGY_PARALLEL, EVSE_MODE_SOLAR_ONLY.key, 4000.0, 1000.0),
        (WIRING_TOPOLOGY_SERIES, EVSE_MODE_SOLAR_PRIORITY.key, 4000.0, 1000.0),
        (WIRING_TOPOLOGY_PARALLEL, EVSE_MODE_SOLAR_PRIORITY.key, 4000.0, 1000.0),
        (WIRING_TOPOLOGY_SERIES, EVSE_MODE_EXCESS.key, 4000.0, 1000.0),
        (WIRING_TOPOLOGY_SERIES, EVSE_MODE_SOLAR_ONLY.key, 1500.0, 1500.0),
        (WIRING_TOPOLOGY_SERIES, EVSE_MODE_SOLAR_PRIORITY.key, 1500.0, 1500.0),
    ],
    ids=[
        "series-solar-only",
        "parallel-solar-only",
        "series-solar-priority",
        "parallel-solar-priority",
        "series-excess",
        "house-takes-the-sun-solar-only",
        "house-takes-the-sun-solar-priority",
    ],
    indirect=True,
)


@pytest.fixture
def site(hass, request):
    wiring, mode, sun_w, house_w = request.param
    hub_entry = MockConfigEntry(
        domain=DOMAIN, version=2, minor_version=2, title="Hub",
        data={CONF_NAME: "Hub", CONF_ENTITY_ID: "hub", ENTRY_TYPE: ENTRY_TYPE_HUB},
        options={
            CONF_PHASE_VOLTAGE: V,
            CONF_MAIN_BREAKER_RATING: 40,
            # No grid CT and no battery entity at all.
            CONF_INVERTER_OUTPUT_PHASE_A_ENTITY_ID: "sensor.inverter_out_a",
            CONF_SOLAR_PRODUCTION_ENTITY_ID: "sensor.solar_production",
            CONF_INVERTER_MAX_POWER: RATING_W,
            CONF_WIRING_TOPOLOGY: wiring,
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
            }
        },
        "loads": {
            evse.entry_id: {
                "entry": evse,
                "hub_entry_id": hub_entry.entry_id,
                "dynamic_control": True,
                "operating_mode": mode,
            }
        },
    }
    return SimpleNamespace(
        hub=hub_entry, evse=evse, mode=mode, sun_w=sun_w, house_w=house_w,
    )


async def _session(hass, site, minutes):
    """``minutes`` of real site cycles against the world. Returns
    [(seconds, accepted limit, site W)] per cycle."""
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
            log.append((clock.now, world.limit, world.site_w))
            clock.now += CYCLE_S
    finally:
        for p in reversed(patches):
            p.stop()
    return log


@SITES
async def test_a_solar_car_charges_on_the_sun_the_house_leaves(hass, site):
    """The car settles at (sun - house) / V - 13.0 A on 4 kW of sun behind a
    1 kW house, 0 A where the house takes all of it - and the site never asks
    the array for more than the sun once past the start.

    Before the fix, measured on this rig (settled command after 3 minutes):

    ===========================  ======  =====
    site                         before  after
    ===========================  ======  =====
    series, Solar Only           0 A     13 A
    parallel, Solar Only         0 A     13 A
    series, Solar Priority       6 A     13 A
    parallel, Solar Priority     6 A     13 A
    series, Excess               6 A     13 A
    house takes the sun, SO      0 A     0 A
    house takes the sun, SP      6 A     0 A
    ===========================  ======  =====

    (the last one 1380 W past the sun: no grid and no pack to back a minimum).
    """
    log = await _session(hass, site, minutes=5)
    start = log[0][0]
    target = max(0.0, (site.sun_w - site.house_w) / V)

    tail = [(limit or 0.0, w) for t, limit, w in log if t > start + 180]
    settled = tail[-1][0]
    assert settled == pytest.approx(target, abs=1.0), (
        f"{site.mode} car settled at {settled:.1f} A with {site.sun_w:.0f} W of "
        f"sun and a {site.house_w:.0f} W house: {target - settled:+.1f} A short "
        f"of the {target:.1f} A the sun leaves"
    )
    worst_w = max(w for _, w in tail)
    assert worst_w <= max(site.sun_w, site.house_w) + DEADBAND_W, (
        f"the site asked {worst_w:.0f} W of {site.sun_w:.0f} W of sun, "
        "with no battery to cover the rest"
    )


@SITES
async def test_solar_remaining_is_the_sun_less_the_house(hass, site):
    """Solar Remaining Power, read through the real hub sensor once the car has
    settled, is the solar pool's sun share - sun less house, 3000 W here, 0 W
    where the house takes it all. (Before the fix it read 0 W: the pool it is
    published from had nothing in it without a battery flow.)"""
    from custom_components.dynamic_ocpp_evse.sensor import (
        DynamicOcppEvseHubDataSensor,
        HUB_SENSOR_DEFINITIONS,
    )

    await _session(hass, site, minutes=3)
    sensor = next(
        DynamicOcppEvseHubDataSensor(hass, site.hub, "Hub", "hub", d)
        for d in HUB_SENSOR_DEFINITIONS
        if d["hub_data_key"] == "available_solar_power"
    )
    await sensor.async_update()
    expected_w = max(0.0, site.sun_w - site.house_w)
    assert sensor.native_value == pytest.approx(expected_w, abs=DEADBAND_W), (
        f"Solar Remaining Power {sensor.native_value} W with {site.sun_w:.0f} W "
        f"of sun and a {site.house_w:.0f} W house"
    )
