# A test Home Assistant, with simulated hardware

The real integration, on a real Home Assistant, reading simulated devices. It
covers the half of the code the pure test tiers cannot reach - the readers, the
config flow, entity publication, the control writers, real timing and restarts -
while the pytest and scenario suites stay the place for arithmetic.

The grid CTs are **derived, not slider-driven**:

```
grid[phase] = household[phase] + managed loads[phase] − inverter[phase]
inverter total = solar − battery power        (+ charging consumes, − discharging adds)
```

So granting a load power really does move the meter, the way it does on a live
site, and there is no way to set a state no site can be in. Independent CT
sliders would let you export 10 kW from an array making nothing, and the
feedback loop would spend every cycle fighting you.

## Start it

```bash
docker compose -f dev/ha-test/docker-compose.yml up -d
```

Then open <http://localhost:8124> - port 8124, not 8123, so it can never take
the port a real Home Assistant on this machine wants.

**One manual step:** Home Assistant needs an owner account, and only you can
create it. Location, timezone, currency and units are already set in
`configuration.yaml`, so the wizard is: username and password → Next → Skip
analytics → Finish.

## What is simulated, and how faithfully

One package per device. Delete a file and that device is gone from the site -
which is how you test a site with no battery, or with only a plug.

| Package | Entities to pick in Load Juggler |
|---|---|
| `packages/site.yaml` | grid A/B/C = `sensor.sim_grid_current_a` / `_b` / `_c`, solar = `sensor.sim_solar_power`, battery SOC = `sensor.sim_battery_soc`, battery power = `sensor.sim_battery_power`, voltage = `sensor.sim_phase_voltage`, inverter output = `sensor.sim_inverter_output_a` / `_b` / `_c` |
| `packages/plug.yaml` | switch = `switch.sim_plug`, power monitor = `sensor.sim_plug_power` |
| `packages/tank.yaml` | climate = `climate.sim_water_tank`, power = `sensor.sim_tank_power` |
| `packages/station.yaml` | charge speed = `number.sim_station_charge_speed`, reserve = `number.sim_station_reserve`, battery level = `sensor.sim_station_battery`, AC input = `sensor.sim_station_ac_input` |

**Real, in the sense that the device's own logic is in the loop:**

* **The plug** presents exactly a smart plug's surface - a `switch` the engine
  opens and closes, and a power monitor that reads 0 until the relay closes.
* **The tank** is an actual `generic_thermostat`. The engine writes a
  *setpoint*; Home Assistant's own thermostat decides whether that means
  heating; only then does the element draw; and the engine reads the result
  back as `hvac_action`, which is where its connector status comes from. A tank
  whose thermostat is idle is an inactive load however high the setpoint went -
  that whole chain is real here, not stubbed.
* **The station** is the modulating path: two `number` registers the engine
  writes, and an AC input that ramps toward the register rather than stepping
  to it (`sim_station_ramp` - 100% is a converter, 50% is a car, and the slow
  case is what the engine's settling test exists for). It clamps to 0 once the
  pack reads 100%, because a station that has finished charging stops drawing
  however high the register is set.

  It stands in for a modulating EVSE, with two limits worth knowing: Load
  Juggler caps a station at **5 kW** of charge power, so it cannot play an
  11 kW charger; and its phase picker offers A/B/C only, so it is single-phase
  *to the engine* even though the engine's own station builder handles any mask
  and the rig here offers the full set.
* **Phase wiring.** Each device has an `input_select` for the phase(s) it is
  wired to, and a multi-phase load splits its draw evenly. That select is the
  *wiring*; the `connected to phase` field in Load Juggler is the engine's
  *belief* about the wiring. Being able to make them disagree is a test.

**Knobs, not simulations - nothing integrates over time:**

* Water temperature does not rise while the element is on. Drag it below the
  setpoint to make the tank call for heat.
* The station's battery level does not fill, and the house battery's SOC does
  not move with its power.
* The inverter does not decide anything: its charge/discharge split is the
  `sim_battery_power` slider, so curtailment at the export limit is not
  modelled. Reproduce curtailment by pulling `sim_solar` down yourself.
* Nothing models the CTs' *noise*, only their delay. They lag 5–10 s (see
  below) but report the lagged value exactly, where a real CT jitters.
* No EVSE: that needs the OCPP integration and a charger to talk to.

## The measurements lag, on purpose

`input_boolean.sim_lag` (on by default) makes the rig behave like instruments
rather than like arithmetic:

* **The grid CTs are 5–10 s behind.** Sampled every 5 seconds, and each sample
  publishes the *previous* one. That lag is not a detail: it is why the engine
  smooths its readings, why it waits for a load's draw to settle before
  trusting it, and how it can over-commit by granting power twice against a
  surplus the first grant already spent. With instant CTs none of that is
  reachable, and the rig would quietly pass a site the real one fails.
* **`sensor.sim_ct_lag_error`** is how far behind the meter currently is, in
  watts. When the engine over-commits, that is the number it over-committed
  against.
* **Every load's power monitor lags too**, by the same 5–10 s. This one is
  easy to get wrong and expensive when you do: the monitors used to be
  instantaneous while the CTs lagged, and the feedback loop then removed a
  managed draw from a CT that had not registered it yet. For one sample a
  phase read 2 kW lighter than it was - enough to flip an importing phase into
  an exporting one and let a load run where it should have been refused. Three
  separate "engine bugs" were chased before the cause turned out to be the rig.
  The physical draw and the measured draw are now separate entities per device.
* **The station ramps as well as lags**, and the two are different physics: the
  ramp is its converter taking a few seconds to reach the setpoint
  (`sensor.sim_station_draw`), the lag is the meter on it
  (`sensor.sim_station_ac_input`).
* The binary loads' *physical* draw does step the instant the relay closes - a
  resistive element really does - it is only the measurement that lags.

Turn it off to isolate an engine question from a timing one. No real site is in
that state.

## The two pages

**Site** is the hardware you are pretending to have: the household, array,
pack and voltage sliders, the derived meter, and the power-flow diagram.

**Loads** pairs each managed load with itself - *the engine* card holds Load
Juggler's own controls (operating mode, dynamic control, rated power, charge
bounds, reserves) and its verdicts; *the hardware* card holds the simulated
device. Side by side, because a permit the device ignores, or a draw the engine
never granted, is then visible at a glance rather than pieced together from two
pages.

Two figures on the engine side are easy to conflate. **Permitted** is what the
engine grants the device to draw; **counted against the budget** is its
measured footprint, which is what other loads are budgeted against. For a
settled device drawing under its permit the two differ, deliberately.

All four devices are configured: the site, a smart plug on phase A, a hot water
tank on phase B and a power station on phase C.

## The power flow diagram

The Simulator page opens with a **Sankey** - `Grid → the site → each managed
load`, ribbons proportional to what is actually flowing right now. It is Home
Assistant's own `power-sankey` card, which means it reads the **Energy
dashboard's** preferences rather than entities named in the card, so three
things have to line up:

1. **`energy:` in `configuration.yaml`.** The card subscribes to the energy
   collection, and without that component the websocket command does not
   exist - the card fails to subscribe and takes the whole dashboard view down
   with it, blank, with nothing in the Home Assistant log. It would have come
   in with `default_config:`, which is trimmed.
2. **kWh statistics**, in `packages/energy.yaml`: a Riemann sum per source,
   because the energy prefs want an ever-increasing meter. `max_sub_interval`
   is load-bearing there - an integration sensor otherwise only advances when
   its source CHANGES, and a simulated site sits perfectly still between
   slider moves, so the totals would freeze exactly when you left the rig
   running to watch it settle.
3. **The preferences themselves**, which live in `.storage/energy` and so are
   not in git. A committed copy is `energy-prefs.json`; install it into a fresh
   instance with

   ```bash
   cp dev/ha-test/energy-prefs.json dev/ha-test/config/.storage/energy
   ```

   and restart. Or set it up by hand at **Settings → Dashboards → Energy**:
   grid (import, export, and a *power* sensor - `Standard`, pointed at
   `sensor.sim_grid_net_power`, which is positive when importing), solar, the
   battery, and the three managed loads as individual devices. The devices are
   what break the loads out of the single "house" block and make this a Load
   Juggler view rather than a generic site one.

Each source carries a **power** sensor beside its energy statistic. That is
what the Sankey's live ribbons read; the energy statistics only feed the
graphs, and they need a five-minute statistics window before they show
anything.

## The timers are deliberately short here

Every wait in Load Juggler is a real Home Assistant timer, so they all apply in
this instance exactly as on a live site. At their production defaults a single
experiment takes ten minutes of watching, so this rig runs them at the floor of
what the config flow accepts:

| Setting | Production default | Here | What it costs you at the default |
|---|---|---|---|
| Site sensor refresh (hub) | 2 s | **1 s** | - |
| Load update frequency | 15 s | **5 s** | three cycles before a change is even re-evaluated |
| Minimum off time (plug, tank) | 5 min | **0** | a load that switches off cannot come back for five minutes |
| Solar/Excess grace period | 5 min | **0** | a load coasts at its minimum for five minutes after conditions fail, so you never see the release |

They live in the config entries, not in the packages, so a reset that keeps
`.storage` keeps them - and one that wipes `.storage` puts the defaults back
along with everything else.

**Read the rig's cadence as the rig's, not the product's.** Behaviour that
looks like chatter here may simply be the anti-chatter guards switched off; the
minimum off time exists to protect a compressor from exactly what a 0 lets you
do. When something looks wrong, put a timer back to its default before
believing it.

Two waits are NOT config and stay as they are: the engine smooths its grid
readings, so a step change takes roughly 40 s to work through, and the
simulated CTs lag 5–10 s on top of that. Give a slider move four or five
cycles before reading anything.

## Working with it

The engine's own cycle log is the fastest read on what it is thinking - faster
than the Overview page, which only re-renders when you reopen it:

```bash
docker logs -f load-juggler-test 2>&1 | grep dynamic_ocpp_evse
```

It prints the three pools, every load's permit and measured draw, and the
distribution's reasoning, once per site cycle.

**Editing the component:** the working tree is bind-mounted in, so edit a file
and restart Home Assistant (Developer tools → YAML → Restart). No rebuild, and
no second copy of the component to drift from the one the test suites run
against.

**Running the scenario set.** `scenarios.py` drives the site through six
states and reports whether each settled, using Home Assistant's REST API
rather than the browser:

```bash
python3 dev/ha-test/scenarios.py            # all six
CASES=2,3 python3 dev/ha-test/scenarios.py  # just those
```

It needs a long-lived access token (Home Assistant → profile → Security →
Create token), in `dev/ha-test/ha_token` (gitignored) or `$HA_TOKEN`.

Each case waits **160 s** before sampling, and that number is load-bearing. A
binary load settles in well under a minute, but a modulating one rings and
damps: measured from a cold start, the station's permit spread decayed
552 → 414 → 345 → 207 → 0 W over 150 s. Sampling at 95 s catches the ring and
reports it as sustained hunting - a mistake made and then corrected on
2026-09-08. Statuses must be exactly constant to pass; permits are allowed one
register step (100 W) of wobble, because that is the smallest change a device
can actually be told about.

**Testing a moving surplus.** The fixed-point scenarios are a poor test of the
control loop, because at a fixed operating point the ring simply damps away.
`moving_surplus.py` drives the array on a slow sinusoid instead - a passing
cloud - and reports how well a modulating load tracks it:

```bash
python3 dev/ha-test/moving_surplus.py
```

Two numbers matter and they trade against each other: the mean **tracking
error** (surplus the load failed to absorb) and **register writes per minute**
(churn inflicted on the device). Measurements on 2026-09-08, solar 15 kW ± 2.5 kW
over 150 s:

There are TWO exponential filters in series, and they were fixed one at a
time, so the table separates them: the **input** EMA on the site's readings
(`engine/readers._smooth`) and the **permit** EMA on the answer that comes back
out (`control/smoothing.apply_smoothing`).

| site refresh | rate limiting | input EMA | permit EMA | mean error | writes/min |
|---|---|---|---|---|---|
| 1 s | constant slew | per-cycle, tau 3.3 s | per-cycle, tau 3.3 s | 719 W | 3.2 |
| 1 s | adaptive 0.15/s | per-cycle, tau 3.3 s | per-cycle, tau 3.3 s | 596 W | 3.6 |
| 1 s | adaptive 0.40/s | per-cycle, tau 3.3 s | per-cycle, tau 3.3 s | 668 W | 2.8 |
| 10 s | adaptive 0.15/s | per-cycle, **tau 33 s** | per-cycle, **tau 33 s** | 1 164 W | 3.4 |
| 10 s | adaptive 0.15/s | time-based, tau 5.6 s | per-cycle, **tau 33 s** | 991 W | 3.8 |
| 1 s | adaptive 0.15/s | time-based, tau 5.6 s | per-cycle, tau 3.3 s | 766 W | 1.2 |
| 10 s | adaptive 0.15/s | time-based, tau 5.6 s | time-based, tau 5.6 s | **717 W** | 2.0 |
| 1 s | adaptive 0.15/s | time-based, tau 5.6 s | time-based, tau 5.6 s | **832 W** | 1.6 |

Read that table carefully, because it does not say "each change made things
better". The adaptive rate helped (719 -> 596). Making a filter time-based
helped at a SLOW cadence and cost at a fast one, both times - 1 164 -> 991 and
991 -> 717 at 10 s, 596 -> 766 -> 832 at 1 s - because at a 1 s refresh the old
per-call weight was accidentally filtering *less* than the 2 s default
intends, so the new number is the consistent one and the old fast behaviour
was the anomaly.

What the last two rows buy is not a smaller number, it is the DISAPPEARANCE OF
THE SETTING'S SIDE EFFECT. Before, moving the site refresh from 1 s to 10 s
cost 225 W of tracking (766 -> 991) that nothing in the UI warned about. After,
the two cadences land within run-to-run noise of each other, and the slower one
is no longer the loser. A refresh rate is a politeness setting for the
inverter's Modbus; it should not be a control-loop tuning knob.

The remaining 10 s vs 1 s gap ran the OTHER way (717 against 832) and was the
rate limiter, not the EMA: `approach = min(RAMP_APPROACH_MAX, RAMP_APPROACH_RATE
* site_freq)` was a fraction per CYCLE that saturated at 0.9 for any interval
past ~6 s, so a 10 s site closed 90% of its error per cycle (tau ~4.3 s) where a
1 s site closed 15% (tau ~6.2 s) - the same per-cycle-versus-per-second mistake
the EMAs had, hiding behind the cap.

That was the third and last stage to convert. `RAMP_TAU_S` now drives it through
the same `1 - exp(-dt/tau)` as the filters, so all three stages of the pipeline
are fixed in seconds:

| interval | old approach | now |
|---|---|---|
| 1 s | 0.150 | 0.164 |
| 2 s | 0.300 | 0.300 |
| 5 s | 0.750 | 0.591 |
| 10 s | 0.900 | 0.832 |
| 30 s and up | 0.900 | 0.900 |

The 2 s column is identical by construction, so default-configured sites did not
move. `RAMP_TAU_S` is a separate constant from `EMA_TAU_S` despite holding the
same 5.6 s: it is derived independently (the old 0.15/s over the 2 s default
closes 0.30, and tau = -2 / ln(1 - 0.30) = 5.6 s), and the ramp and the input
filter are different design decisions that should be retunable apart.

### The inverter curtails, so unabsorbed surplus is LOST

Added 2026-09-08. `Sim inverter delivered` caps the array at
`household + managed loads + export limit`, which is what a real inverter does:
it holds its own export limit by throttling, and the production sensor reads the
lower figure. Nothing on a site exports past its limit and apologises
afterwards. `Sim curtailed power` is the meter for what is being thrown away,
and `Inverter curtails to the export limit` turns the whole thing off if you
want to see what the array COULD have made.

This changes what the rig measures, and for the better. Before it, the rig
exported 12.9 kW against an 11 kW limit and the excess was scored as "tracking
error" - a quantity no real site can even produce. With it, export is pinned at
the limit by the inverter and the unabsorbed surplus shows up where it belongs:
as production that never happened. That is the number Excess mode exists to
drive to zero.

It also makes the engine's difficulty visible. Curtailed power is invisible from
the meter - the export reading sits at the limit whether 0 W or 2 kW is being
wasted behind it - which is exactly why the pool is sized from a loads-off
RECONSTRUCTION rather than from export directly.

Baseline with curtailment on, solar 14.7 kW +/- 1.1 kW, 1 s refresh
(2026-09-08):

| measure | value |
|---|---|
| tracking error mean/max | 608 / 1 220 W |
| curtailed mean/max | 186 / 955 W |
| export mean/min/max | 10 516 / 9 147 / 11 001 W |
| cycles over the 11 kW limit | 4 of 80, by 1 W |
| register writes | 41 (8.2 per minute), range 200-2 300 W |

Read the export row rather than the tracking row for anything that matters. The
site never exceeded its limit, because the inverter will not let it; what it did
was dip 1 853 W BELOW the limit at the top of the cycle, which is the station
still drawing 2 392 W against a 1 527 W ideal. Over-absorption costs exported
energy, under-absorption costs curtailed energy, and only the second is
recoverable by anything the engine does.

A CAVEAT ON EVERY ROW ABOVE, found on 2026-09-08 while reading the traces
rather than the summaries. All the loads in this rig carry
`solar_grace_period: 0`, set when the timers were shortened to make scenarios
run quickly. For that particular timer 0 does not mean "short", it means OFF -
and the grace hold is the thing that bridges a permit collapse. Without it, the
moment the station's permit dips under its 200 W minimum, `entities/load.py`
starts a charge pause of `CHARGE_PAUSE_DURATION` (default **3 minutes**, never
configured here) and pins the command at 0 for the whole span however much
surplus returns. Both 2026-09-08 runs show it: the permit recovers to 1 794 W
while the register sits at 200 W for the rest of the window.

That does not invalidate the comparison - the metric samples the PERMIT, which
keeps tracking throughout, and every row was measured under the same
configuration - but it does mean the second half of each run exercises the
allocator without exercising the actuation, and the writes/min figures are
lower than a properly configured site would show. Give the rig a non-zero grace
period before reading anything into register churn.

Note also how much of the remaining error is the METRIC rather than the loop:
`ideal` is clamped at 0 whenever the surplus falls below the station's 200 W
minimum, so a permit correctly decaying through 500 W is scored as 500 W of
error. Do not chase this number below ~600 W without first making the metric
honest about that.

With the constant slew the permit moved at exactly `RAMP_UP_RATE` /
`RAMP_DOWN_RATE` the whole time - saturated, so it could not keep up. The
proportional term fixes that, and then going faster stops helping: past about
0.15/s the bottleneck is the measurement chain (CT lag plus the engine's grid
EMA), not the follower.

**Checking the rig itself**, when a number looks wrong and you need to know
whether it is the engine or the simulator:

```bash
docker cp dev/ha-test/check_physics.py load-juggler-test:/tmp/
docker exec load-juggler-test python3 /tmp/check_physics.py
```

It renders the templates against stubbed states and checks the arithmetic
against hand-computed answers.

**Resetting.** The account and the config entries live in `.storage`:

```bash
docker compose -f dev/ha-test/docker-compose.yml down
rm -rf dev/ha-test/config/.storage dev/ha-test/config/*.db*
docker compose -f dev/ha-test/docker-compose.yml up -d
```

Only `configuration.yaml`, `packages/`, `dashboards/` and this file are in git;
everything else Home Assistant writes there is ignored.

## Two traps

**A template that iterates a domain is never re-rendered.** The per-phase
aggregates used to gather their sources with
`states.sensor | selectattr('object_id', 'match', ...)` - tidy, and silently
broken: filtering a domain on an attribute gives Home Assistant no trackable
entity dependency, so it rendered them once at startup and *never again*. The
grid CTs are derived from those sums, so the whole simulated site froze at
whatever happened to be drawing when HA booted; a tank that switched on later
did not exist to the meter. They now name each source with `states('...')`,
which is tracked. Adding a device means adding a line to `site.yaml`.

`check_physics.py` could not catch that and still cannot: it renders the
templates itself, so it verifies the *arithmetic* and never Home Assistant's
reactivity. When a figure looks stale, compare `last_updated` on the sensor
against `last_updated` on its source - that is what exposed it.

**Renaming a template entity needs its `unique_id` changed too.** Home
Assistant derives an `entity_id` from the name only at *first* registration and
the registry keeps it thereafter, so a name-only edit leaves the old
`entity_id` in place and the references to it silently miss.
