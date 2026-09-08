# A test Home Assistant, with simulated hardware

The real integration, on a real Home Assistant, reading simulated devices. It
covers the half of the code the pure test tiers cannot reach — the readers, the
config flow, entity publication, the control writers, real timing and restarts —
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

Then open <http://localhost:8124> — port 8124, not 8123, so it can never take
the port a real Home Assistant on this machine wants.

**One manual step:** Home Assistant needs an owner account, and only you can
create it. Location, timezone, currency and units are already set in
`configuration.yaml`, so the wizard is: username and password → Next → Skip
analytics → Finish.

## What is simulated, and how faithfully

One package per device. Delete a file and that device is gone from the site —
which is how you test a site with no battery, or with only a plug.

| Package | Entities to pick in Load Juggler |
|---|---|
| `packages/site.yaml` | grid A/B/C = `sensor.sim_grid_current_a` / `_b` / `_c`, solar = `sensor.sim_solar_power`, battery SOC = `sensor.sim_battery_soc`, battery power = `sensor.sim_battery_power`, voltage = `sensor.sim_phase_voltage`, inverter output = `sensor.sim_inverter_output_a` / `_b` / `_c` |
| `packages/plug.yaml` | switch = `switch.sim_plug`, power monitor = `sensor.sim_plug_power` |
| `packages/tank.yaml` | climate = `climate.sim_water_tank`, power = `sensor.sim_tank_power` |
| `packages/station.yaml` | charge speed = `number.sim_station_charge_speed`, reserve = `number.sim_station_reserve`, battery level = `sensor.sim_station_battery`, AC input = `sensor.sim_station_ac_input` |

**Real, in the sense that the device's own logic is in the loop:**

* **The plug** presents exactly a smart plug's surface — a `switch` the engine
  opens and closes, and a power monitor that reads 0 until the relay closes.
* **The tank** is an actual `generic_thermostat`. The engine writes a
  *setpoint*; Home Assistant's own thermostat decides whether that means
  heating; only then does the element draw; and the engine reads the result
  back as `hvac_action`, which is where its connector status comes from. A tank
  whose thermostat is idle is an inactive load however high the setpoint went —
  that whole chain is real here, not stubbed.
* **The station** is the modulating path: two `number` registers the engine
  writes, and an AC input that ramps toward the register rather than stepping
  to it (`sim_station_ramp` — 100% is a converter, 50% is a car, and the slow
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

**Knobs, not simulations — nothing integrates over time:**

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
* **The station ramps** toward its register instead of stepping to it.
* The binary loads do **not** — a resistive element really does step the
  instant its relay closes.

Turn it off to isolate an engine question from a timing one. No real site is in
that state.

## The two pages

**Site** is the hardware you are pretending to have: the household, array,
pack and voltage sliders, the derived meter, and the power-flow diagram.

**Loads** pairs each managed load with itself — *the engine* card holds Load
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

The Simulator page opens with a **Sankey** — `Grid → the site → each managed
load`, ribbons proportional to what is actually flowing right now. It is Home
Assistant's own `power-sankey` card, which means it reads the **Energy
dashboard's** preferences rather than entities named in the card, so three
things have to line up:

1. **`energy:` in `configuration.yaml`.** The card subscribes to the energy
   collection, and without that component the websocket command does not
   exist — the card fails to subscribe and takes the whole dashboard view down
   with it, blank, with nothing in the Home Assistant log. It would have come
   in with `default_config:`, which is trimmed.
2. **kWh statistics**, in `packages/energy.yaml`: a Riemann sum per source,
   because the energy prefs want an ever-increasing meter. `max_sub_interval`
   is load-bearing there — an integration sensor otherwise only advances when
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
   grid (import, export, and a *power* sensor — `Standard`, pointed at
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
| Site sensor refresh (hub) | 2 s | **1 s** | — |
| Load update frequency | 15 s | **5 s** | three cycles before a change is even re-evaluated |
| Minimum off time (plug, tank) | 5 min | **0** | a load that switches off cannot come back for five minutes |
| Solar/Excess grace period | 5 min | **0** | a load coasts at its minimum for five minutes after conditions fail, so you never see the release |

They live in the config entries, not in the packages, so a reset that keeps
`.storage` keeps them — and one that wipes `.storage` puts the defaults back
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

The engine's own cycle log is the fastest read on what it is thinking — faster
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
`states.sensor | selectattr('object_id', 'match', ...)` — tidy, and silently
broken: filtering a domain on an attribute gives Home Assistant no trackable
entity dependency, so it rendered them once at startup and *never again*. The
grid CTs are derived from those sums, so the whole simulated site froze at
whatever happened to be drawing when HA booted; a tank that switched on later
did not exist to the meter. They now name each source with `states('...')`,
which is tracked. Adding a device means adding a line to `site.yaml`.

`check_physics.py` could not catch that and still cannot: it renders the
templates itself, so it verifies the *arithmetic* and never Home Assistant's
reactivity. When a figure looks stale, compare `last_updated` on the sensor
against `last_updated` on its source — that is what exposed it.

**Renaming a template entity needs its `unique_id` changed too.** Home
Assistant derives an `entity_id` from the name only at *first* registration and
the registry keeps it thereafter, so a name-only edit leaves the old
`entity_id` in place and the references to it silently miss.
