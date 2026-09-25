"""Closed-loop dynamics of the control loop, at a thousand times wall clock.

The Docker rig in ``dev/ha-test`` answers "does this work on a real Home
Assistant". This answers a narrower question much faster: given a site whose
production is MOVING, does the loop track it, and does it ring at a fixed
operating point? A 300 s scenario at a 1 s cadence is 300 iterations, so a
sweep that costs 40 minutes of wall clock on the rig runs here in seconds.

That speed is the point. Tuning a control loop from single runs is how you end
up preferring an unstable configuration: on 2026-09-08 a shorter permit filter
scored a BETTER mean tracking error (501 W against 608 W) while oscillating
600 W peak-to-peak on a dead-flat input, because mean-|error| rewards a ring
centred on the right answer over an honest lag. Only a fixed-point ring test
caught it, and at seven minutes a run there was no way to repeat it enough to
separate signal from scatter.

WHAT IS REAL HERE AND WHAT IS NOT
---------------------------------
Real, imported rather than reimplemented:
  * ``run_hub_calculation``        - the whole site cycle (``Engine``): the
                                     input EMAs on the grid phases AND on the
                                     managed draw, each advanced once, the
                                     feedback subtraction, the Excess verdict
                                     and its latch, the allocator
  * ``apply_smoothing``            - EMA, Schmitt trigger, adaptive rate limit,
                                     fed the permit rounded as the load
                                     processor rounds it
  * ``_build_power_station_load``  - the station, built from its config entry
                                     and the entities the plant publishes, so
                                     what its draw is BOOKED at is production's
                                     choice: its commanded speed when it has no
                                     AC output sensor, as on the rig
  * ``send_power_station_command`` - the station's register and reserve
                                     writes, behind the load processor's
                                     command gate (CONF_UPDATE_FREQUENCY)

Modelled, mirroring ``dev/ha-test`` rather than Home Assistant:
  * the site's physics, including inverter curtailment to an export limit
  * the CT measurement delay - by default the rig's: sampled on a 5 s tick,
    each sample publishing the previous one
  * the station's converter ramping toward its register on that tick
  * the load processor's command gate (one comparison, entities/load.py)
  * the tank, a ``LoadContext`` the plant builds itself

Until 2026-09-24 the first line was a hand-assembled copy - ``_smooth`` on the
grid keys, then the managed draws subtracted RAW - while production smoothed
the draw on the grid's EMA (201e2bb), and from 2.1.1 advanced that EMA twice a
cycle (fixed in 4cbbdd1). The copy could model neither. Going through the real
cycle, ``RampSim`` reproduces that bug's overshoot to the watt when it is put
back, and dev/tests/test_dynamics_harness.py keeps it that way.

VALIDATED, not assumed - but see the note after this. On first run
(2026-09-08) the PERMIT_TAU_S sweep reproduced the rig's own conclusion from
that day: a sustained ~1 kW ring at every tau up to 4 s, decaying at 5.6 s,
fully settled at 8 s. The rig reached that over 40 minutes of wall clock and
two noisy columns; this reaches it in 0.8 s. It also reproduces the trap -
mean tracking error is BEST in the oscillating region (264 W at tau 1.0
against 289 W at 5.6), while curtailed energy, which is the quantity that
actually costs anything, is best where the ring is zero. Trust the ORDERING
here, not the absolute figures: the moving error runs about half the rig's,
because the omissions below all cost real watts.

Those figures came off the RAW-draw copy. With production's smoothed draw the
same sweep (2026-09-24) rings only at tau 1.0 (300 W) and is flat from 1.5 s
up, while tracking error and curtailment rise with tau - 330 W / 30 W at 1.5
against 469 W / 82 W at 8.0 - so the agreement with the rig's ring is no
longer reproduced here, and the ordering that chose PERMIT_TAU_S = 7.0 does
not come out of this model any more.

The rig re-measurement (8151a69) said why: its station has no AC output
sensor, so production books it at its COMMANDED speed while the CTs see the
draw ramping toward it, and its instruments and its register move on a 5 s
clock rather than every cycle. With all of that in (``Sim``; 2026-09-24) the
sweep reproduces the rig's table: 290 W of ring at tau 1.0 against the rig's
300, some tick phases ringing up to 5 s, none at 7 s, tracking error 5-85 W
under the rig's and curtailment within 15 W of it. Each part is needed: the
command interval alone damps the old 300 W ring away; the booked command rings
not at all without the rig's clock, and with only the converter on it (the CTs
a pure delay) in 2 of 5 phases and about a third as hard; and a metered
station on the same clock never rings.

NOT modelled at all, and this is the limit of the tool: the rest of the Home
Assistant path. The charge pause, entity restore across restarts, template
staleness, the grace window, the EVSE builder (settle detection, the
stuck-readout watch). Every one of those has produced a real bug in this
project, and none of them would show up here. Screen candidates with this;
confirm the winner on the rig.

    python3 dev/tests/dynamics.py                 # the standard comparison
    python3 dev/tests/dynamics.py --plot out.html # and a chart to eyeball
"""

import math
import sys
from collections import deque
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
from standalone_loader import load_pure_modules

# "hub_calculation" pulls the whole engine chain, which is what makes
# readers importable - it reaches forecast_reader, which needs the
# calculations package __init__ executed. Same combination the
# availability-contract tests use.
load_pure_modules(
    engine_modules=("hub_calculation",),
    control_modules=("smoothing", "power_station"),
)

from custom_components.dynamic_ocpp_evse.calculations.models import (  # noqa: E402
    LoadContext,
)
from custom_components.dynamic_ocpp_evse.const import (  # noqa: E402
    CONF_CONNECTED_TO_PHASE,
    CONF_DEVICE_TYPE,
    CONF_ENTITY_ID,
    CONF_EXCESS_HYSTERESIS,
    CONF_EXCESS_TRIGGER_MARGIN,
    CONF_GRID_EXPORT_LIMIT,
    CONF_LOAD_PRIORITY,
    CONF_MAIN_BREAKER_RATING,
    CONF_PHASE_A_CURRENT_ENTITY_ID,
    CONF_PHASE_B_CURRENT_ENTITY_ID,
    CONF_PHASE_C_CURRENT_ENTITY_ID,
    CONF_PHASE_VOLTAGE,
    CONF_SITE_UPDATE_FREQUENCY,
    CONF_STATION_AC_INPUT_ENTITY_ID,
    CONF_STATION_AC_OUTPUT_ENTITY_ID,
    CONF_STATION_BATTERY_LEVEL_ENTITY_ID,
    CONF_STATION_CHARGE_SPEED_ENTITY_ID,
    CONF_STATION_MAX_CHARGE_POWER,
    CONF_STATION_MIN_CHARGE_POWER,
    CONF_STATION_RESERVE_ENTITY_ID,
    CONF_UPDATE_FREQUENCY,
    DEFAULT_UPDATE_FREQUENCY,
    DEVICE_TYPE_POWER_STATION,
    DOMAIN,
)
from custom_components.dynamic_ocpp_evse.control.power_station import (  # noqa: E402
    send_power_station_command,
)
from custom_components.dynamic_ocpp_evse.control.smoothing import (  # noqa: E402
    apply_smoothing,
)
from custom_components.dynamic_ocpp_evse.engine import (  # noqa: E402
    hub_calculation,
)
from custom_components.dynamic_ocpp_evse.helpers import (  # noqa: E402
    get_entry_value,
)

V = 230.0


# -- the engine -----------------------------------------------------------
HUB_ID = "dynamics_hub"
GRID_CTS = (
    (CONF_PHASE_A_CURRENT_ENTITY_ID, "sensor.dynamics_grid_a"),
    (CONF_PHASE_B_CURRENT_ENTITY_ID, "sensor.dynamics_grid_b"),
    (CONF_PHASE_C_CURRENT_ENTITY_ID, "sensor.dynamics_grid_c"),
)


class _Reading:
    """A Home Assistant state, as far as the readers look at one."""

    __slots__ = ("state", "attributes")

    def __init__(self, value, unit):
        # repr round-trips a float exactly, so publishing a reading costs it
        # no precision the plant had.
        self.state = repr(float(value))
        self.attributes = {"unit_of_measurement": unit}


class Engine:
    """The production site cycle, ``run_hub_calculation``, fed by a plant.

    Called once per cycle, it keeps everything the engine carries between
    cycles - every input EMA, the Excess latch - where production keeps it, in
    the hub's runtime bucket, and advances it exactly as often as production
    does. That is why the harness goes through the whole cycle instead of
    calling its parts in an order of its own. Until 2026-09-24 it did the
    latter: it smoothed the grid phases itself and subtracted RAW managed
    draws, while production smoothed the draw on the grid's EMA - and, from
    2.1.1 until 4cbbdd1, advanced that EMA twice a cycle, which let a charger
    ramping on a breaker-limited phase be permitted 414 W past its allowance.
    A harness wiring the pieces together itself can model neither the design
    nor a bug in how the cycle wires it; this one exercises both
    (dev/tests/test_dynamics_harness.py).

    Three things stand in for Home Assistant, and nothing else does:
      * the grid CTs and the devices' own entities are states in a dict, read
        through the real readers;
      * a ``number.set_value`` call just lands the value in that dict, which is
        all a register write is to the device on the other side;
      * a load is EITHER the plant's own ``LoadContext``, handed to the cycle
        where ``_add_loads_to_site`` would have built it, OR a config entry
        that the real ``_add_loads_to_site`` builds from those states - what a
        load's draw is booked at is then production's decision, not the
        plant's. The station is the second kind (see ``Sim``).
    """

    def __init__(self, *, site_freq, phases=3, breaker_a=25.0,
                 export_limit_w=0.0, trigger_margin_w=500.0, hysteresis_w=0.0):
        options = {
            CONF_PHASE_VOLTAGE: V,
            CONF_MAIN_BREAKER_RATING: breaker_a,
            CONF_SITE_UPDATE_FREQUENCY: site_freq,
            CONF_GRID_EXPORT_LIMIT: export_limit_w,
            CONF_EXCESS_TRIGGER_MARGIN: trigger_margin_w,
            # Explicit, because the hub's default band is 500 W, not none.
            CONF_EXCESS_HYSTERESIS: hysteresis_w,
        }
        self._cts = [entity for _, entity in GRID_CTS[:phases]]
        options.update(GRID_CTS[:phases])
        # One entry for the engine AND the permit pipeline, as in production:
        # apply_smoothing reads its cadence and dials off the same hub entry.
        self.hub_entry = SimpleNamespace(entry_id=HUB_ID, data={}, options=options)
        self.runtime = {}
        self._states = {}
        self.hass = SimpleNamespace(
            states=self._states,
            data={DOMAIN: {"hubs": {HUB_ID: self.runtime}, "loads": {}}},
            # No inverter or circuit-group entries: the plant has none.
            config_entries=SimpleNamespace(async_entries=lambda domain=None: []),
            services=SimpleNamespace(async_call=self._set_value),
        )

    def publish(self, entity, value, unit):
        """Put a reading where the readers look for it."""
        self._states[entity] = _Reading(value, unit)

    async def _set_value(self, domain, service, data, blocking=False):
        """``number.set_value``, the only service the command modules call
        here. The register keeps its unit; the plant reads it back."""
        entity = data["entity_id"]
        self.publish(entity, data["value"],
                     self._states[entity].attributes["unit_of_measurement"])

    def cycle(self, grid_a, loads, entries=()):
        """One site cycle. ``grid_a``: what each CT reads (A, + import).

        ``loads``: the plant's own ``LoadContext``s. ``entries``: load config
        entries for the real ``_add_loads_to_site`` to build."""
        for entity, amps in zip(self._cts, grid_a):
            self.publish(entity, amps, "A")

        builders = hub_calculation._add_loads_to_site

        def plant_loads(hass, site, hub_entry_id, load_entries=None, **kw):
            site.loads.extend(loads)
            builders(hass, site, hub_entry_id, list(entries), **kw)

        # Swapped for the one call, so nothing else sharing the module (the
        # pytest tier imports the real package) ever sees the stand-in.
        hub_calculation._add_loads_to_site = plant_loads
        try:
            return hub_calculation.run_hub_calculation(self.hass, self.hub_entry)
        finally:
            hub_calculation._add_loads_to_site = builders


def _complete(coro):
    """Run a command coroutine whose only await is the plant's own service
    call, which never suspends - so it finishes on the first step, and needs
    no event loop (the pytest tier may already own one)."""
    try:
        coro.send(None)
    except StopIteration:
        return
    coro.close()
    raise RuntimeError("the command awaited something the plant does not provide")


def permit_a(result, load_id):
    """The permit the load processor hands ``apply_smoothing``, rounded as
    entities/load.py rounds it."""
    return round(result["load_available"][load_id], 1)


def _permit_state(name):
    """What ``apply_smoothing`` keeps on the load's sensor entity. It only ever
    touches these five attributes, so a namespace is the whole contract."""
    return SimpleNamespace(
        _attr_name=name,
        _ema_current=None,
        _schmitt_current=None,
        _schmitt_state="rising",
        _rate_limited_current=0.0,
    )


STATION_SPEED = "number.dynamics_station_charge_speed"
STATION_RESERVE = "number.dynamics_station_reserve"
STATION_BATTERY = "sensor.dynamics_station_battery"
STATION_AC_IN = "sensor.dynamics_station_ac_input"
STATION_AC_OUT = "sensor.dynamics_station_ac_output"


class Sim:
    """One site, stepped a cycle at a time.

    Defaults mirror ``dev/ha-test/moving_surplus.py`` exactly, so a number out
    of here is comparable in shape to one off the rig. Changing a default here
    without changing it there silently ends that.

    The station is a config entry, built each cycle by production's own
    ``_build_power_station_load`` from the entities the plant publishes, and
    driven by production's own ``send_power_station_command`` - so what its
    draw is booked at, and when its register moves, are production's answers:

      * ``station_metered=False`` (the rig's station,
        dev/ha-test/config/packages/station.yaml): an AC input sensor and no
        AC output sensor.
        The builder can only measure ``ac_input - ac_output`` with both, so it
        books the station at the speed REGISTER while the station was last
        told to charge - its command, not its draw. The CTs see the draw that
        is ramping toward that register. The same shape as a charger whose
        readout is judged stuck (blind mode), which is controlled on its
        command too.
      * ``station_metered=True``: both sensors, so it is booked at what its
        AC input shows - live on the continuous clock (what this harness
        modelled for every station until 2026-09-24), a tick behind on the
        rig's.
      * ``update_frequency``: the station entry's CONF_UPDATE_FREQUENCY, the
        load processor's command gate (entities/load.py): a command goes out
        once that long has passed since the last one, and the register moves
        only then. 5 s is the rig's setting (dev/ha-test/README.md); None
        leaves it unset, so production's DEFAULT_UPDATE_FREQUENCY applies,
        resolved as the processor resolves it.
      * ``tick_s``: the rig's instruments run on their own 5 s clock
        (``time_pattern /5``): every tick the CTs and the station's AC input
        take a sample and publish the previous one (5-10 s behind), and the
        station's converter closes ``device_ramp`` of the gap to its register.
        So a register write reaches the draw 0-5 s later, depending on where
        the tick falls against the command gate - ``tick_phase_s``, which a
        rig restart lands at random, and which decides how far the booked
        command runs ahead of what the CTs can see. None is the model until
        2026-09-24: CTs a pure ``ct_lag_s`` delay, the AC input live, the
        converter stepping every cycle.
    """

    def __init__(
        self,
        *,
        site_freq=1.0,
        ct_lag_s=7.0,
        device_ramp=1.0,
        household_phase_w=300.0,
        export_limit_w=11000.0,
        trigger_margin_w=500.0,
        hysteresis_w=0.0,
        tank_w=2000.0,
        station_min_w=200.0,
        station_max_w=2400.0,
        station_metered=False,
        update_frequency=5.0,
        tick_s=5.0,
        tick_phase_s=2.0,
        curtail=True,
    ):
        self.dt = float(site_freq)
        self.device_ramp = device_ramp
        self.hh_w = household_phase_w
        self.export_limit_w = export_limit_w
        self.threshold_w = export_limit_w - trigger_margin_w
        self.tank_w = tank_w
        self.station_min_w = station_min_w
        self.station_max_w = station_max_w
        self.curtail = curtail

        # The measurement delay, as whole cycles. A real CT chain is a filter
        # rather than a pure delay, but the readers' EMA downstream supplies
        # the filtering; what this stands in for is the transport lag, which is
        # what actually costs the loop its phase margin.
        self.lag = deque([(0.0, 0.0, 0.0)] * max(1, round(ct_lag_s / self.dt)))
        # Or the rig's sampled instruments: (CTs, AC input), taken and shown.
        self.tick_s = tick_s
        self.tick_phase_s = tick_phase_s
        self.sampled = self.shown = ((0.0, 0.0, 0.0), 0.0)

        self.engine = Engine(
            site_freq=self.dt,
            export_limit_w=export_limit_w,
            trigger_margin_w=trigger_margin_w,
            hysteresis_w=hysteresis_w,
        )
        self.hub_entry = self.engine.hub_entry
        self.station_draw_w = 0.0
        self.commanded_w = 0.0
        self.now = 0.0

        # The station as the rig configures it: speed and reserve registers,
        # its battery level, its AC input - and its AC output only if metered.
        data = {
            CONF_DEVICE_TYPE: DEVICE_TYPE_POWER_STATION,
            CONF_ENTITY_ID: "station",
            CONF_LOAD_PRIORITY: 2,
            CONF_CONNECTED_TO_PHASE: "C",
            CONF_STATION_MIN_CHARGE_POWER: station_min_w,
            CONF_STATION_MAX_CHARGE_POWER: station_max_w,
            CONF_STATION_CHARGE_SPEED_ENTITY_ID: STATION_SPEED,
            CONF_STATION_RESERVE_ENTITY_ID: STATION_RESERVE,
            CONF_STATION_BATTERY_LEVEL_ENTITY_ID: STATION_BATTERY,
            CONF_STATION_AC_INPUT_ENTITY_ID: STATION_AC_IN,
        }
        if station_metered:
            data[CONF_STATION_AC_OUTPUT_ENTITY_ID] = STATION_AC_OUT
        if update_frequency is not None:
            data[CONF_UPDATE_FREQUENCY] = update_frequency
        self.station = SimpleNamespace(entry_id="station", data=data, options={})
        self.metered = station_metered
        # Half full, as the rig's station starts: below every reserve the
        # command module writes while charging, above the one it drops to.
        self.station_soc = 50.0
        self.engine.hass.data[DOMAIN]["loads"]["station"] = {}
        self.engine.publish(STATION_SPEED, 0.0, "W")
        self.engine.publish(STATION_RESERVE, 0.0, "%")
        self.engine.publish(STATION_BATTERY, self.station_soc, "%")

        # What the load processor keeps between cycles, as far as
        # apply_smoothing and send_power_station_command look at it.
        self.sensor = _permit_state("station")
        self.sensor.config_entry = self.station
        self.sensor.hass = self.engine.hass
        self.sensor._last_command_time = -math.inf

    def _register(self, entity):
        return float(self.engine.hass.states[entity].state)

    # -- loads ------------------------------------------------------------
    def _loads(self):
        """The plant's own loads; the station is built from its entry."""
        tank_a = self.tank_w / V
        return [
            LoadContext(
                load_id="tank",
                entity_id="tank",
                min_current=tank_a,
                max_current=tank_a,
                phases=1,
                priority=1,
                device_type="hot_water_tank",
                operating_mode="Normal",
                mode_behavior="full_power",
                mode_priority=1,
                active_phases_mask="B",
                l1_phase="B",
                l1_current=tank_a,
                rated_current=tank_a,
            ),
        ]

    # -- physics ----------------------------------------------------------
    def ideal_station_w(self, solar_w):
        """What the station should be permitted, from first principles."""
        left = solar_w - 3 * self.hh_w - self.tank_w - self.threshold_w
        return 0.0 if left < self.station_min_w else min(left, self.station_max_w)

    def _site(self, solar_w):
        """The true per-phase grid position (A, + import) and the curtailment."""
        managed = (0.0, self.tank_w, self.station_draw_w)
        demand_w = 3 * self.hh_w + sum(managed)

        potential_w = solar_w
        delivered_w = (
            min(potential_w, max(0.0, demand_w + self.export_limit_w))
            if self.curtail
            else potential_w
        )
        curtailed_w = max(0.0, potential_w - delivered_w)
        inv_phase_a = delivered_w / 3.0 / V
        return tuple((self.hh_w + m) / V - inv_phase_a for m in managed), curtailed_w

    def _ramp(self):
        self.station_draw_w += (self.commanded_w - self.station_draw_w) * self.device_ramp

    def _ticks(self, t):
        """How many instrument ticks fall in this cycle, [t, t + dt)."""
        def before(x):
            return math.ceil((x - self.tick_phase_s) / self.tick_s - 1e-9)
        return before(t + self.dt) - before(t)

    def step(self, solar_w):
        true, curtailed_w = self._site(solar_w)

        # The CTs report the site as it was a while ago, the station's AC input
        # its draw. From here the whole site cycle - the station's builder,
        # input EMAs, the managed-draw subtraction, the Excess latch, the
        # allocator - is production's.
        if self.tick_s is None:
            self.lag.append(true)
            cts, ac_in = self.lag.popleft(), self.station_draw_w
        else:
            cts, ac_in = self.shown
        self.engine.publish(STATION_AC_IN, ac_in, "W")
        if self.metered:
            self.engine.publish(STATION_AC_OUT, 0.0, "W")
        result = self.engine.cycle(cts, self._loads(), [self.station])
        margin = result["excess_margin_power"]

        permit = apply_smoothing(
            self.sensor, permit_a(result, "station"), False, self.hub_entry
        )
        permit_w = permit * V

        # The load processor's command gate (entities/load.py), then its
        # command: the limit rounded as it rounds it, quantised, written and
        # the reserve set by production's command module.
        interval = get_entry_value(
            self.station, CONF_UPDATE_FREQUENCY, DEFAULT_UPDATE_FREQUENCY
        )
        t = self.now
        if t - self.sensor._last_command_time >= interval:
            _complete(send_power_station_command(
                self.sensor, round(permit, 1), self.hub_entry, t
            ))
        self.now += self.dt

        # The device: it charges toward its speed register while its reserve
        # is above its battery level (the reserve is the on/off gate), closing
        # a fraction of the remaining gap each cycle - or, on the rig's clock,
        # at each tick, when its instruments take their sample first.
        charging = self._register(STATION_RESERVE) > self.station_soc
        self.commanded_w = self._register(STATION_SPEED) if charging else 0.0
        if self.tick_s is None:
            self._ramp()
        else:
            for _ in range(self._ticks(t)):
                self.shown, self.sampled = (
                    self.sampled, (self._site(solar_w)[0], self.station_draw_w)
                )
                self._ramp()

        export_w = -sum(true) * V
        return {
            "solar": solar_w,
            "ideal": self.ideal_station_w(solar_w),
            "permit": permit_w,
            "reg": self.commanded_w,
            "draw": self.station_draw_w,
            "managed": self.tank_w + self.station_draw_w,
            "export": export_w,
            "curtailed": curtailed_w,
            "margin": margin,
        }


class RampSim:
    """A charger ramping up on a breaker-limited phase, one cycle at a time.

    One phase, a 25 A breaker and 8 A of household that never moves, so the
    right permit is the 17 A left over and it never moves either. The car
    plugs in once every input EMA has settled on the household alone, then
    slews its draw toward its last command; the CT reads household plus that
    draw and the charger reports the draw, both on the same cycle. Whatever the
    permit reaches above 17 A is the reconstruction (grid - managed draw)
    reading the household low while the car ramps - which is what a draw
    filter running ahead of the grid filter does, and what 4cbbdd1 fixed.

    The same loop dev/tests/test_managed_draw_smoothing.py closes through Home
    Assistant, with the same numbers, so the two tiers can be held against
    each other.
    """

    def __init__(self, *, car_ramp_a_s=1.0, site_freq=2.0, breaker_a=25.0,
                 household_a=8.0, plug_in_cycle=30):
        self.dt = float(site_freq)
        self.car_step_a = car_ramp_a_s * self.dt
        self.breaker_a = breaker_a
        self.household_a = household_a
        self.allowance_a = breaker_a - household_a
        self.plug_in_cycle = plug_in_cycle
        self.engine = Engine(site_freq=self.dt, phases=1, breaker_a=breaker_a)
        self.hub_entry = self.engine.hub_entry
        self.sensor = _permit_state("evse")
        self.cycle = 0
        self.draw_a = 0.0
        self.command_a = 0.0

    def _load(self, plugged):
        """A 1-phase 6-32 A Standard EVSE: the breaker binds, not the car."""
        return LoadContext(
            load_id="evse",
            entity_id="evse",
            min_current=6.0,
            max_current=32.0,
            rated_current=32.0,
            phases=1,
            l1_phase="A",
            l1_current=self.draw_a,
            connector_status="Charging" if plugged else "Available",
        )

    def step(self):
        plugged = self.cycle >= self.plug_in_cycle
        if plugged:
            gap = self.command_a - self.draw_a
            self.draw_a += max(-self.car_step_a, min(self.car_step_a, gap))
        result = self.engine.cycle(
            (self.household_a + self.draw_a,), [self._load(plugged)]
        )
        permit = permit_a(result, "evse")
        self.command_a = apply_smoothing(self.sensor, permit, False, self.hub_entry)
        row = {
            "t": self.cycle * self.dt,
            "draw": self.draw_a,
            "permit": permit,
            "command": self.command_a,
            "import": self.household_a + self.draw_a,
        }
        self.cycle += 1
        return row


def ramp(car_ramp_a_s=1.0, cycles=120, **kw):
    """Run a RampSim; return its rows and the two overshoots, in W.

    ``permit_over``: the most the permit went past the allowance. ``site_over``:
    the most the site's import went past the breaker - the physical cost.
    """
    sim = RampSim(car_ramp_a_s=car_ramp_a_s, **kw)
    rows = [sim.step() for _ in range(cycles)]

    def over(key, limit_a):
        return max(0.0, max(r[key] for r in rows) - limit_a) * V

    return {
        "rows": rows,
        "permit_over": over("permit", sim.allowance_a),
        "site_over": over("import", sim.breaker_a),
        "final_draw": rows[-1]["draw"],
        "allowance": sim.allowance_a,
    }


# -- drivers --------------------------------------------------------------
def sine(mean_w=14700.0, amplitude_w=1100.0, period_s=150.0, periods=2, dt=1.0):
    """A slow swell in production.

    Refuses to run when ``dt`` cannot resolve ``period_s``, because the metrics
    do not fail loudly when it cannot - they just get quieter. The default
    150 s period sampled every 60 s is five points per cycle, and on
    2026-09-08 that reported ZERO curtailment at a 60 s refresh, which read as
    the best result in the table; measured against a period it could actually
    resolve, the same cadence curtails 109 W. Twelve samples per cycle is not a
    rigorous bound, just far enough from Nyquist that a peak cannot hide
    between two samples.
    """
    per_cycle = period_s / dt
    if per_cycle < 12:
        raise ValueError(
            f"{per_cycle:.0f} samples per cycle: a {period_s:.0f} s period at a "
            f"{dt:.0f} s cadence is aliased, and the metrics will flatter it. "
            f"Raise period_s to at least {dt * 12:.0f} s."
        )
    for i in range(int(period_s * periods / dt)):
        yield mean_w + amplitude_w * math.sin(2 * math.pi * (i * dt) / period_s)


def hold(value_w, seconds, dt=1.0):
    for _ in range(int(seconds / dt)):
        yield value_w


def step(low_w=13600.0, high_w=15800.0, low_s=60.0, high_s=180.0, dt=1.0):
    """A production step, which is what a cloud edge actually looks like.

    The sinusoid measures tracking; this measures how fast the loop can move at
    all, which is the quantity the rate limiter governs and the one a mean error
    over a whole cycle hides.
    """
    for _ in range(int(low_s / dt)):
        yield low_w
    for _ in range(int(high_s / dt)):
        yield high_w


def rise_time_s(rows, key="reg", frac=0.9):
    """Seconds from the step to ``frac`` of the eventual value.

    Measured from where the input moves rather than from t=0, and against the
    value the run actually reaches, so a run that never gets there reports None
    instead of flattering itself.
    """
    solars = [r["solar"] for r in rows]
    step_i = next((i for i in range(1, len(solars)) if solars[i] > solars[i - 1] + 1), None)
    if step_i is None:
        return None
    after = rows[step_i:]
    start = after[0][key]
    final = max(r[key] for r in after)
    if final <= start:
        return None
    want = start + (final - start) * frac
    hit = next((r for r in after if r[key] >= want), None)
    return None if hit is None else hit["t"] - after[0]["t"]


def binding_share(rows, dt, ramp_up_rate=None):
    """Share of RISING cycles whose step exceeded the fixed floor.

    The floor is ``RAMP_UP_RATE * site_freq``; a step bigger than that can only
    have come from the proportional term. This is the mechanism the 2026-09-08
    finding was about, asserted directly rather than inferred from an average:
    if the proportional term never binds, the adaptive rate is decoration.
    """
    from custom_components.dynamic_ocpp_evse.const import RAMP_UP_RATE

    floor_w = (ramp_up_rate if ramp_up_rate is not None else RAMP_UP_RATE) * dt * V
    rising = [
        (b["permit"] - a["permit"])
        for a, b in zip(rows, rows[1:])
        if b["permit"] > a["permit"] + 1e-9
    ]
    if not rising:
        return 0.0
    return sum(1 for d in rising if d > floor_w + 1e-6) / len(rising)
def run(sim, driver, warmup_s=120.0, warmup_w=14700.0):
    """Settle the loop, then record. The warmup is discarded, as on the rig."""
    for w in hold(warmup_w, warmup_s, sim.dt):
        sim.step(w)
    rows = []
    for t_i, w in enumerate(driver):
        r = sim.step(w)
        r["t"] = t_i * sim.dt
        rows.append(r)
    return rows


# -- metrics --------------------------------------------------------------
def report(rows, label=""):
    errs = [abs(r["permit"] - r["ideal"]) for r in rows]
    lost = [r["curtailed"] for r in rows]
    exp = [r["export"] for r in rows]
    regs = [r["reg"] for r in rows]
    writes = sum(1 for a, b in zip(regs, regs[1:]) if a != b)
    mins = (rows[-1]["t"] - rows[0]["t"]) / 60.0 or 1.0
    out = {
        "label": label,
        "err_mean": sum(errs) / len(errs),
        "err_max": max(errs),
        "curtailed_mean": sum(lost) / len(lost),
        "curtailed_max": max(lost),
        "export_mean": sum(exp) / len(exp),
        "export_min": min(exp),
        "export_max": max(exp),
        "writes_per_min": writes / mins,
    }
    return out


def ring(rows, window_s=30.0):
    """Peak-to-peak per window, so decaying / sustained / growing is legible."""
    out = []
    t0 = rows[0]["t"]
    span = rows[-1]["t"] - t0
    lo = 0.0
    while lo < span:
        w = [r for r in rows if lo <= r["t"] - t0 < lo + window_s]
        if len(w) >= 3:
            out.append(
                (
                    lo,
                    max(r["reg"] for r in w) - min(r["reg"] for r in w),
                    max(r["export"] for r in w) - min(r["export"] for r in w),
                )
            )
        lo += window_s
    return out


# -- plotting -------------------------------------------------------------
# Deliberately hand-rolled SVG. Nothing under dev/ has a third-party
# dependency - the pure tier's whole premise is that it runs on a machine with
# nothing installed - and a chart is not worth breaking that for. The palette
# and dark ground match Home Assistant's history card so a run here can be held
# up against "The last hour" on the rig dashboard without re-reading the
# colours.
SERIES = [
    ("solar", "#f5c518", "Solar"),
    ("managed", "#e8705a", "Managed load total"),
    ("curtailed", "#4bbf9a", "Curtailed"),
    ("export_neg", "#5a8dee", "Grid net power"),
]


def _svg(runs, width=960, height=380, pad=54):
    """One panel per run, sharing a y scale so they can be compared by eye."""
    vals = []
    for rows in runs.values():
        for r in rows:
            vals += [r["solar"], r["managed"], r["curtailed"], -r["export"]]
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1.0
    lo, hi = lo - span * 0.08, hi + span * 0.08

    def y(v):
        return pad + (hi - v) / (hi - lo) * (height - 2 * pad)

    out = []
    for name, rows in runs.items():
        t0, t1 = rows[0]["t"], rows[-1]["t"]
        tspan = (t1 - t0) or 1.0

        def x(t):
            return pad + (t - t0) / tspan * (width - 2 * pad)

        grid = []
        for frac in range(5):
            gy = pad + frac * (height - 2 * pad) / 4
            val = hi - frac * (hi - lo) / 4
            grid.append(
                f'<line x1="{pad}" y1="{gy:.1f}" x2="{width - pad}" y2="{gy:.1f}" '
                f'stroke="#3a3f45" stroke-width="1"/>'
                f'<text x="{pad - 8}" y="{gy + 4:.1f}" fill="#9aa0a6" font-size="11" '
                f'text-anchor="end">{val:,.0f}</text>'
            )
        for frac in range(5):
            gx = pad + frac * (width - 2 * pad) / 4
            grid.append(
                f'<line x1="{gx:.1f}" y1="{pad}" x2="{gx:.1f}" y2="{height - pad}" '
                f'stroke="#3a3f45" stroke-width="1"/>'
                f'<text x="{gx:.1f}" y="{height - pad + 18}" fill="#9aa0a6" '
                f'font-size="11" text-anchor="middle">'
                f'{t0 + frac * tspan / 4:.0f}s</text>'
            )

        paths = []
        for key, colour, _ in SERIES:
            pts = " ".join(
                f"{x(r['t']):.1f},{y(-r['export'] if key == 'export_neg' else r[key]):.1f}"
                for r in rows
            )
            paths.append(
                f'<polyline points="{pts}" fill="none" stroke="{colour}" '
                f'stroke-width="1.6" stroke-linejoin="round"/>'
            )

        legend = []
        for i, (_, colour, label) in enumerate(SERIES):
            lx = pad + i * 210
            legend.append(
                f'<circle cx="{lx}" cy="{height - 14}" r="5" fill="{colour}"/>'
                f'<text x="{lx + 11}" y="{height - 10}" fill="#c8cdd2" '
                f'font-size="12">{label}</text>'
            )

        out.append(
            f'<figure style="margin:0 0 26px 0">'
            f'<figcaption style="color:#e8eaed;font:600 15px system-ui;'
            f'margin:0 0 6px 2px">{name}</figcaption>'
            f'<svg width="{width}" height="{height}" '
            f'style="background:#1c1f24;border-radius:12px">'
            f'<rect width="{width}" height="{height}" fill="#1c1f24"/>'
            f'{"".join(grid)}{"".join(paths)}{"".join(legend)}'
            f'<text x="{pad}" y="{pad - 14}" fill="#9aa0a6" font-size="11">W</text>'
            f"</svg></figure>"
        )
    return "".join(out)


def plot(runs, path, summaries=None):
    """Write a self-contained page. Open it in a browser; no server needed."""
    rows_html = ""
    if summaries:
        head = (
            "<tr><th>run</th><th>err mean</th><th>err max</th>"
            "<th>curtailed mean</th><th>export min</th><th>writes/min</th></tr>"
        )
        body = "".join(
            f"<tr><td>{s['label']}</td><td>{s['err_mean']:,.0f}</td>"
            f"<td>{s['err_max']:,.0f}</td><td>{s['curtailed_mean']:,.0f}</td>"
            f"<td>{s['export_min']:,.0f}</td><td>{s['writes_per_min']:.1f}</td></tr>"
            for s in summaries
        )
        rows_html = (
            "<table style='border-collapse:collapse;color:#c8cdd2;"
            "font:13px system-ui;margin-top:8px'>"
            "<style>th,td{border:1px solid #3a3f45;padding:5px 11px;text-align:right}"
            "th:first-child,td:first-child{text-align:left}</style>"
            f"{head}{body}</table>"
        )
    Path(path).write_text(
        "<meta charset='utf-8'><title>Loop dynamics</title>"
        "<body style='background:#111418;margin:0;padding:22px'>"
        f"{_svg(runs)}{rows_html}</body>",
        encoding="utf-8",
    )
    return path


# -- entry point ----------------------------------------------------------
def _configs():
    """The three the rig measured on 2026-09-08, so the harness can be checked
    against numbers that came off real hardware before it is trusted.

    Named by what the CALLER sets, never by the constants - a label naming a
    time constant goes stale the moment that constant is retuned, and then
    reads as a measurement of something it is not.
    """
    return [
        ("1 s refresh, device ramp 50%", dict(device_ramp=0.5)),
        ("1 s refresh, device ramp 100%", dict(device_ramp=1.0)),
        ("10 s refresh, device ramp 100%", dict(site_freq=10.0, device_ramp=1.0)),
    ]


# Every whole second of the rig's 5 s instrument tick (Sim's tick_phase_s).
TICK_PHASES = (0.0, 1.0, 2.0, 3.0, 4.0)


@contextmanager
def permit_tau(tau):
    """PERMIT_TAU_S set to ``tau`` for the block, where apply_smoothing reads it.

    ``smoothing`` binds the constant at import (``from ..const import ...``),
    so it is rebound on that module rather than on ``const`` - patching const
    would change nothing already imported.
    """
    from custom_components.dynamic_ocpp_evse.control import smoothing

    original = smoothing.PERMIT_TAU_S
    smoothing.PERMIT_TAU_S = tau
    try:
        yield
    finally:
        smoothing.PERMIT_TAU_S = original


def fixed_point_ring(**kw):
    """Solar held flat: (register peak-to-peak, mean of the second half's
    windows, and the last window's). The last window says whether it settles;
    the second half says whether it is still hunting."""
    sim = Sim(**kw)
    pp = [w[1] for w in ring(run(sim, hold(14700.0, 210.0, sim.dt), warmup_s=120.0))]
    late = pp[len(pp) // 2:]
    return sum(late) / len(late), pp[-1]


def sweep_permit_tau(values=(1.0, 1.5, 2.0, 3.0, 4.0, 5.0, 7.0, 10.0),
                     phases=TICK_PHASES, **kw):
    """Where does the permit filter stop damping the loop?

    A single run cannot answer this. On the rig each point costs seven minutes
    and the window-to-window scatter is as large as the effect, so the
    2026-09-08 A/B of 5.6 against 2.0 came down to reading two noisy columns.
    Here the whole curve costs seconds. ``values`` are the ones the rig
    measured on 2026-09-24 (const/common.py), so the two tables line up.

    Each point is the mean over ``phases`` - every whole second of the rig's
    5 s instrument tick - because a rig restart lands the tick anywhere
    against the command gate, and that decides whether a short filter rings:
    at 1 s, from 25 W to over 600 W. ``ring_late_worst`` is the worst phase.
    """
    def mean(xs):
        return sum(xs) / len(xs)

    out = []
    for tau in values:
        with permit_tau(tau):
            rings = [fixed_point_ring(tick_phase_s=p, **kw) for p in phases]
            reports = []
            for phase in phases:
                sim = Sim(tick_phase_s=phase, **kw)
                reports.append(report(run(sim, sine(dt=sim.dt), warmup_s=120.0)))
        out.append(
            {
                "tau": tau,
                "ring_last": mean([last for _, last in rings]),
                "ring_late_mean": mean([late for late, _ in rings]),
                "ring_late_worst": max(late for late, _ in rings),
                "err_mean": mean([r["err_mean"] for r in reports]),
                "curtailed_mean": mean([r["curtailed_mean"] for r in reports]),
                "writes_per_min": mean([r["writes_per_min"] for r in reports]),
            }
        )
    return out


def main(argv):
    plot_path = None
    if "--plot" in argv:
        plot_path = argv[argv.index("--plot") + 1]

    runs, summaries = {}, []
    print("MOVING SURPLUS - solar 14.7 kW +/- 1.1 kW over 2 x 150 s\n")
    for label, kw in _configs():
        sim = Sim(**kw)
        rows = run(sim, sine(dt=sim.dt), warmup_s=120.0)
        s = report(rows, label)
        summaries.append(s)
        runs[f"moving surplus - {label}"] = rows
        print(
            f"  {label:<22} err {s['err_mean']:>5,.0f}/{s['err_max']:>5,.0f} W  "
            f"curtailed {s['curtailed_mean']:>4,.0f}/{s['curtailed_max']:>4,.0f} W  "
            f"export min {s['export_min']:>7,.0f} W  "
            f"writes {s['writes_per_min']:>4.1f}/min"
        )

    print("\nFIXED POINT - solar held flat, peak-to-peak per 30 s window\n")
    for label, kw in _configs():
        sim = Sim(**kw)
        rows = run(sim, hold(14700.0, 210.0, kw.get("site_freq", 1.0)),
                   warmup_s=120.0)
        runs[f"fixed point - {label}"] = rows
        pp = ring(rows)
        print(f"  {label}")
        for lo, reg_pp, exp_pp in pp:
            print(f"      {lo:>4.0f}-{lo + 30:<4.0f}  reg {reg_pp:>6,.0f} W   "
                  f"export {exp_pp:>6,.0f} W")

    print("\nPERMIT_TAU_S SWEEP - ring in the second half vs tracking, "
          "mean of the five tick phases\n")
    print(f"  {'tau':>5} {'ring late':>10} {'worst':>6} {'ring last':>10} "
          f"{'err mean':>9} {'curtailed':>10} {'writes/min':>11}")
    for row in sweep_permit_tau():
        print(f"  {row['tau']:>5.1f} {row['ring_late_mean']:>10,.0f} "
              f"{row['ring_late_worst']:>6,.0f} "
              f"{row['ring_last']:>10,.0f} {row['err_mean']:>9,.0f} "
              f"{row['curtailed_mean']:>10,.0f} {row['writes_per_min']:>11.1f}")

    print("\nRAMP ON A BREAKER-LIMITED PHASE - 25 A breaker, 8 A household, "
          "17 A allowance\n")
    for label, rate in (("car at 1 A/s", 1.0), ("instant step", 100.0)):
        r = ramp(rate)
        print(f"  {label:<13} permit {r['permit_over']:>5,.0f} W over the "
              f"allowance, site {r['site_over']:>5,.0f} W over the breaker")

    if plot_path:
        plot(runs, plot_path, summaries)
        print(f"\nchart written to {plot_path}")


if __name__ == "__main__":
    main(sys.argv[1:])
