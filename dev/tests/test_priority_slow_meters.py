"""Two chargers on one breaker, both reporting every 30 s - Andrej's site.

Machine-authored tests - not yet human-reviewed.

The field case (Andrej, 4 Oct 2026, 20 A main breaker, a 15-minute block
power limit as Max Import Power): two 3-phase go-eCharger V4s, 6-16 A,
commanded every 30 s, each sending a meter value every 30 s. In Priority mode
the priority-1 charger ("Charger") was commanded 7 -> 15 -> 8 -> 14 -> 8 ->
11 A, a new value every 30 s, and its car followed every one; the priority-2
charger ("Charger Nova"), plugged in at 19:13, was allocated 0 for 15-25 s at
a time and paused for 3 minutes, again and again - 0.19 kWh in 8 minutes. In
Optimized mode (19:41-19:45) its allocation flickered 6 / 0 every few seconds
and every flicker that met a command paused it.

Everything here runs the REAL hub cycle - engine, feedback loop, both load
processors with their smoothing, command gate and charge pause, and the OCPP
commands - against a small model of that site:

  * a 20 A breaker, a house of 1.7/1.3/1.3 A, grid CTs on all three phases,
    and optionally a block power allowance on the site total;
  * two 3-phase cars that draw whatever limit their charger was last sent,
    until a car decides to stop (SuspendedEV);
  * per charger, a Current Import reading that moves only with a meter value
    every 30 s, zeroed at a suspension and re-written with every status
    change - the way the ocpp integration shows a go-eCharger's.

The clock is simulated, so a quarter of an hour runs in about a second.
"""

from types import SimpleNamespace

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.dynamic_ocpp_evse import sensor as sensor_platform
from custom_components.dynamic_ocpp_evse.const import (
    CONF_CHARGE_PAUSE_DURATION,
    CONF_CHARGE_RATE_UNIT,
    CONF_CHARGER_ID,
    CONF_CHARGER_L1_PHASE,
    CONF_CHARGER_L2_PHASE,
    CONF_CHARGER_L3_PHASE,
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
    CONF_PHASE_A_CURRENT_ENTITY_ID,
    CONF_PHASE_B_CURRENT_ENTITY_ID,
    CONF_PHASE_C_CURRENT_ENTITY_ID,
    CONF_PHASE_VOLTAGE,
    CONF_PHASES,
    CONF_PROFILE_VALIDITY_MODE,
    CONF_UPDATE_FREQUENCY,
    DEFAULT_SITE_UPDATE_FREQUENCY,
    DISTRIBUTION_MODE_PRIORITY,
    DISTRIBUTION_MODE_SEQUENTIAL_OPTIMIZED,
    DISTRIBUTION_MODE_SEQUENTIAL_STRICT,
    DISTRIBUTION_MODE_SHARED,
    DOMAIN,
    ENTRY_TYPE,
    ENTRY_TYPE_HUB,
    ENTRY_TYPE_LOAD,
)

from .closed_loop import clocked

V = 230.0
BREAKER_A = 20.0
HOUSE_A = (1.7, 1.3, 1.3)
MIN_A, MAX_A = 6.0, 16.0
CYCLE_S = float(DEFAULT_SITE_UPDATE_FREQUENCY)
METER_EVERY = int(30 / CYCLE_S)
PLUG_IN = int(3 * 60 / CYCLE_S)       # the second car arrives after 3 minutes
AMPS = {"device_class": "current", "unit_of_measurement": "A"}
WATTS = {"device_class": "power", "unit_of_measurement": "W"}
# Room on the breaker for both minimums (6 + 6 A, a 1.7 A house on 20 A)...
NO_ALLOWANCE_W = 40000.0
# ...and a block power allowance that leaves them 0.5 A per phase to spare,
# as Andrej's did at 19:18 (9.55 kW, a 1 kW house).
TIGHT_ALLOWANCE_W = (sum(HOUSE_A) + 3 * (2 * MIN_A + 0.5)) * V

# Every mode that runs both minimums when the site carries them. Strict does
# not, by design: the first car takes up to its maximum before the next gets
# anything (its own test is at the end).
MODES = [
    DISTRIBUTION_MODE_PRIORITY,
    DISTRIBUTION_MODE_SEQUENTIAL_OPTIMIZED,
    DISTRIBUTION_MODE_SHARED,
]


class Charger:
    """One go-eCharger and the car on it."""

    def __init__(self, hass, slug, meter_offset):
        self.hass = hass
        self.slug = slug
        self.meter_offset = meter_offset  # which cycle of the 30 s its meter value lands on
        self.plugged = True
        self.car_wants = True             # False: the car has stopped taking current
        self.limit = None                 # the last limit the charger accepted (A)
        self._held = 0.0
        self._pushed_status = None

    @property
    def draw(self):
        if self.status() != "Charging":
            return 0.0
        return self.limit

    def status(self):
        if not self.plugged:
            return "Available"
        if self.limit is None:
            return "Preparing"
        if self.limit < MIN_A:
            return "SuspendedEVSE"
        return "Charging" if self.car_wants else "SuspendedEV"

    def publish(self, cycle):
        set_state = self.hass.states.async_set
        set_state(f"switch.{self.slug}_charge_control", "on")
        status = self.status()
        if status in ("SuspendedEV", "SuspendedEVSE", "Available"):
            self._held = 0.0
        metered = (cycle - self.meter_offset) % METER_EVERY == 0
        if metered:
            self._held = round(self.draw, 2)
        # The integration moves the reading only with a meter value, and at
        # each meter value and status change re-writes every sensor - the
        # reading before the status.
        if metered or status != self._pushed_status:
            set_state(f"sensor.{self.slug}_current_import", str(self._held), AMPS)
            set_state(f"sensor.{self.slug}_status_connector", status)
            self._pushed_status = status


class World:
    def __init__(self, hass, allowance_w):
        self.hass = hass
        self.allowance_w = allowance_w
        # Their meter values land 2 s apart, as on the site (:18 and :20),
        # just before each charger's own command.
        self.chargers = {
            "stara": Charger(hass, "stara", meter_offset=13),
            "nova": Charger(hass, "nova", meter_offset=12),
        }
        self.cycle = 0
        self.commanded_at = {}     # charger -> the cycle of its last command
        self.extra_house = None    # optional script: extra house load (A) this cycle
        self.extra_a = 0.0

    def grid(self):
        draw = sum(c.draw for c in self.chargers.values())
        return [round(h + self.extra_a + draw, 3) for h in HOUSE_A]

    def publish(self, cycle):
        self.cycle = cycle
        if self.extra_house is not None:
            self.extra_a = self.extra_house(self)
        for charger in self.chargers.values():
            charger.publish(cycle)
        for phase, amps in zip("abc", self.grid()):
            self.hass.states.async_set(f"sensor.grid_{phase}", str(amps), AMPS)
        self.hass.states.async_set("sensor.grid_allowance", str(self.allowance_w), WATTS)

    async def accept(self, domain, service, data=None, *args, **kwargs):
        """The mocked service registry: each charger accepts every profile."""
        if domain == "ocpp" and service == "set_charge_rate":
            period = data["custom_profile"]["chargingSchedule"]["chargingSchedulePeriod"]
            self.chargers[data["devid"]].limit = float(period[0]["limit"])
            self.commanded_at[data["devid"]] = self.cycle


def _charger_entry(hub, slug, priority):
    return MockConfigEntry(
        domain=DOMAIN, version=2, minor_version=2, title=slug,
        data={
            CONF_ENTITY_ID: slug,
            CONF_NAME: slug,
            ENTRY_TYPE: ENTRY_TYPE_LOAD,
            CONF_CHARGER_ID: slug,
            CONF_OCPP_DEVICE_ID: slug,
            CONF_EVSE_CURRENT_IMPORT_ENTITY_ID: f"sensor.{slug}_current_import",
            CONF_HUB_ENTRY_ID: hub.entry_id,
        },
        options={
            CONF_PHASES: 3,
            CONF_CHARGER_L1_PHASE: "A",
            CONF_CHARGER_L2_PHASE: "B",
            CONF_CHARGER_L3_PHASE: "C",
            CONF_LOAD_PRIORITY: priority,
            CONF_EVSE_MINIMUM_CHARGE_CURRENT: MIN_A,
            CONF_EVSE_MAXIMUM_CHARGE_CURRENT: MAX_A,
            CONF_CHARGE_RATE_UNIT: "A",
            CONF_PROFILE_VALIDITY_MODE: "relative",
            CONF_UPDATE_FREQUENCY: 30,
            CONF_CHARGE_PAUSE_DURATION: 3,
        },
    )


def _site(hass, mode):
    hub = MockConfigEntry(
        domain=DOMAIN, version=2, minor_version=2, title="Hub",
        data={CONF_NAME: "Hub", CONF_ENTITY_ID: "hub", ENTRY_TYPE: ENTRY_TYPE_HUB},
        options={
            CONF_PHASE_A_CURRENT_ENTITY_ID: "sensor.grid_a",
            CONF_PHASE_B_CURRENT_ENTITY_ID: "sensor.grid_b",
            CONF_PHASE_C_CURRENT_ENTITY_ID: "sensor.grid_c",
            CONF_MAIN_BREAKER_RATING: BREAKER_A,
            CONF_PHASE_VOLTAGE: V,
            CONF_MAX_IMPORT_POWER_ENTITY_ID: "sensor.grid_allowance",
        },
    )
    stara = _charger_entry(hub, "stara", 1)
    nova = _charger_entry(hub, "nova", 2)
    hass.data[DOMAIN] = {
        "hubs": {
            hub.entry_id: {
                "entry": hub,
                "loads": [stara.entry_id, nova.entry_id],
                "distribution_mode": mode,
                "allow_grid_charging": True,
                "power_buffer": 0,
                "battery_soc_min": 20,
                "battery_soc_target": 50,
            }
        },
        "loads": {
            e.entry_id: {"entry": e, "hub_entry_id": hub.entry_id, "dynamic_control": True}
            for e in (stara, nova)
        },
    }
    processors = hass.data[DOMAIN].setdefault("load_processors", {}).setdefault(
        hub.entry_id, {}
    )
    for entry, slug in ((stara, "stara"), (nova, "nova")):
        processors[entry.entry_id] = sensor_platform.LoadJugglerDeviceSensor(
            hass, entry, hub, slug, slug
        )
    return SimpleNamespace(hub=hub, stara=stara, nova=nova)


@pytest.fixture(params=MODES)
def site(hass, request):
    return _site(hass, request.param)


async def _evening(hass, site, minutes_after_plug_in=15, script=None,
                   allowance_w=NO_ALLOWANCE_W):
    """The priority-1 car charges alone for three minutes, then the second car
    is plugged in. Returns one row per cycle from the plug-in on."""
    world = World(hass, allowance_w)
    world.chargers["nova"].plugged = False
    if script is not None:
        script(world)
    log = []
    with clocked(world.accept) as clock:
        for cycle in range(PLUG_IN + int(minutes_after_plug_in * 60 / CYCLE_S)):
            if cycle == PLUG_IN:
                world.chargers["nova"].plugged = True
            world.publish(cycle)
            await sensor_platform.async_run_hub_cycle(hass, site.hub)
            if world.chargers["nova"].plugged:
                log.append({
                    "t": clock.now,
                    "cycle": cycle,
                    "stara": world.chargers["stara"].limit,
                    "nova": world.chargers["nova"].limit,
                    "grid": max(world.grid()),
                    "total_w": sum(world.grid()) * V,
                })
            clock.now += CYCLE_S
    return log


def _stops(limits):
    """How many times a running charger was cut to below its minimum."""
    limits = [x for x in limits if x is not None]
    return sum(1 for a, b in zip(limits, limits[1:]) if a >= MIN_A and b < MIN_A)


@pytest.mark.parametrize("allowance_w", [NO_ALLOWANCE_W, TIGHT_ALLOWANCE_W])
async def test_the_second_charger_charges_through_beside_the_first(
    hass, site, allowance_w
):
    """Room for both minimums - on the breaker, and in a block allowance that
    leaves them 0.5 A per phase - so the priority-2 charger, once started, is
    never stopped."""
    log = await _evening(hass, site, allowance_w=allowance_w)
    nova = [row["nova"] for row in log]
    assert _stops(nova) == 0, f"Nova was stopped {_stops(nova)} times: {nova[::15]}"
    # It started within its first command interval or two and held at least
    # its minimum to the end.
    running = nova[45:]
    assert min(running) >= MIN_A, running[::15]


@pytest.mark.parametrize("allowance_w", [NO_ALLOWANCE_W, TIGHT_ALLOWANCE_W])
async def test_the_first_charger_settles_instead_of_swinging(hass, site, allowance_w):
    """Its car follows every command, so a steady site should give it a
    steady command: the room left beside the second car. In the field it
    swung 7 <-> 15 A every 30 s, and each swing up took the breaker past 20 A
    for as long as the old reading lasted."""
    log = await _evening(hass, site, allowance_w=allowance_w)
    tail = [row for row in log if row["t"] >= log[0]["t"] + 5 * 60]
    commands = [row["stara"] for row in tail]
    assert max(commands) - min(commands) <= 1.5, sorted(set(commands))
    over = max(row["grid"] for row in tail) - BREAKER_A
    assert over <= 0.5, f"the breaker was overrun by {over:.1f} A"
    over_w = max(row["total_w"] for row in tail) - allowance_w
    assert over_w <= 0.5 * 3 * V, f"the allowance was overrun by {over_w:.0f} W"


async def test_a_dip_between_two_commands_does_not_pause_the_second_charger(hass, site):
    """Once both cars run steadily, a burst of house load (9 A for 8 s, two
    cycles after the second charger's command) leaves no room for its 6 A
    minimum for a cycle or two - and is gone 20 s before its next command.
    The engine offers the minimum again by then, but the permit's smoothing
    climbs back to it over about 25 s, so that command found it a few tenths
    short and paused the car for 3 minutes. The pause is for a shortage the
    engine still sees when the command goes out, not for one already over."""
    state = {}

    def burst(world):
        nova_at = world.commanded_at.get("nova")
        if "from" not in state and nova_at is not None and world.cycle >= PLUG_IN + 120 and (
            world.cycle == nova_at + 2
        ):
            state["from"] = world.cycle
        start = state.get("from")
        return 9.0 if start is not None and start <= world.cycle < start + 4 else 0.0

    log = await _evening(hass, site, script=lambda world: setattr(world, "extra_house", burst))
    assert "from" in state, "the burst never ran"
    nova = [row["nova"] for row in log]
    assert _stops(nova) == 0, f"Nova was stopped {_stops(nova)} times: {nova[::15]}"


async def test_one_cycle_without_room_at_the_command_does_not_pause_it(hass, site):
    """The other half of the same rule: the engine's permit for the second
    charger is 0 on exactly one site cycle - here a house spike that takes
    the room for its minimum for that cycle alone - and that cycle is the one
    its command goes out on. One reading is not a shortage: the decision
    waits for the next cycle, which offers the minimum again, so the car
    runs on instead of pausing for 3 minutes."""
    state = {}

    def spike(world):
        nova_at = world.commanded_at.get("nova")
        if "at" not in state and nova_at is not None and world.cycle >= PLUG_IN + 120:
            state["at"] = nova_at + METER_EVERY   # its next command cycle
        return 25.0 if world.cycle == state.get("at") else 0.0

    log = await _evening(hass, site, script=lambda world: setattr(world, "extra_house", spike))
    assert "at" in state, "the spike never ran"
    nova = [row["nova"] for row in log]
    assert _stops(nova) == 0, f"Nova was stopped {_stops(nova)} times: {nova[::15]}"


@pytest.mark.parametrize("mode", [*MODES, DISTRIBUTION_MODE_SEQUENTIAL_STRICT])
async def test_a_car_that_stops_hands_its_room_to_the_second(hass, mode):
    """Andrej, 19:44:40: the priority-1 car stopped taking current
    (SuspendedEV) and kept its allocation; the second car was paused. In
    every mode - Strict included, where the second car waits for the first
    to finish - the second car must end up with the whole room, and once
    running it must not be stopped on the way."""
    site = _site(hass, mode)

    def stops_at_six_minutes(world):
        def script(w):
            w.chargers["stara"].car_wants = w.cycle < PLUG_IN + int(3 * 60 / CYCLE_S)
            return 0.0
        world.extra_house = script

    log = await _evening(hass, site, minutes_after_plug_in=8, script=stops_at_six_minutes)
    nova = [row["nova"] for row in log]
    assert _stops(nova) == 0, f"Nova was stopped {_stops(nova)} times: {nova[::15]}"
    end = [row["nova"] for row in log[-15:]]
    assert min(end) >= MAX_A - 1.0, end
