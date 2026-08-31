# Load Juggler

Intelligent load management for Home Assistant. Dynamically distributes available power across your managed loads — EV chargers (via OCPP), smart plugs, and more — based on solar production, battery state, grid capacity, and per-load operating modes. Works with both grid-tied and off-grid installations.

## Table of Contents

- [Features](#features)
- [Supported Load Types](#supported-load-types)
- [Operating Modes](#operating-modes)
- [Distribution Modes](#distribution-modes)
- [Battery System Support](#battery-system-support)
- [Installation](#installation)
- [Configuration](#configuration)
- [Services & Automations](#services--automations)
- [Supported Equipment](#supported-equipment)
- [Troubleshooting](#troubleshooting)
- [Testing and Feedback](#testing-and-feedback)

## Features

- **Dynamic load management** — distributes available power in real time across all managed loads
- **Per-load operating modes** — each load chooses its own mode independently (Standard, Solar Priority, Solar Only, Excess)
- **Multi-load priority distribution** — Shared, Priority, Optimized, or Strict allocation (see [Distribution Modes Guide](DISTRIBUTION_MODES_GUIDE.md))
- **Battery system integration** with SOC thresholds and discharge control
- **Phase-aware calculations** for 1-phase, 2-phase, and 3-phase installations
- **Per-load phase mapping** (L1/L2/L3 to site phases A/B/C)
- **Symmetric and asymmetric inverter** support
- **Circuit groups** — shared breaker limits for co-located loads
- **Off-grid support** — grid CTs optional, infers phases from inverter output
- **Auto-detection** of sensors, phase mapping, and charger settings
- **OCPP 1.6J** control for EV chargers (Amps or Watts, auto-detected)
- **Hot water tank control** — climate-entity-driven binary heating loads with away/normal/boost setpoints
- **Portable power station control** — modulated charge rate plus a managed backup reserve, so a station soaks up surplus and spends it again
- **Relative and absolute OCPP profile modes** for different charger compatibility
- **Current rate limiting** (ramp up/down) for stable operation
- **Failsafe operation** — loads revert to safe defaults if sensors become unavailable (EMA holdover, grid stale detection)

## Supported Load Types

| Type | Control Method | Available Modes | Description |
|------|---------------|-----------------|-------------|
| **EVSE** | OCPP 1.6J current/power profiles | Standard, Solar Priority, Solar Only, Excess | EV chargers with variable current control |
| **Smart Plug** | On/off switch | Continuous, Solar Priority, Solar Only, Excess | Any device behind a smart plug (heaters, pumps, etc.) |
| **Hot Water Tank** | Climate entity (on/off + setpoint) | Freeze Protection, Normal, Solar Priority | Tank with a thermostat (e.g. Generic Thermostat); the mode picks an away/normal/boost setpoint |
| **Power Station** | Charge-speed + backup-reserve numbers | Standard, Solar Priority, Solar Only, Excess | Portable station (EcoFlow Delta and similar); modulates its charge rate, and its reserve is the on/off gate |
| **SG Ready** | *Planned* | Automatic | 2-relay site-state mapping (Block/Normal/Recommend/Force) |

## Operating Modes

Each load has its own operating mode, set independently. This allows mixing modes across your loads — for example, your daily driver on Standard while a pool heater runs on Solar Only.

### EVSE Modes

- **Standard**: Charges as fast as possible from all available power sources (grid + solar + battery). Ideal for maximum charging speed.
- **Solar Priority**: Charges at minimum current, increases with solar production. Prevents grid export while maintaining minimum charge rate. With battery: graduates charging based on SOC thresholds.
- **Solar Only**: Only charges when sufficient solar power is available. Zero grid import — stops if import would be required.
- **Excess**: Starts charging only once the site can no longer absorb its own production — grid export has reached the configured threshold *and* the home battery is charging as fast as it is allowed to (or is full). That verdict is what starts the charge, at the load's minimum current even when the surplus of the moment is smaller than that minimum; from there the rate follows the surplus. Designed for large solar systems to soak up power that would otherwise be curtailed.

### Smart Plug Modes

- **Continuous**: Always on (when connected) — uses the grid if needed.
- **Solar Priority**: On while the battery is above its minimum SOC (drains stored solar down to the minimum; no grid). Without a battery, on when live solar surplus covers the plug.
- **Solar Only**: On while the battery is above its target SOC (uses only the surplus stored above target). Without a battery, on when live solar surplus covers the plug.
- **Excess**: Turns on only when the battery is near-full, or the site has run out of places to put its production — grid export at its allowance *and* the battery already charging as fast as it can.

### Hot Water Tank Modes

A hot water tank is driven through a `climate` entity (e.g. a Generic Thermostat) — the climate entity handles temperature regulation, while Load Juggler picks one of three setpoints (**Away**, **Normal**, **Boost**) based on the mode and conditions.

- **Freeze Protection**: Targets the Away setpoint (minimal / frost protection), raised to Boost when there is surplus energy — the hub reports Excess (see Excess mode above), or the home battery is above its target SOC.
- **Normal**: Targets the Normal setpoint, raised to Boost on the same surplus test.
- **Solar Priority**: Targets Away below the battery minimum SOC, Normal up to the battery target SOC, and Boost at/above the target — heats from solar surplus only. Without a battery there is no SOC band to follow, so it stays at Normal.

While a tank is aiming at its **Boost** setpoint it is heating past what its mode asks for, on energy the site would otherwise dump — so it competes at the Excess urgency tier instead of its own, and yields power to every must-run load. A Solar Priority tank that has dropped below its Normal temperature keeps its promoted tier: needing heat outranks having free energy.

### Power Station Modes

A portable power station (EcoFlow Delta and similar, via a local integration such as [ha-ef-ble](https://github.com/rabits/ha-ef-ble)) charges at a rate Load Juggler sets, so it uses the same four modes as an EVSE — **Excess** by default, since absorbing surplus is the point.

Its second knob does the gating. The charge-speed control has no zero (200 W is typically the floor), so "don't charge" is expressed through the **backup reserve** instead: below the station's current battery level it draws nothing from the wall and runs its own loads from its battery.

- **Nothing to absorb**: reserve drops to the **Normal Reserve** (default 30%) — the station discharges into its own loads down to that floor.
- **Power allocated**: reserve rises to the station's own Max Charge Limit and it charges at the allocated rate.
- **Storm Reserve switch on**: reserve holds at the **Storm Reserve** (default 80%), charged from any source at full rate and not discharged below — this overrides the operating mode.

Whatever is plugged into the station passes through to its outputs and counts as household consumption; only the charging component (`AC input − AC output`) is treated as this load's draw. Minimum and maximum charge power are configured rather than read from the device, so a station can be held below what its hardware allows.

### Mode Urgency

When multiple loads compete for limited power, modes determine priority:

**Standard/Continuous (highest) > Solar Priority > Solar Only > Excess (lowest)**

Within the same mode, the load's priority number decides who gets power first.

For detailed mode behavior with battery systems, examples, and configuration parameters, see the [Charge Modes Guide](CHARGE_MODES_GUIDE.md).

## Distribution Modes

When multiple loads are managed by a single hub, the distribution mode determines how available power is split:

| Mode | Strategy |
|------|----------|
| **Shared** | Equal split after minimums |
| **Priority** | Higher priority gets remainder first |
| **Optimized** | Sequential with leftover sharing |
| **Strict** | Fully satisfy highest priority first |

See the [Distribution Modes Guide](DISTRIBUTION_MODES_GUIDE.md) for detailed explanations.

### Circuit Groups

When multiple loads share a sub-breaker (e.g., two chargers on a 20A circuit), create a circuit group with a per-phase current limit. The system enforces the group limit after distribution, reducing lower-priority members first. See the [Distribution Modes Guide](DISTRIBUTION_MODES_GUIDE.md#circuit-groups) for details.

## Off-Grid Support

Load Juggler works on off-grid installations. When no grid CT entities are configured:
- Active phases are inferred from inverter output entities
- Grid current is treated as 0A (same calculation engine, no separate code paths)
- Solar production is derived from inverter output (series: `solar = inverter - battery`, parallel: `solar = inverter`)

Configure inverter output entities on the inverter entry. The hub status sensor shows "Off-grid mode" when no grid CTs are present.

## Multiple Inverters

A site can have several inverters — typically an older AC-coupled string inverter plus a newer hybrid with a battery. Each is added as its own **Inverter** entry linked to the hub (*Add Inverter / Home Battery* in the setup menu), carrying everything physically attached to it: capacity, per-phase limit, wiring topology, output sensors, its PV array (production sensor and solar forecast device) and optionally its battery (SOC/power sensors, charge/discharge limits, full-SOC, capacity).

The hub keeps only what is site-wide — the grid connection, export limit, base consumption, SOC hysteresis and forecast SOC floor — on a single **Hub Settings** page.

The hub aggregates the whole fleet: capacities and outputs sum, solar production is each inverter's own sensor when it has one and derived from its output otherwise (a series inverter's output minus its own battery flow), and the hub's battery sensors are fleet aggregates — SOC is capacity-weighted across all batteries. Forecast devices from every inverter merge into one site forecast, because clipping is decided by the site-wide export limit that all arrays share. Excess mode counts each battery's charge capacity only while *that* battery is below *its* full SOC, and discharge capacity only for batteries above the site minimum. The SOC Target / SOC Min sliders and Allow Grid Charging switch remain hub-level site policy applied to the fleet.

Each inverter device gets its own sensors: Solar Production, Battery SOC/Power, and — with the PV clipping forecast enabled — the **Recommended Battery Max SOC** and **Recommended Battery Charge Limit** for *its* battery (the fleet reservation split by capacity and charge rate).

**Upgrading:** a hub configured the classic way (inverter/battery/solar fields on the hub itself) is migrated automatically on startup — those fields move onto an inverter entry, and the hub's sensors keep their entity IDs as fleet aggregates. No manual steps.

## Battery System Support

- **Battery SOC monitoring** — tracks current battery state of charge
- **SOC target management** — respects minimum battery charge levels
- **Intelligent discharge control** — allows battery power to supplement loads when SOC is above target
- **Charge/discharge power limits** — configurable maximum battery charge and discharge rates
- **Grid charging control** — optional switch to allow/disallow charging from grid power
- **SOC hysteresis** — prevents oscillation when battery SOC hovers near thresholds

Battery entities (SOC Target, SOC Min, Allow Grid Charging) are only shown when a battery sensor is configured.

### PV Clipping Forecast (export-limited sites)

Sites with more PV than they may export (e.g. 15 kWp behind a 5 kW export limit) curtail the midday peak if the home battery fills up on morning production — energy that could have been exported instead. When a **grid export limit**, a **battery capacity** and one or more **solar forecast devices** (from the [Open-Meteo Solar Forecast](https://github.com/rany2/ha-open-meteo-solar-forecast) integration, which creates one device per PV array — select each array's device) are configured, the hub computes how much of the forecast production cannot be exported or consumed, and publishes **advisory sensors** — Load Juggler never commands the house battery itself:

| Sensor | Meaning |
|---|---|
| Forecast Clippable Energy (kWh) | Production above the export limit + base consumption in the next clipping window — the rest of today while today still clips, tomorrow's peak once it does not |
| Forecast Storable Energy (kWh) | The part of that the battery could physically absorb at its charge rate |
| Recommended Battery Max SOC (%) | SOC ceiling that leaves exactly that much headroom **below where the battery was heading anyway** — your *Normal SOC ceiling source*, or 100% when none is configured — and rises back to that destination as the peak passes, so the battery still ends the day where you wanted it — then holds there through the evening and drops to the next day's reserve just in time for it: the overnight discharge is estimated from your Base consumption and scheduled against the forecast's first crossing above that draw (with a 20% head start), so the house drains the battery for free through the small hours instead of the floor parking it full while the grid serves the night |
| Battery Headroom Deficit (kWh) | Non-zero when the battery already holds more than the recommendation allows |
| Recommended Battery Charge Limit (W) | Charge-rate cap protecting the reserved headroom **and the destination** — direct feedback that steers *export* onto one *Excess trigger margin* below the export limit, so export settles just under the limit and a hard-limiting inverter cannot mask the signal; at or above the destination it holds whatever the forecast says, and it releases to full rate only below the destination, and there only when nothing is left to clip or SOC is comfortably below the reserved ceiling |

The reserve is carved below the battery's **destination** rather than below 100%. On a battery that normally stops at 95%, a 2 kWh reserve in a 20 kWh pack holds it at 85% and lets it arrive at 95% by absorbing the clip through the peak — where aiming at 100% would reserve that unused top 5% twice and meet the peak with only 5% of room. With several batteries the destinations are averaged by capacity; with no ceiling source configured anywhere the anchor is 100%, exactly as before. The band between the destination and 100% is left alone on purpose: it is the buffer for a forecast that under-reads, and above the destination the battery becomes the *absorber of last resort* — an engaged Excess load takes the surplus first and the battery only charges with what that load cannot absorb. The destination is therefore a **standing ceiling**, not something only a forecast clip enforces: on a day forecast to clip nothing the battery still stops charging when it gets there, and climbs past it only on production the site genuinely cannot export. It resumes charging at full rate once the house has taken it a couple of percent back below the destination.

#### Writing the limit to the inverter

By default this is advice only — feed it to your inverter with an automation if you like. An inverter entry can also apply it itself: on its **Battery Charge Management** page, point *Battery charge limit entity* at the inverter's own maximum-charge-current number (Deye: `Battery Max Charge Current`), pick whether that register takes DC amps or watts, and give it a battery voltage source for the conversion. That adds a **Battery Charge Control** switch to the inverter device.

Nothing is written until you turn that switch on. While it is on, the recommended charge limit is written to the register and the normal value (a value you configure, or the register's own maximum) is restored as soon as the advice releases. Writes are paced **directionally**, because these registers go over Modbus and some firmwares commit them to EEPROM: a *lower* limit is written only once every reading in the last *How long a reduction must hold* (default 5 min) agrees on it, and then only by the least reduction they all agree on — so a boiling kettle, a passing cloud or a car plugging in costs no write at all — while a *higher* limit is written as soon as it clears the *Write deadband* (default 5% of the normal limit), still bounded to one Excess trigger margin per write. The one exception is the cap **engaging**: that is protection, and it lands immediately. A **Charge Control** sensor on the inverter measures the register itself in its own unit (amps or watts), so it graphs and earns long-term statistics, with the control's standing (`off` / `idle` / `limiting`) as a `control_state` attribute beside it.

While the limit is engaged the value is **direct feedback on your own meter**, and it holds no history at all. Each cycle it permits what the battery is already absorbing plus whatever *reconstructed* export is running over the setpoint (the export limit less the Excess trigger margin) — reconstructed being the same draws-credited-back figure the Excess trigger uses, so your own loads can never steer it. That lands export on the trigger point in one step whatever the house happens to be doing, and it needs no guess about the house: **Base consumption** is not consulted here at all, only in the clippable- and storable-energy figures, where it is a day-scale average and belongs. A battery that is *selling* rather than absorbing simply makes the first term negative and the value floors at zero — a charge cap cannot force a discharge. The volatility a live meter brings is absorbed at the register instead (see the pacing above), which is why nothing here has to be slow: a cloud, a kettle or a car is answered honestly and then forgotten. It does need your inverter's **Battery power entity**; without one the value falls back to the export error alone, which is conservative — a real surplus is then admitted only as fast as the meter shows it.

The two features meet in Excess mode: while the limit is engaged, Excess counts only the charge rate the register actually permits, not the battery's plate rating. A clipping window is exactly when the site has surplus it cannot place, so it is when Excess-mode loads (EVSE, plug, tank boost, power station) engage — where counting the forbidden rate would have locked them out for the whole window. Advice alone changes nothing: with the switch off, the battery really does still charge at its plate rating.

**Your inverter's work mode.** The charge control assumes the work mode keeps battery discharge off the meter — a Deye in *Zero-Export-to-CT*, or any equivalent self-consumption mode where the pack serves the house and never sells. Both places the site's own readings feed a decision are robust to a battery that does sell: the Excess verdict counts only **solar** export (the pack's discharge is netted off the meter reading, so stored energy can never start an Excess load), and the charge limit treats a discharging pack as exactly that — the value it would need is a discharge, which a charge cap cannot ask for, so it floors at zero instead of chasing the meter. What is *not* supported is running a sell-down **underneath** the charge control: a work mode or automation that actively empties the battery below its destination while this control is trying to hold or refill it is two controllers fighting over one pack, and which one wins depends on which wrote last. Arm the charge control or the sell-down, not both at once.

The charge *rate* is the register every hybrid exposes, and slowing the fill leaves the battery charging all day rather than stopping it dead. Where a real SOC ceiling exists, the same page can write that instead — or as well. On a Deye the ceiling is not one register but one per **time-of-use slot**, so *Battery SOC ceiling entities* takes a list: select every slot the battery may charge in and the recommended max SOC is written to all of them together. *Normal SOC ceiling source* names the entity your everyday ceiling lives in (an `input_number`, a template sensor, whatever your automations already set); Load Juggler writes the **lower** of that and the recommendation, so it can only ever hold the battery below what you asked for, and changes you make there propagate to the slots on the next cycle. That same entity is what the clipping reserve is measured down from (see above), so it is worth pointing at your real everyday ceiling even if you never arm the write. Without it the everyday ceiling — and the reserve's anchor — is 100%; if it is configured but unavailable, nothing is written that cycle rather than a ceiling being guessed, and the advice keeps using the last value it published. A separate **Battery SOC Control** switch arms it (default off), writes are paced by the same interval setting with a fixed 1-point deadband applied per slot, and no release write is needed — the recommendation climbing back to your ceiling as the peak passes hands the slots back by itself. A **SOC Control** sensor reports the ceiling being enforced, with each slot's read-back as an attribute.

## Installation

**Method 1 _(easiest)_:**

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=LeoAlioth&repository=Dynamic_OCPP_EVSE&category=integration)

**Method 2:**

1. Download the files from the repository
2. Copy the `dynamic_ocpp_evse` folder into `custom_components/dynamic_ocpp_evse` in your Home Assistant config directory
3. Restart Home Assistant

## Configuration

### Prerequisites

Create a template sensor for the maximum import power limit. You will need it during configuration:

- The template can be whatever you want, for a simple static example of 6 kW: `{{ 6000 }}`
- Unit of measurement: W
- Device class: Power
- State class: Measurement

I recommend including "Power Limit" in the name so it gets auto-selected during configuration.

### Quick Start

1. Go to **Settings > Devices & Services > Add Integration** and search for `Dynamic OCPP EVSE`
2. **Create a Hub** (represents your electrical site):
   - Select your phase current/power sensors (all optional — for off-grid sites, configure inverter output entities instead)
   - Configure main breaker rating, voltage, and max import power
   - Optionally configure battery sensors and inverter settings
3. **Add a Load** (the integration auto-discovers OCPP chargers):
   - Confirm the detected EVSE or manually select entities
   - For EVSE: set min/max current, phase count, and phase mapping
   - For smart plugs: select the plug switch entity and optional power sensor
4. **Press the Reset OCPP EVSE button** on EVSE devices to clear any existing profiles
5. Set each load's operating mode and you're ready to go

### Configuration Reference

#### Hub (Site) Settings

| Field | Description | Default |
|---|---|---|
| Phase A/B/C current entity | Grid current sensors (A or W, auto-converted) | — |
| Main breaker rating | Maximum current per phase (A) | 25A |
| Phase voltage | Voltage per phase (V) | 230V |
| Max import power entity | Template sensor for grid import limit (W) | — |
| Grid export limit | Physical/contract export ceiling — Excess triggers at limit − margin, and it enables the PV clipping forecast (0 = unlimited, both off) | 0 |
| Excess trigger margin | How far below the export limit Excess mode engages (W) | 500W |
| Invert phases | Flip CT polarity if installed backwards | Off |
| Distribution mode | How to allocate power between loads | Priority |
| Solar/Excess grace period | How long Solar/Excess loads keep running at minimum when conditions lapse (min) | 5 |
| Battery SOC hysteresis | SOC change before triggering mode switches (%) | 3% |
| Base house consumption | Typical daytime minimum draw (PV clipping forecast) (W) | 300W |
| Forecast SOC floor | Lowest max-SOC the forecast may recommend (%) | 30% |

#### Inverter Settings

One entry per inverter — everything physically attached to it, including its PV array and battery.

| Field | Description | Default |
|---|---|---|
| Inverter max power | Total inverter capacity (W) | — |
| Inverter max power per phase | Per-phase inverter limit (W) | — |
| Inverter supports asymmetric | Can inverter balance power across phases | Off |
| Inverter output phase A/B/C entity | Per-phase inverter output sensors (optional) | — |
| Wiring topology | Parallel (AC-coupled) or Series (hybrid with DC battery) | Parallel |
| Solar production entity | This inverter's PV power sensor (optional — derived from its output otherwise) | — |
| Solar forecast | Open-Meteo Solar Forecast device(s) for this inverter's array(s) | — |
| Battery SOC entity | Battery state of charge sensor | — |
| Battery power entity | Battery charge/discharge power sensor (W) | — |
| Battery max charge/discharge power | Battery power limits (W) | 5000W |
| Battery full SOC | SOC at/above which this battery counts as full (%) | 97% |
| Battery capacity | kWh this battery's SOC spans (PV clipping forecast, 0 = off) | 0 |
| Battery charge limit entity | Inverter register to write the forecast's charge limit to (optional) | — |
| Charge limit unit | What that register expects — DC amps or watts | A |
| Battery voltage entity / nominal voltage | Source for the watts↔amps conversion | — / 51.2V |
| Normal charge limit | Value restored when the limit releases (0 = the register's own max) | 0 |
| How long a reduction must hold | A lower charge limit is written only when every reading in this window agrees (s); also the write pacing for a release and for the SOC ceiling | 300 |
| Write deadband | Minimum change worth a write, as % of the normal limit | 5% |

#### EVSE Load Settings

| Field | Description | Default |
|---|---|---|
| EVSE current import entity | OCPP current import sensor | — |
| EVSE current offered entity | OCPP current offered sensor | — |
| OCPP device ID | Device ID for OCPP service calls | — |
| Min/Max charge current | Charger operating range (A) | 6A / 16A |
| Phases | Number of phases (1 or 3) | 3 |
| Priority | Distribution priority (1=highest) | 1 |
| L1/L2/L3 phase mapping | Which site phase each charger leg uses | A/B/C |
| Charge rate unit | Amps or Watts (auto-detected) | Auto |
| Profile validity mode | Absolute (timestamp) or Relative (duration) | Absolute |
| OCPP profile timeout | Profile validity duration (seconds) | 120 |
| Charge pause duration | Minimum pause before restart (minutes) | 3 |
| Stack level | OCPP charging profile stack level | 2 |

#### Smart Plug Load Settings

| Field | Description | Default |
|---|---|---|
| Plug switch entity | The on/off switch entity | — |
| Power monitoring entity | Power sensor for the plug (optional, auto-detected) | — |
| Priority | Distribution priority (1=highest) | 1 |

## Services & Automations

### Available Services

| Service | Description | Parameters |
|---|---|---|
| `dynamic_ocpp_evse.set_operating_mode` | Change a load's operating mode | `entry_id`, `mode` (Standard/Continuous/Solar Priority/Solar Only/Excess) |
| `dynamic_ocpp_evse.set_distribution_mode` | Change hub distribution mode | `entry_id`, `mode` (Shared/Priority/Sequential - Optimized/Sequential - Strict) |
| `dynamic_ocpp_evse.set_max_current` | Set charger max current | `entry_id`, `current` (A) |
| `dynamic_ocpp_evse.set_min_current` | Set charger min current | `entry_id`, `current` (A) |
| `dynamic_ocpp_evse.reset_ocpp_evse` | Reset charger profiles | `entry_id` |

The `entry_id` is the config entry ID of the hub or load. You can find it in the URL when viewing the integration entry in Settings, or by inspecting the device info.

### Using the Select Entity (Alternative)

You can also change modes directly via the select entity in automations:

```yaml
action:
  - service: select.select_option
    target:
      entity_id: select.dynamic_ocpp_evse_<charger_name>_operating_mode
    data:
      option: "Standard"
```

### Common Automation Examples

#### Time-of-day charging (free power hours)

```yaml
automation:
  - alias: "Charge at max during free hours"
    trigger:
      - platform: time
        at: "11:00:00"
    action:
      - service: select.select_option
        target:
          entity_id: select.dynamic_ocpp_evse_garage_charger_operating_mode
        data:
          option: "Standard"

  - alias: "Switch to Solar Only after free hours"
    trigger:
      - platform: time
        at: "14:00:00"
    action:
      - service: select.select_option
        target:
          entity_id: select.dynamic_ocpp_evse_garage_charger_operating_mode
        data:
          option: "Solar Only"
```

#### Tariff-based charging (cheap night rate)

```yaml
automation:
  - alias: "Standard mode during cheap tariff"
    trigger:
      - platform: time
        at: "23:00:00"
    action:
      - service: select.select_option
        target:
          entity_id: select.dynamic_ocpp_evse_garage_charger_operating_mode
        data:
          option: "Standard"

  - alias: "Solar Priority during expensive tariff"
    trigger:
      - platform: time
        at: "07:00:00"
    action:
      - service: select.select_option
        target:
          entity_id: select.dynamic_ocpp_evse_garage_charger_operating_mode
        data:
          option: "Solar Priority"
```

#### Mixed-mode: daily driver + pool heater

```yaml
automation:
  - alias: "Morning: car charges fast, pool heater waits for sun"
    trigger:
      - platform: time
        at: "06:00:00"
    action:
      - service: select.select_option
        target:
          entity_id: select.dynamic_ocpp_evse_garage_charger_operating_mode
        data:
          option: "Standard"
      - service: select.select_option
        target:
          entity_id: select.dynamic_ocpp_evse_pool_heater_operating_mode
        data:
          option: "Solar Only"
```

## Supported Equipment

### Power Meters / Inverters

While technically you can use any Home Assistant entity, the integration auto-detects sensors for some setups:

- SolarEdge
- Deye — External CTs
- Deye — Internal CTs
- Solar Assistant — Grid Power (Individual Phases)

The integration supports both current (A) and power (W) sensors, automatically converting power to current using the configured phase voltage.

### EVSE Chargers

Any charger supported by the [OCPP integration](https://github.com/lbbrhzn/ocpp) should work. Tested with:

- EVBox Elvi
- ZJBeny
- Huawei SCharger-7KS-S0

### Smart Plugs

Any smart plug in Home Assistant with an on/off switch entity. Power monitoring sensors are auto-detected for:

- Shelly
- Sonoff
- Tasmota
- Kasa
- Tuya

## Troubleshooting

### Charger rejecting profiles

**Symptom:** Logs show `SetChargingProfile` response `Rejected`

**Solutions:**
1. Press the **Reset OCPP EVSE** button to clear existing profiles
2. Check the charge rate unit — some chargers (e.g., Huawei) only accept Watts, not Amps. The integration auto-detects this, but you can manually set it in charger settings.
3. Try switching profile validity mode from Absolute to Relative (or vice versa)

### Current oscillating / unstable

**Symptom:** Charger current toggles rapidly between values

**Cause:** Charger's internal clock drifts, causing the OCPP profile to expire mid-session

**Solution:** Switch profile validity mode to **Relative** (duration-based) in charger settings

### Solar Only mode not charging

**With battery:** Battery SOC must be at or above the target SOC before Solar Only mode will allocate power.

**Without battery:** Export power must exceed the load's minimum current. Check your grid current sensors.

### Solar Priority mode charging too fast/slow at night

**Expected:** Solar Priority mode charges at the minimum rate when no solar is available. If it's charging faster, check that grid current sensors are reading correctly and the invert_phases setting is correct.

### Hub status sensor shows warnings

The hub creates a **Status** sensor that shows the site health:
- **OK** — everything is configured and working
- **Initializing** — first calculation cycle hasn't completed yet
- **No power measurement** — no grid CTs, inverter output, or solar entity configured
- **Grid sensors unavailable** — configured grid CT sensors are returning unavailable/unknown. Chargers hold last known values for 60s, then fall to minimum current as a safety measure.

Check the sensor's `warnings` attribute for details.

### No entities showing up

After adding the integration, entities are created automatically. If battery-related entities don't appear, it's because no battery sensor is configured — this is intentional to keep the UI clean.

### "Allow Grid Charging" — what does it do?

This switch only affects systems with home batteries, in Standard and Solar Priority modes:
- **ON**: Charging uses all available power including grid import
- **OFF**: Charging stops when it would require grid import (only uses solar + battery discharge above target SOC)

For more troubleshooting tips, see the [Charge Modes Guide](CHARGE_MODES_GUIDE.md#troubleshooting).

## Testing and Feedback

This integration is actively being developed and improved. Looking for users to test it with different setups and provide feedback.

**Especially interested in testing with:**

- Different inverter/meter brands and models
- Battery systems (different brands and configurations)
- Multi-phase vs single-phase installations
- Different grid configurations and power limits
- Smart plugs and mixed load setups

**How to help:**

- Install and test the integration with your setup
- Report any issues or unexpected behavior via [GitHub Issues](https://github.com/LeoAlioth/Dynamic_OCPP_EVSE/issues)
- Share your configuration and experiences
- Suggest improvements or new features
