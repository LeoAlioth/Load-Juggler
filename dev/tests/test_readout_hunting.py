"""The hunting a readout stuck at 0 causes - reproduced, then stopped.

Machine-authored tests - not yet human-reviewed.

The owner's field report (2026-09-23): "evse allocates lets say 5 kW, the
total house load goes from 1 kW to 6 kW, but because the EVSE readout stays at
0, the load juggler interprets that as the other house loads went up for 5 kW.
now if the total grid allowance was 6 kW, that in turn makes the load juggler
think the EVSE needs to stop completely."

Everything here runs the REAL site cycle - engine, feedback loop, the load
processor with its smoothing, command-interval gate and charge pause, and the
OCPP command it sends - against a small model of the world:

  * a 1 kW house on phase A and a 6 kW grid allowance (Max Import Power);
  * a 1-phase car that draws whatever limit the charger was last sent;
  * a grid CT that sees house + car, and a charger status that follows the
    car (Charging while it draws, SuspendedEVSE while held at 0 A);
  * the charger's Current Import reading, which is healthy in a first session
    (so the watch learns how often it reports) and pinned at 0.0 in a second.

The clock is simulated (every module's ``time.monotonic``), so twenty minutes
of site cycles run in well under a second.
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
    CONF_LOAD_PRIORITY,
    CONF_MAIN_BREAKER_RATING,
    CONF_MAX_IMPORT_POWER_ENTITY_ID,
    CONF_NAME,
    CONF_OCPP_DEVICE_ID,
    CONF_BATTERY_MAX_CHARGE_POWER,
    CONF_BATTERY_MAX_DISCHARGE_POWER,
    CONF_BATTERY_POWER_ENTITY_ID,
    CONF_BATTERY_SOC_ENTITY_ID,
    CONF_INVERTER_MAX_POWER,
    CONF_INVERTER_OUTPUT_PHASE_A_ENTITY_ID,
    CONF_WIRING_TOPOLOGY,
    WIRING_TOPOLOGY_SERIES,
    CONF_PHASE_A_CURRENT_ENTITY_ID,
    CONF_PHASE_VOLTAGE,
    CONF_PHASES,
    CONF_PROFILE_VALIDITY_MODE,
    CONF_UPDATE_FREQUENCY,
    DEAD_BAND,
    DEFAULT_SITE_UPDATE_FREQUENCY,
    DOMAIN,
    ENTRY_TYPE,
    ENTRY_TYPE_HUB,
    ENTRY_TYPE_LOAD,
    EVSE_RT_READOUT_WATCH,
)
from custom_components.dynamic_ocpp_evse.engine import (
    auto_detect,
    hub_calculation,
    hub_result,
    load_builders,
    readers,
    readout_watch,
)
from custom_components.dynamic_ocpp_evse.control import compliance, status
from custom_components.dynamic_ocpp_evse.entities import load as load_entity

V = 230.0
HOUSE_W = 1000.0
ALLOWANCE_W = 6000.0
MIN_A = 6.0
CYCLE_S = float(DEFAULT_SITE_UPDATE_FREQUENCY)
# How far over the allowance a settled charger may sit: the permit's Schmitt
# trigger deliberately holds a command within DEAD_BAND of its target.
SETTLED_W = ALLOWANCE_W + DEAD_BAND * V
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
    """The site the integration is controlling, and the charger's readout."""

    def __init__(self, hass, off_grid=False):
        self.hass = hass
        self.off_grid = off_grid
        self.house_a = HOUSE_W / V
        self.limit = None          # the last limit the charger accepted (A)
        self.plugged = True
        self.reading_frozen = False
        self.car_draws = True      # False: a car that takes nothing when offered
        self.now = 0.0             # simulated seconds, kept by _run
        # Optional scripts, callables of the World: extra house load (A), and
        # whether the car takes current right now.
        self.extra_house = None
        self.car_takes = None
        self.on_new_limit = None   # called when the charger accepts a new limit
        self.extra_a = 0.0
        self._reports = 0

    @property
    def draw(self):
        if not self.plugged or not self.car_draws or self.limit is None:
            return 0.0
        if self.car_takes is not None and not self.car_takes(self):
            return 0.0
        return self.limit if self.limit >= MIN_A else 0.0

    @property
    def grid_w(self):
        return (self.house_a + self.extra_a + self.draw) * V

    def charger_status(self):
        if not self.plugged:
            return "Available"
        if self.limit is None:
            return "Preparing"
        return "Charging" if self.limit >= MIN_A else "SuspendedEVSE"

    def publish(self, cycle):
        """Set this cycle's entity states."""
        set_state = self.hass.states.async_set
        if self.extra_house is not None:
            self.extra_a = self.extra_house(self)
        site_amps = round(self.house_a + self.extra_a + self.draw, 3)
        if self.off_grid:
            # No grid: the hybrid's AC output IS everything the site draws, and
            # at night the battery supplies all of it.
            set_state("sensor.inverter_out_a", str(site_amps),
                      {"device_class": "current", "unit_of_measurement": "A"})
            set_state("sensor.battery_power", str(round(site_amps * V, 1)),
                      {"device_class": "power", "unit_of_measurement": "W"})
            set_state("sensor.battery_soc", "80",
                      {"device_class": "battery", "unit_of_measurement": "%"})
        else:
            set_state("sensor.grid_a", str(site_amps),
                      {"device_class": "current", "unit_of_measurement": "A"})
            set_state("sensor.grid_allowance", str(ALLOWANCE_W),
                      {"device_class": "power", "unit_of_measurement": "W"})
        set_state("sensor.evse_status_connector", self.charger_status())
        set_state("switch.evse_charge_control", "on")
        if self.reading_frozen:
            reading = 0.0
        else:
            # A healthy meter: a new value every fifth cycle (10 s), a hair of
            # measurement noise on the true draw.
            if cycle % 5 == 0:
                self._reports += 1
            reading = round(self.draw + (0.05 * (self._reports % 3) if self.draw else 0.0), 2)
        set_state("sensor.evse_current_import", str(reading),
                  {"device_class": "current", "unit_of_measurement": "A"})

    async def accept(self, domain, service, data=None, *args, **kwargs):
        """The mocked service registry: the charger accepts every profile."""
        if domain == "ocpp" and service == "set_charge_rate":
            period = data["custom_profile"]["chargingSchedule"]["chargingSchedulePeriod"]
            limit = float(period[0]["limit"])
            if limit != self.limit and self.on_new_limit is not None:
                self.on_new_limit(self, self.limit, limit)
            self.limit = limit


def _make_site(hass, off_grid=False):
    """Grid-tied: a 6 kW Max Import Power on phase A. Off-grid: no grid CT at
    all, and a series hybrid (the hub's own inverter fields) rated 6 kW with
    its AC output metered on phase A and a large battery behind it - so the
    binding limit is again 6 kW, now the inverter's."""
    if off_grid:
        hub_options = {
            CONF_PHASE_VOLTAGE: V,
            CONF_MAIN_BREAKER_RATING: 40,
            CONF_INVERTER_OUTPUT_PHASE_A_ENTITY_ID: "sensor.inverter_out_a",
            CONF_INVERTER_MAX_POWER: ALLOWANCE_W,
            CONF_WIRING_TOPOLOGY: WIRING_TOPOLOGY_SERIES,
            CONF_BATTERY_SOC_ENTITY_ID: "sensor.battery_soc",
            CONF_BATTERY_POWER_ENTITY_ID: "sensor.battery_power",
            CONF_BATTERY_MAX_DISCHARGE_POWER: 20000,
            CONF_BATTERY_MAX_CHARGE_POWER: 5000,
        }
    else:
        hub_options = {
            CONF_PHASE_A_CURRENT_ENTITY_ID: "sensor.grid_a",
            CONF_MAIN_BREAKER_RATING: 40,
            CONF_PHASE_VOLTAGE: V,
            CONF_MAX_IMPORT_POWER_ENTITY_ID: "sensor.grid_allowance",
        }
    hub_entry = MockConfigEntry(
        domain=DOMAIN, version=2, minor_version=2, title="Hub",
        data={CONF_NAME: "Hub", CONF_ENTITY_ID: "hub", ENTRY_TYPE: ENTRY_TYPE_HUB},
        options=hub_options,
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
    return SimpleNamespace(hub=hub_entry, evse=evse, off_grid=off_grid)


@pytest.fixture
def site(hass):
    return _make_site(hass)


@pytest.fixture
def off_grid_site(hass):
    return _make_site(hass, off_grid=True)


async def _run(hass, site, world, cycles, clock, log, cycle0=0):
    """``cycles`` real site cycles against the world, one CYCLE_S apart.
    Appends (seconds, limit, grid_w) per cycle to ``log``."""
    processor = hass.data[DOMAIN]["load_processors"][site.hub.entry_id][site.evse.entry_id]
    for i in range(cycles):
        world.now = clock.now
        world.publish(cycle0 + i)
        await sensor_platform.async_run_hub_cycle(hass, site.hub)
        log.append((clock.now, world.limit, world.grid_w, processor))
        clock.now += CYCLE_S
    return cycle0 + cycles


def _stops(log, since=0.0):
    """How many times the charger was cut from running to 0 A."""
    limits = [limit for t, limit, _, _ in log if t >= since and limit is not None]
    return sum(1 for a, b in zip(limits, limits[1:]) if a >= MIN_A and b < MIN_A)


async def _session(hass, site, *, lockstep_enabled, frozen_minutes=20, car_draws=True,
                   script=None, frozen=True):
    """A healthy first session (the watch learns the meter's cadence), the car
    leaves, a second car arrives - and this time the readout is pinned at 0.
    Returns (log of the second session, the load's watch state)."""
    world = World(hass, off_grid=site.off_grid)
    clock = _Clock(10_000.0)
    evse_sensor = sensor_platform.LoadJugglerDeviceSensor(
        hass, site.evse, site.hub, "evse", "evse"
    )
    hass.data[DOMAIN].setdefault("load_processors", {}).setdefault(
        site.hub.entry_id, {}
    )[site.evse.entry_id] = evse_sensor

    patches = [patch.object(module, "time", clock) for module in _CLOCKED]
    patches.append(patch(
        "homeassistant.core.ServiceRegistry.async_call",
        new_callable=AsyncMock, side_effect=world.accept,
    ))
    if not lockstep_enabled:
        patches.append(patch.object(
            hub_calculation, "_watch_readouts_against_household", lambda *a, **k: None
        ))
    for p in patches:
        p.start()
    try:
        first = []
        cycle = await _run(hass, site, world, 90, clock, first)           # 3 min healthy
        world.plugged = False
        cycle = await _run(hass, site, world, 5, clock, first, cycle)     # car leaves
        world.plugged, world.limit = True, None
        world.reading_frozen = frozen
        world.car_draws = car_draws
        world.session_start = clock.now
        if script is not None:
            script(world)
        second = []
        await _run(hass, site, world, int(frozen_minutes * 60 / CYCLE_S), clock, second, cycle)
    finally:
        for p in reversed(patches):
            p.stop()
    watch = hass.data[DOMAIN]["loads"][site.evse.entry_id][EVSE_RT_READOUT_WATCH]
    return first, second, watch


async def test_the_healthy_session_runs_steady_at_the_allowance(hass, site):
    """Control: with a working meter the same site charges at 5 kW without a
    single stop - so whatever the frozen session does is the readout's fault.

    (The first half-minute overshoots the allowance by up to ~430 W. That is
    NOT this feature: the managed-draw EMA is advanced twice per cycle -
    engine/hub_calculation._apply_feedback_loop and _charge_control_view both
    call _managed_phase_draws with the EMA dict - so on every rise the draw
    term leads the grid term and the household reads low for a few cycles.
    Pre-existing; reported separately. Only the settled tail is pinned.)"""
    first, _, watch = await _session(hass, site, lockstep_enabled=True, frozen_minutes=0.1)
    assert _stops(first) == 0
    running = [limit for _, limit, _, _ in first[20:90] if limit is not None]
    assert min(running) >= MIN_A
    assert max(g for _, _, g, _ in first[45:90]) <= SETTLED_W
    assert readout_watch.normal_gap(watch) is not None, "the cadence was learned"


async def test_a_readout_stuck_at_zero_hunts_without_the_household_check(hass, site):
    """The field report, reproduced. Each start books the car's 5 kW as house
    load, the allowance is gone, the charger is cut and paused; the "house
    load" vanishes, the pause ends, and it starts over. Twenty minutes, one
    stop every pause."""
    _, second, watch = await _session(hass, site, lockstep_enabled=False)
    stops = _stops(second)
    assert stops >= 4, f"expected the charger to hunt, it stopped {stops} times"
    # ...and it never charged for more than one command interval at a time.
    assert not readout_watch.is_stuck(watch)


async def test_the_household_check_stops_the_hunting(hass, site):
    """Same world, with the lockstep path. The first start and cut are the
    evidence; from the restart on the charger is controlled blind, its assumed
    5 kW is taken out of the household, and it charges straight through - at
    the allowance, never over it."""
    _, second, watch = await _session(hass, site, lockstep_enabled=True)

    assert readout_watch.is_stuck(watch)
    assert watch["stuck_how"] == readout_watch.HOUSEHOLD_LOCKSTEP
    assert _stops(second) <= 1, f"stopped {_stops(second)} times"

    # After the one pause, it runs to the end without another stop...
    restart = next(
        t for (t, a, _, _), (_, b, _, _) in zip(second, second[1:])
        if (a or 0) < MIN_A and (b or 0) >= MIN_A and t > second[0][0] + 60
    )
    tail = [(t, limit, grid) for t, limit, grid, _ in second if t > restart + 30]
    assert tail and min(limit for _, limit, _ in tail) >= MIN_A
    # ...at essentially the allowance: the engine sees the house at 1 kW again.
    settled = [grid for t, _, grid in tail if t > restart + 60]
    assert max(settled) <= SETTLED_W, max(settled)
    assert min(settled) >= ALLOWANCE_W - DEAD_BAND * V - V, min(settled)

    attrs = second[-1][3].extra_state_attributes
    assert attrs["readout_stuck"] is True
    assert attrs["readout_stuck_evidence"] == "household_lockstep"
    assert attrs["readout_assumed_current"][0] >= MIN_A


async def test_a_car_that_takes_nothing_is_not_mistaken_for_a_stuck_readout(hass, site):
    """Status Charging, a reading of 0.0 - and it is TRUE: the car is full and
    takes nothing it is offered. The grid never answers our commands, so the
    watch never judges, and nothing is assumed."""
    _, second, watch = await _session(
        hass, site, lockstep_enabled=True, frozen_minutes=10, car_draws=False
    )
    assert not readout_watch.is_stuck(watch)
    assert _stops(second) == 0


async def test_a_house_load_that_coincides_with_a_command_is_not_a_stuck_readout(
    hass, site, monkeypatch
):
    """The hardest honest case for the lockstep: the reading IS 0.0 and it is
    TRUE (the car in Charging takes nothing), a heat pump cycling every three
    minutes keeps moving our command, and a 2.3 kW kettle switches on in the
    very cycle we RAISE the limit - and stays on for 90 s. That coincidence is
    one answered change, and the watch does count it; the engine's own
    reaction to the house loads is the opposite sign; nothing is ever judged
    stuck."""
    runs = []
    judged = readout_watch._judged

    def spy(chain, pending, after, leg_phases, now):
        run = judged(chain, pending, after, leg_phases, now)
        runs.append((now, len(run)))
        return run

    monkeypatch.setattr(readout_watch, "_judged", spy)

    def script(world):
        state = {"kettle_from": None}

        def heat_pump_and_kettle(w):
            since = w.now - w.session_start
            pump = 1500.0 / V if int(since // 180) % 2 else 0.0
            kettle_from = state["kettle_from"]
            kettle = 2300.0 / V if kettle_from is not None and w.now - kettle_from < 90 else 0.0
            return pump + kettle

        def coincide(w, old, new):
            # The first command after the first heat-pump cycle: the kettle
            # goes on at exactly that moment.
            if (
                state["kettle_from"] is None
                and old is not None
                and new > old
                and w.now - w.session_start > 200
            ):
                state["kettle_from"] = w.now
                world.kettle_at = w.now

        world.extra_house = heat_pump_and_kettle
        world.on_new_limit = coincide
        world.car_draws = False

    _, second, watch = await _session(
        hass, site, lockstep_enabled=True, frozen_minutes=15, script=script
    )
    limits = {limit for _, limit, _, _ in second if limit is not None}
    assert len(limits) >= 3, f"the house loads should have moved the command: {limits}"
    in_session = [n for t, n in runs if t >= second[0][0]]
    assert max(in_session) == 1, (
        "the kettle should have answered exactly one change, and nothing more: "
        f"longest run {max(in_session)}"
    )
    assert not readout_watch.is_stuck(watch)


async def test_a_car_that_briefly_takes_nothing_is_not_a_stuck_readout(hass, site):
    """A healthy readout and a car that twice stops taking current for 40 s
    while its connector says Charging (BMS balancing, a pre-heat): the reading
    says 0.0 and is right. No stop, and nothing judged stuck."""
    def script(world):
        world.car_takes = lambda w: not (
            60 <= (w.now - w.session_start) % 240 < 100
        )

    _, second, watch = await _session(
        hass, site, lockstep_enabled=True, frozen_minutes=10, script=script,
        frozen=False,
    )
    assert not readout_watch.is_stuck(watch)
    assert _stops(second) == 0


# ── Off-grid: the same hunting, through the inverter's capacity ────────────
#
# No grid CTs, so no grid allowance - but the inverter is the allowance, and
# the engine sizes it on the household it reconstructs from the inverter's own
# AC output: output minus every managed draw. A readout pinned at 0 leaves the
# charger's draw in that household, and the 6 kW inverter "fills" with house
# load the moment the charger starts - the grid-tied hunt, one component over.
# The watch follows the same reconstruction off-grid: the inverter output
# minus our draws steps with our commands to the charger.


async def test_off_grid_the_healthy_session_stays_inside_the_inverter(hass, off_grid_site):
    """Control, off-grid: with a working meter the charger starts and runs at
    the inverter's allowance without a stop - and without ever putting the
    inverter over its rating, the start included.

    Until 2026-09-24 every start overshot by up to 520 W for about 30 s: the
    household is the SMOOTHED inverter output minus the managed draw, and the
    draw was taken RAW, so the car's whole draw came off while the output it
    is part of was still catching up and the house read as having shrunk
    (dev/tests/test_offgrid_household_smoothing.py). Now the whole session is
    pinned, not only its settled tail."""
    first, _, _ = await _session(
        hass, off_grid_site, lockstep_enabled=True, frozen_minutes=0.1
    )
    assert _stops(first) == 0
    running = [limit for _, limit, _, _ in first[20:90] if limit is not None]
    assert min(running) >= MIN_A
    peak = max(supply for _, _, supply, _ in first)
    assert peak <= SETTLED_W, (
        f"the inverter carried {peak - ALLOWANCE_W:.0f} W over its "
        f"{ALLOWANCE_W:.0f} W rating"
    )


async def test_off_grid_a_readout_stuck_at_zero_hunts_without_the_check(hass, off_grid_site):
    _, second, watch = await _session(hass, off_grid_site, lockstep_enabled=False)
    stops = _stops(second)
    assert stops >= 4, f"expected the charger to hunt off-grid, it stopped {stops} times"
    assert not readout_watch.is_stuck(watch)


async def test_off_grid_the_household_check_stops_the_hunting(hass, off_grid_site):
    _, second, watch = await _session(hass, off_grid_site, lockstep_enabled=True)

    assert readout_watch.is_stuck(watch)
    assert watch["stuck_how"] == readout_watch.HOUSEHOLD_LOCKSTEP
    # As grid-tied: the first start and cut are the evidence, and from the
    # restart after the one pause the charger is controlled blind. (Until
    # 2026-09-24 this run did not stop off-grid, for two reasons that were
    # both bugs: the 0 A the reading had held since the car arrived counted
    # as a SETTLED draw once charging began and lifted every permit through
    # the squeeze by the car's 6 A minimum - dev/tests/test_start_settle.py -
    # and the previous car, gone 10 s before, still sat in the smoothed
    # output while its raw draw was already 0, so the house read high and the
    # second car was started 4 A low - dev/tests/test_offgrid_household_
    # smoothing.py. With a two-minute gap it stopped once then too.)
    assert _stops(second) <= 1, f"stopped {_stops(second)} times"
    entered = watch["stuck_since"]
    restarts = [
        t for (t, a, _, _), (_, b, _, _) in zip(second, second[1:])
        if (a or 0) < MIN_A and (b or 0) >= MIN_A and t > second[0][0] + 60
    ]
    since = max([entered] + restarts)
    # From the verdict - or the restart after the one pause - it runs to the
    # end at the inverter's 6 kW rating, and at no point over it: the
    # household is the smoothed output minus the SAME smoothed draw, blind
    # loads at their assumed current, so neither the verdict nor the restart
    # reads as the house shrinking.
    tail = [(t, limit, supply) for t, limit, supply, _ in second if t > since]
    assert tail and min(limit for _, limit, _ in tail) >= MIN_A
    peak = max(supply for _, _, supply in tail)
    assert peak <= SETTLED_W, f"{peak - ALLOWANCE_W:.0f} W over the inverter"
    settled = [supply for t, _, supply in tail if t > since + 60]
    assert min(settled) >= ALLOWANCE_W - DEAD_BAND * V - V, min(settled)


async def test_off_grid_a_car_that_takes_nothing_is_not_a_stuck_readout(
    hass, off_grid_site
):
    """Off-grid, a true 0.0 in Charging and a heat pump moving our command
    every three minutes: the inverter output never answers the charger's
    commands, so nothing is judged."""
    def script(world):
        world.car_draws = False
        world.extra_house = lambda w: (
            1500.0 / V if int((w.now - w.session_start) // 180) % 2 else 0.0
        )

    _, second, watch = await _session(
        hass, off_grid_site, lockstep_enabled=True, frozen_minutes=12, script=script
    )
    limits = {limit for _, limit, _, _ in second if limit is not None}
    assert len(limits) >= 3, f"the heat pump should have moved the command: {limits}"
    assert not readout_watch.is_stuck(watch)
