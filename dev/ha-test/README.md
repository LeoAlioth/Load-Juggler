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
  writes, and an AC input that follows the register — and clamps to 0 once the
  pack reads 100%, because a station that has finished charging stops drawing
  however high the register is set.
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
* The CTs respond instantly. Real CTs lag several seconds, which is the
  smoothing and settling the engine is built around — the one omission that
  matters for testing the feedback loop specifically.
* No EVSE: that needs the OCPP integration and a charger to talk to.

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

**Renaming a template entity needs its `unique_id` changed too.** Home
Assistant derives an `entity_id` from the name only at *first* registration and
the registry keeps it thereafter, so a name-only edit leaves the old
`entity_id` in place — and the site package's per-phase sums match on name.

**The aggregates must not be called `sim_managed_*`.** `sensor.sim_site_load_a`
sums every `sensor.sim_managed_<device>_a`; naming it `sim_managed_draw_a` made
it a sum of itself. `check_physics.py` pins this.
