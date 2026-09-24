"""Off-grid with NO battery and nothing measuring the spare sun: probe for it.

Machine-authored tests - not yet human-reviewed.

A PV-only off-grid inverter produces only what is drawn, so its output - and a
solar sensor on it - reads house + our loads, and the sun it is not asked for
shows nowhere. Read through output sensors alone, or a solar sensor alone,
the pools can offer a load only what it already holds
(target_calculator._off_grid_unused_sun is 0 there by identity): a car at 0 A
never starts, however much sun is spare. Only a solar sensor read beside
output sensors measures the spare directly (72de109).

The rule these tests pin (engine/hub_calculation._apply_sun_probe): offer one
step - a load at 0 A its minimum, a running one its minimum more - and watch
the production. If it rises by the step, the sun had room: the loads keep it
and the next step follows. If it has not within the probe's window, the loads
fall back to what the production did follow and no step is offered again for
the load's own restart dwell (its charge pause) plus one command interval.
The window is the load's command interval + the draw settle time + the
household hold bridge (15 + 15 + 15 = 45 s here).

Everything runs the REAL site cycle - engine, load processor with its
smoothing, command-interval gate and charge pause, the OCPP command - against
a small off-grid world: one PV-only inverter with no battery whose output
follows demand up to what the sun (and its rating) give, a steady house, and a
1-phase car that asks for the limit its charger last accepted and gets what
the house leaves of the sun. The clock is simulated.
"""

import math
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
    CONF_INVERTER_OUTPUT_PHASE_B_ENTITY_ID,
    CONF_INVERTER_OUTPUT_PHASE_C_ENTITY_ID,
    CONF_INVERTER_SUPPORTS_ASYMMETRIC,
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
    EVSE_MODE_SOLAR_ONLY,
    EVSE_MODE_STANDARD,
    HOUSEHOLD_HOLD_BRIDGE_SECONDS,
    PERMIT_TAU_S,
    SETTLE_DRAW_SECONDS,
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
RATING_W = 12000.0        # far above the sun unless a test says otherwise
CYCLE_S = float(DEFAULT_SITE_UPDATE_FREQUENCY)
COMMAND_S = 15            # the charger's command interval
PAUSE_MIN = 3             # its charge pause
DEADBAND_W = DEAD_BAND * V
# The probe's window, built from the figures it stands on (see the module doc).
WINDOW_S = COMMAND_S + SETTLE_DRAW_SECONDS + HOUSEHOLD_HOLD_BRIDGE_SECONDS
# No step is offered again for the charge pause plus one command interval.
PAUSE_S = PAUSE_MIN * 60 + COMMAND_S
# How long a failed probe may leave the site asking the array for more than
# the sun. A failed START: the window, then the stop command's own send gate
# (a command interval) and the cycle it is decided on - 62 s. A failed GROWTH
# step is not stopped but brought back down through the charger's permit
# filter, whose PERMIT_TAU_S takes PERMIT_TAU_S * ln(step / DEAD_BAND) to come
# within the dead band of the cut - 21 s for the 6.3 A step - so 83 s.
FAILED_START_S = WINDOW_S + COMMAND_S + CYCLE_S
FAILED_STEP_S = FAILED_START_S + PERMIT_TAU_S * math.log((MIN_A + DEAD_BAND) / DEAD_BAND)

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
    """The PV-only off-grid site and the car behind the charger."""

    def __init__(self, hass, site):
        self.hass = hass
        self.site = site
        self.limit = None

    @property
    def asked(self):
        """What the car asks for (A): the accepted limit, if it can run on it."""
        if self.limit is None:
            return 0.0
        return self.limit if self.limit >= MIN_A else 0.0

    @property
    def draw(self):
        """What it gets: the array puts out no more than the sun and its
        rating give, and the house takes its share first. On the 3-phase
        site the car is on B, a leg of its own (a third of the rating), and
        the house on A."""
        supply = min(self.site.sun_w, self.site.rating_w)
        leg = self.site.rating_w / 3 / V if self.site.sensors == "output3" else math.inf
        return min(self.asked, leg, max(0.0, (supply - self.site.house_w) / V))

    @property
    def output_w(self):
        return self.site.house_w + self.draw * V

    @property
    def asked_w(self):
        """What the site asks of the array."""
        return self.site.house_w + self.asked * V

    def charger_status(self):
        if self.limit is None:
            return "Preparing"
        return "Charging" if self.limit >= MIN_A else "SuspendedEVSE"

    def publish(self):
        set_state = self.hass.states.async_set
        if self.site.sensors == "output3":
            for leg, amps in zip("abc", (self.site.house_w / V, self.draw, 0.0)):
                set_state(f"sensor.inverter_out_{leg}", str(round(amps, 3)),
                          {"device_class": "current", "unit_of_measurement": "A"})
        if self.site.sensors in ("output", "both"):
            set_state("sensor.inverter_out_a", str(round(self.output_w / V, 3)),
                      {"device_class": "current", "unit_of_measurement": "A"})
        if self.site.sensors in ("solar", "both"):
            # Alone, a solar sensor on a PV-only array reads its production -
            # what is drawn. Beside output sensors, 72de109's premise: it reads
            # the sun apart from the house.
            solar_w = self.output_w if self.site.sensors == "solar" else self.site.sun_w
            set_state("sensor.solar_production", str(round(solar_w, 1)),
                      {"device_class": "power", "unit_of_measurement": "W"})
        set_state("sensor.evse_status_connector", self.charger_status())
        set_state("switch.evse_charge_control", "on")
        set_state("sensor.evse_current_import", str(round(self.draw, 2)),
                  {"device_class": "current", "unit_of_measurement": "A"})

    async def accept(self, domain, service, data=None, *args, **kwargs):
        if domain == "ocpp" and service == "set_charge_rate":
            period = data["custom_profile"]["chargingSchedule"]["chargingSchedulePeriod"]
            self.limit = float(period[0]["limit"])


@pytest.fixture
def site(hass, request):
    sensors, mode, sun_w, house_w, rating_w = request.param
    options = {
        CONF_PHASE_VOLTAGE: V,
        CONF_MAIN_BREAKER_RATING: 40,
        # No grid CT and no battery entity at all.
        CONF_INVERTER_MAX_POWER: rating_w,
        CONF_WIRING_TOPOLOGY: WIRING_TOPOLOGY_SERIES,
    }
    if sensors in ("output", "both"):
        options[CONF_INVERTER_OUTPUT_PHASE_A_ENTITY_ID] = "sensor.inverter_out_a"
    if sensors == "output3":
        options.update({
            CONF_INVERTER_OUTPUT_PHASE_A_ENTITY_ID: "sensor.inverter_out_a",
            CONF_INVERTER_OUTPUT_PHASE_B_ENTITY_ID: "sensor.inverter_out_b",
            CONF_INVERTER_OUTPUT_PHASE_C_ENTITY_ID: "sensor.inverter_out_c",
            CONF_INVERTER_SUPPORTS_ASYMMETRIC: False,
        })
    if sensors in ("solar", "both"):
        options[CONF_SOLAR_PRODUCTION_ENTITY_ID] = "sensor.solar_production"
    hub_entry = MockConfigEntry(
        domain=DOMAIN, version=2, minor_version=2, title="Hub",
        data={CONF_NAME: "Hub", CONF_ENTITY_ID: "hub", ENTRY_TYPE: ENTRY_TYPE_HUB},
        options=options,
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
            CONF_CHARGER_L1_PHASE: "B" if sensors == "output3" else "A",
            CONF_LOAD_PRIORITY: 1,
            CONF_EVSE_MINIMUM_CHARGE_CURRENT: MIN_A,
            CONF_EVSE_MAXIMUM_CHARGE_CURRENT: 32,
            CONF_CHARGE_RATE_UNIT: "A",
            CONF_PROFILE_VALIDITY_MODE: "relative",
            CONF_UPDATE_FREQUENCY: COMMAND_S,
            CONF_CHARGE_PAUSE_DURATION: PAUSE_MIN,
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
        hub=hub_entry, evse=evse, sensors=sensors, mode=mode,
        sun_w=sun_w, house_w=house_w, rating_w=rating_w,
    )


async def _session(hass, site, minutes, sun_at=None):
    """``minutes`` of real site cycles against the world. ``sun_at(t)``, when
    given, sets the sun (W) at each cycle's time t. Returns one
    SimpleNamespace per cycle: t, sun_w, limit, draw, asked_w and probe (the
    hub's probe state, or None)."""
    world = World(hass, site)
    clock = _Clock(10_000.0)
    hass.data[DOMAIN].setdefault("load_processors", {}).setdefault(
        site.hub.entry_id, {}
    )[site.evse.entry_id] = sensor_platform.LoadJugglerDeviceSensor(
        hass, site.evse, site.hub, "evse", "evse"
    )
    hub_rt = hass.data[DOMAIN]["hubs"][site.hub.entry_id]
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
            t = clock.now - 10_000.0
            if sun_at is not None:
                site.sun_w = sun_at(t)
            world.publish()
            await sensor_platform.async_run_hub_cycle(hass, site.hub)
            log.append(SimpleNamespace(
                t=t,
                sun_w=site.sun_w,
                limit=world.limit or 0.0,
                draw=world.draw,
                asked_w=world.asked_w,
                probe=hub_rt.get("_sun_probe"),
            ))
            clock.now += CYCLE_S
    finally:
        for p in reversed(patches):
            p.stop()
    return log


def _starts(log):
    """Times the car was commanded from under its minimum to at least it."""
    before = [SimpleNamespace(limit=0.0)] + log
    return [
        cur.t for prev, cur in zip(before, log)
        if prev.limit < MIN_A <= cur.limit
    ]


def _stops(log):
    return [
        cur.t for prev, cur in zip(log, log[1:])
        if cur.limit < MIN_A <= prev.limit
    ]


def _longest_past_the_sun(log):
    """Longest continuous stretch (s) the site asked for more than the sun."""
    longest = run = 0.0
    for entry in log:
        past = entry.asked_w > min(entry.sun_w, RATING_W) + DEADBAND_W
        run = run + CYCLE_S if past else 0.0
        longest = max(longest, run)
    return longest


def _period(log):
    """The last probe period: one window and one pause."""
    return [e for e in log if e.t > log[-1].t - WINDOW_S - PAUSE_S]


@pytest.mark.parametrize(
    "site",
    [
        ("output", EVSE_MODE_SOLAR_ONLY.key, 4000.0, 1000.0, RATING_W),
        ("solar", EVSE_MODE_SOLAR_ONLY.key, 4000.0, 1000.0, RATING_W),
        ("output", EVSE_MODE_STANDARD.key, 4000.0, 1000.0, RATING_W),
        ("solar", EVSE_MODE_STANDARD.key, 4000.0, 1000.0, RATING_W),
    ],
    ids=["output-solar-only", "solar-solar-only", "output-standard", "solar-standard"],
    indirect=True,
)
async def test_spare_sun_starts_the_car_and_it_grows_into_it(hass, site):
    """4 kW of sun behind a 1 kW house, read through one sensor kind alone:
    the probe starts the car at its minimum on the first command, production
    follows, and it keeps charging and grows a step at a time into the
    13.0 A the sun leaves - drawing it through the whole last probe period and
    commanded it between probes. Only the growth step that finds the edge asks
    the array for more than the sun, for no longer than FAILED_STEP_S.

    Before the fix (measured on this rig): the car was never started - 0 A
    for all 10 minutes, 3 kW of sun unused."""
    log = await _session(hass, site, minutes=10)
    starts = _starts(log)
    assert starts, "the car was never started on 3 kW of spare sun"
    assert starts[0] <= COMMAND_S + CYCLE_S, f"first start at {starts[0]:.0f} s"
    assert not [t for t in _stops(log) if t > starts[0]], (
        f"production followed, yet the car was stopped at {_stops(log)} s"
    )
    target = (site.sun_w - site.house_w) / V
    last = _period(log)
    assert min(e.draw for e in last) == pytest.approx(target, abs=0.5), (
        f"drew as little as {min(e.draw for e in last):.1f} A where the sun "
        f"leaves {target:.1f} A"
    )
    assert min(e.limit for e in last) == pytest.approx(target, abs=1.0), (
        f"rested at {min(e.limit for e in last):.1f} A between probes"
    )
    past = _longest_past_the_sun(log)
    assert past <= FAILED_STEP_S, (
        f"the site asked for more than the sun for {past:.0f} s at a stretch"
    )


@pytest.mark.parametrize(
    "site",
    [
        ("output", EVSE_MODE_SOLAR_ONLY.key, 1500.0, 1500.0, RATING_W),
        ("solar", EVSE_MODE_SOLAR_ONLY.key, 1500.0, 1500.0, RATING_W),
        ("output", EVSE_MODE_STANDARD.key, 2000.0, 1500.0, RATING_W),
        ("solar", EVSE_MODE_STANDARD.key, 2000.0, 1500.0, RATING_W),
    ],
    ids=[
        "output-house-takes-the-sun", "solar-house-takes-the-sun",
        "output-under-the-minimum", "solar-under-the-minimum",
    ],
    indirect=True,
)
async def test_no_spare_sun_backs_off_and_waits_out_the_pause(hass, site):
    """No sun to spare (the house takes it all), or less than the car's
    minimum (500 W for a 1380 W minimum): the probe starts the car,
    production does not follow, it is stopped within FAILED_START_S, and it
    is not started again for the charge pause. Over 10 minutes: three probes.

    Before the fix (measured on this rig): no probe - the car was never
    started, so there was nothing to back off from."""
    log = await _session(hass, site, minutes=10)
    starts, stops = _starts(log), _stops(log)
    assert len(starts) >= 2, f"probed at {starts} s - expected a retry after the pause"
    for start in starts:
        stop = next((s for s in stops if s > start), None)
        assert stop is not None and stop - start <= FAILED_START_S, (
            f"started at {start:.0f} s, stopped at {stop} s"
        )
    gaps = [b - a for a, b in zip(starts, starts[1:])]
    assert min(gaps) >= PAUSE_MIN * 60, f"re-probed after {gaps} s"
    past = _longest_past_the_sun(log)
    assert past <= FAILED_START_S, (
        f"the site asked for more than the sun for {past:.0f} s at a stretch"
    )


@pytest.mark.parametrize(
    "site",
    [
        ("output", EVSE_MODE_SOLAR_ONLY.key, 1500.0, 1500.0, RATING_W),
        ("solar", EVSE_MODE_SOLAR_ONLY.key, 1500.0, 1500.0, RATING_W),
    ],
    ids=["output", "solar"],
    indirect=True,
)
async def test_the_sun_rising_after_a_failed_probe_starts_the_car_after_the_pause(
    hass, site
):
    """The house takes all the sun until 150 s, then the sun rises to 4 kW.
    The first probe fails and backs off; the first probe after the pause
    starts the car, production follows, and it keeps charging into the
    10.9 A the sun leaves the 1.5 kW house. (Before the fix: never started.)"""
    log = await _session(
        hass, site, minutes=10, sun_at=lambda t: 1500.0 if t < 150 else 4000.0
    )
    starts, stops = _starts(log), _stops(log)
    assert len(starts) == 2 and len(stops) == 1, (starts, stops)
    assert stops[0] - starts[0] <= FAILED_START_S
    # The retry: the failed window, the pause, then the start command's send
    # gate - each rounded up to a cycle.
    retry_s = WINDOW_S + PAUSE_S + COMMAND_S + 2 * CYCLE_S
    assert PAUSE_MIN * 60 <= starts[1] - starts[0] <= retry_s, starts
    target = (4000.0 - site.house_w) / V
    assert min(e.draw for e in _period(log)) == pytest.approx(target, abs=0.5)


@pytest.mark.parametrize(
    "site",
    [
        ("output", EVSE_MODE_SOLAR_ONLY.key, 4000.0, 1000.0, 3000.0),
        ("solar", EVSE_MODE_SOLAR_ONLY.key, 4000.0, 1000.0, 3000.0),
    ],
    ids=["output", "solar"],
    indirect=True,
)
async def test_the_probe_never_offers_past_the_inverters_rating(hass, site):
    """A 3 kW inverter under 4 kW of sun and a 1 kW house: every step is
    capped like the pools it rides in, at the rating less the house, so the
    car is never commanded past (3000 - 1000) / 230 = 8.7 A - not even for a
    window - and settles there. (Before the fix: 0 A, never started.)"""
    log = await _session(hass, site, minutes=10)
    cap = (site.rating_w - site.house_w) / V
    worst = max(e.limit for e in log)
    assert worst <= cap + DEAD_BAND, (
        f"commanded {worst:.1f} A past the rating's {cap:.1f} A"
    )
    assert log[-1].limit == pytest.approx(cap, abs=1.0)


@pytest.mark.parametrize(
    "site",
    [("output3", EVSE_MODE_SOLAR_ONLY.key, 9000.0, 2300.0, 9000.0)],
    ids=["symmetric-9kW"],
    indirect=True,
)
async def test_the_probe_never_offers_past_a_phase_leg(hass, site):
    """3-phase symmetric 9 kW inverter (3 kW legs) read through its per-phase
    outputs, 9 kW of sun, a 2.3 kW house on A and the car on B: the sun would
    leave it 6.7 kW, its leg carries 3 kW. Every step is capped at the leg,
    so the car is never commanded past 3000 / 230 = 13.0 A and settles there.
    (Before the fix: 0 A, never started.)"""
    log = await _session(hass, site, minutes=10)
    leg = site.rating_w / 3 / V
    worst = max(e.limit for e in log)
    assert worst <= leg + DEAD_BAND, f"commanded {worst:.1f} A past the {leg:.1f} A leg"
    assert log[-1].limit == pytest.approx(leg, abs=1.0)


@pytest.mark.parametrize(
    "site",
    [
        ("both", EVSE_MODE_SOLAR_ONLY.key, 4000.0, 1000.0, RATING_W),
        ("both", EVSE_MODE_STANDARD.key, 1500.0, 1500.0, RATING_W),
    ],
    ids=["spare", "house-takes-the-sun"],
    indirect=True,
)
async def test_a_solar_sensor_beside_output_sensors_needs_no_probe(hass, site):
    """With a solar sensor beside the output sensors the spare sun is measured
    (72de109), so no probe ever runs: the car settles at the 13.0 A the sun
    leaves exactly as before, and where the house takes all the sun it is
    never started at all."""
    log = await _session(hass, site, minutes=5)
    assert all(e.probe is None for e in log), "a probe ran where the spare is measured"
    target = max(0.0, (site.sun_w - site.house_w) / V)
    if target < MIN_A:
        assert not _starts(log)
    else:
        assert log[-1].limit == pytest.approx(target, abs=1.0)
