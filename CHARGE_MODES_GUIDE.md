# Load Juggler — Operating Modes Guide

For distribution modes (Shared, Priority, Optimized, Strict), see [DISTRIBUTION_MODES_GUIDE.md](DISTRIBUTION_MODES_GUIDE.md).
For circuit groups (shared breaker limits), see [DISTRIBUTION_MODES_GUIDE.md](DISTRIBUTION_MODES_GUIDE.md#circuit-groups).

## Table of Contents
1. [Operating Modes Overview](#operating-modes-overview)
2. [Standard Mode](#standard-mode) (EVSE)
3. [Continuous Mode](#continuous-mode) (Smart Plug)
4. [Solar Priority Mode](#solar-priority-mode)
5. [Solar Only Mode](#solar-only-mode)
6. [Excess Mode](#excess-mode)
7. [Hot Water Tank Modes](#hot-water-tank-modes)
8. [Power Station Modes](#power-station-modes)
9. [Configuration Parameters](#configuration-parameters)

---

## Operating Modes Overview

Load Juggler provides per-load operating modes — each managed load chooses its own mode independently. This allows mixing modes across your loads (e.g., daily driver on Standard while a pool heater runs on Solar Only).

### Mode Urgency

When multiple loads compete for limited power, mode urgency determines allocation order:

**Standard/Continuous (highest) > Solar Priority > Solar Only > Excess (lowest)**

Within the same mode, the load's priority number decides who gets power first.

### Quick Comparison Table (EVSE)

| Mode | Without Battery | With Battery | Grid Import | Battery Discharge |
|------|----------------|--------------|-------------|-------------------|
| **Standard** | Full speed from grid + solar | Full speed, battery provides no power below min SOC | Yes | Yes (above min SOC) |
| **Solar Priority** | Min rate + solar (prevents export) | Graduated charging based on battery SOC | Minimal | Above target SOC only |
| **Solar Only** | Solar/export only (stops if import needed) | Solar only, requires battery at target SOC | No | Above target SOC only |
| **Excess** | Charge when export >= the export allowance | Charge once export **and** battery charging together reach both allowances | No | No |

### Smart Plug Modes

A smart plug is a binary on/off load, so its modes resolve to a simple on/off
decision. It has **four** modes that form a ladder — each one drains the home
**battery** to a progressively higher SOC floor, and none of them (bar
Continuous, and bar the grace window noted below) ever use the grid:

| Mode | Battery floor | With battery (hybrid or off-grid) | Without battery |
|------|---------------|-----------------------------------|-----------------|
| **Continuous** | minimum, then grid | Always on | Always on |
| **Solar Priority** | minimum | On while battery SOC is **above the minimum** | On when live solar surplus covers the plug |
| **Solar Only** | target | On while battery SOC is **above the target** | On when live solar surplus covers the plug |
| **Excess** | near-full | On while battery SOC is **at/above the "full" SOC** (default 97%), or the site has run out of absorption capacity | On when grid export reaches the export allowance |

**Why the battery changes things:** with a battery, the battery *is* the
surplus buffer — it stores solar. Each mode decides how deep into that buffer
the plug may dig:

- **Continuous** — a must-run load: drains the battery to its minimum SOC,
  then pulls from the grid, and stops only if neither can supply it.
- **Solar Priority** — drains the battery to its minimum SOC, then stops.
  Never uses the grid: this mode has **no grace ride-through** (by design —
  a grace hold would also bridge minimum-SOC sheds, and the minimum SOC is a
  protective floor that must act immediately), so it sheds at once on
  inverter saturation.
- **Solar Only** — drains only the band *above the target* SOC (genuine stored
  surplus), then stops. Doesn't use the grid beyond the grace window described
  below.

> **Both halves must agree:** these modes are gated on SOC **and** on the
> inverter being able to deliver it. SOC only says the energy is *stored*; if
> the inverter is already putting out its rated power (other loads saturate it),
> that stored energy has no path to the plug and the shortfall would come from
> the grid. So the plug additionally needs the inverter's rating to cover its
> own draw, judged as if the plug were off — a plug whose own draw is what fills
> the inverter keeps running, it is never asked to shed itself.
>
> **Saturation shorter than the Solar grace period rides through.** While the
> grace window runs the plug stays on, and on a grid-tied site that means it is
> briefly grid-assisted — the honest price of not flapping the relay on every
> passing peak. Saturation that outlasts the window sheds the plug. Set the
> grace period to how long you are willing to import for: 0 sheds immediately.
> Off grid there is nothing to import from, and the same shed protects the
> inverter from running past its rating.
>
> Sites with no inverter capacity configured cannot be checked and behave as
> before: SOC alone decides.
- **Excess** — runs only when the battery is essentially full (the configurable
  "full" SOC, default 97%) *and* the inverter can pass the plug's draw, or the
  site can no longer absorb its production — export at its allowance *and* the
  battery already charging as fast as it can. The absorption verdict needs no
  inverter check of its own: power the site is exporting is already on the AC
  bus, and the plug only redirects it.
  See [Excess Mode](#excess-mode) for the single comparison behind that.

This is the same on a hybrid grid-tied site and an off-grid site — only the
presence of a battery matters, not the grid connection.

Without a battery there is no buffer: **Solar Priority** and **Solar Only**
both fall back to "on when live solar surplus covers the plug" (they differ
only on a battery system), and **Excess** needs grid export above the
configured threshold.

A plug's solar mode competes for power at the same urgency tier as the
matching EVSE mode — Solar Priority at tier 2, Solar Only at tier 3 — so a
plug and an EVSE in the same mode are ordered by their configured priority
numbers.

> EVSEs are unaffected by this — they modulate charge current to the available
> solar/excess power rather than switching fully on/off.

---

## Standard Mode

**Available for:** EVSE

### Purpose
Maximum speed charging — charge as fast as possible within available power limits.

### Without Battery

**Behavior:**
- Uses all available power sources (grid + solar/export)
- Charges at maximum available current
- Respects site breaker limits and configured max current

**When to use:**
- Need fastest charging possible
- Don't care about electricity costs
- Emergency charging situations

**Example:**
```
Available Grid Import: 10A
Solar Export: 5A
Result: Charge at 15A (if charger supports it)
```

### With Battery

**Behavior:**
- Uses all available power sources (grid + battery + solar)
- Allows battery discharge for EV charging when SOC >= min threshold
- When battery SOC < min threshold: battery provides no power (acts like no-battery system)

**Battery SOC Thresholds:**
- **Below min_soc**: Battery provides no power (acts like no-battery system, still charges from grid + solar)
- **Above min_soc**: Full speed with battery discharge available

**When to use:**
- Battery is sufficiently charged for discharge
- Fast charging takes priority over battery preservation
- Time-sensitive charging needs

**Example Scenarios:**

*Scenario 1: Battery above min SOC*
```
Battery SOC: 60% (above min SOC of 20%)
Battery Max Discharge: 5000W = 22A @ 230V
Available Grid Import: 10A
Solar Export: 3A
Result: Charge at min(22A + 10A + 3A, max_current) = 35A or less
```

*Scenario 2: Battery below min SOC*
```
Battery SOC: 15% (below min SOC of 20%)
Battery contribution: 0A (battery cannot provide power)
Available Grid Import: 10A
Solar Export: 3A
Result: Charge at 13A (grid + solar only, battery protected)
```

---

## Continuous Mode

**Available for:** Smart Plug

### Purpose
Always-on operation. The load stays powered whenever it is connected.

**When to use:**
- Devices that should always run when plugged in
- Non-solar-dependent loads that still benefit from priority-based power allocation

---

## Solar Priority Mode

**Available for:** EVSE, Smart Plug

### Purpose
Economical charging that prioritizes solar production and minimizes grid export while maintaining a minimum charging rate. Formerly known as "Eco" mode.

> For a **smart plug** this mode is a binary on/off — see [Smart Plug Modes](#smart-plug-modes) for how the battery SOC band gates it. The sections below describe the EVSE's modulating behaviour.

### Without Battery

**Behavior:**
- Uses available solar/export power to prevent grid export
- Guarantees minimum charging rate even without solar
- Keeps grid import at a minimum

**Logic:**
```
target_current = max(solar_available, min_current)
If solar_available < min_current:
    Charge at min_current (small grid import)
Else:
    Charge at solar_available (no grid import)
```

**When to use:**
- Maximize use of solar production
- Minimize electricity costs
- Don't want to export solar to grid when car is available

**Example Scenarios:**

*Scenario 1: Sunny day with excess solar*
```
Solar Export: 8A available
Min Current: 6A
Result: Charge at 8A (using excess solar)
Grid Import: 0A
```

*Scenario 2: Cloudy day with minimal solar*
```
Solar Export: 2A available
Min Current: 6A
Result: Charge at 6A (minimum rate)
Grid Import: 4A
```

### With Battery

**Behavior:**
Graduated charging based on battery SOC with smart solar utilization:

1. **Below min_soc**: No charging (protect battery)
2. **Between min_soc and target_soc**: Use solar/export + minimum rate
   - Uses available solar/export to prevent grid export
   - Guarantees minimum charging rate
   - No battery discharge for EV
3. **At target_soc with solar**: Solar production rate
   - Battery at target and charging = excess solar available
   - Charge at solar rate
4. **Above target_soc**: Full speed (like Standard mode)
   - Battery well-charged
   - Battery discharge available for EV

**When to use:**
- Balance between charging speed and battery management
- Prefer solar charging when possible
- Want to prevent exporting solar to grid
- Acceptable to charge slowly during low solar periods

**Example Scenarios:**

*Scenario 1: Battery at 15% (below min 20%)*
```
Battery SOC: 15% (below min SOC)
Solar Production: 4000W = 17A @ 230V
Min Current: 6A
Grid Import: 0W (no import, no export)
Battery Power: 0W (protected, not discharging)
Result: Charge at max(17A, 6A) = 17A (acts like no-battery system)
```

*Scenario 2: Battery at 50% (between min 20% and target 80%)*
```
Battery SOC: 50% (between min and target)
Solar Production: 1380W = 6A @ 230V
Min Current: 6A
Grid Import: 0W (solar exactly matches minimum)
Battery Power: 0W (not charging, not discharging - all solar to EV)
Result: Charge at 6A (minimum rate from solar)
```

*Scenario 3: Battery at 80% (at target SOC)*
```
Battery SOC: 80% (at target)
Solar Production: 2300W = 10A @ 230V
Grid Import: 0W (no grid import)
Battery Power: 0W (not charging - all solar to EV)
Result: Charge at 10A (match solar production)
```

*Scenario 4: Battery at 85% (above target)*
```
Battery SOC: 85% (above target)
Battery Max Discharge: 5000W = 22A @ 230V
Grid Available: 10A
Solar: 0A (nighttime)
Result: Charge at 32A (battery discharge + grid)
Battery discharges to provide full speed charging
```

---

## Solar Only Mode

**Available for:** EVSE, Smart Plug

### Purpose
Pure solar charging — stricter than Solar Priority about using only solar power (no grid import, no battery discharge below target SOC).

> For a **smart plug** this mode is a binary on/off — see [Smart Plug Modes](#smart-plug-modes) for how the battery SOC band gates it. The sections below describe the EVSE's modulating behaviour.

### Without Battery

**Behavior:**
- Only charges when solar is available (exporting to grid)
- Uses export power for EV charging
- Zero grid import — stops charging if grid import would be required
- Stops charging when solar production drops below minimum current

**When to use:**
- Want 100% solar-powered charging
- Excess solar would otherwise export to grid
- Not time-sensitive
- Maximizing solar self-consumption

**Example:**
```
Scenario: Sunny afternoon
Solar Export: 12A available
Result: Charge at 12A (pure solar)

Scenario: Cloudy/Evening
Solar Export: 2A available
Min Current: 6A
Result: No charging (would require grid import)
```

### With Battery

**Behavior:**
- Requires battery at or above target SOC
- Only charges from solar production (uses power that would charge battery)
- Battery SOC gating:
  - **Below target_soc**: No charging (prioritize battery charging)
  - **At/above target_soc with solar**: Charge at solar rate

**When to use:**
- Battery is sufficiently charged
- Want to prevent grid export without using battery
- Maximize solar self-consumption
- Not time-sensitive

**Example Scenarios:**

*Scenario 1: Battery full, sunny day*
```
Battery SOC: 82% (above target 80%)
Solar Export: 10A
Battery Power: -2300W (still charging slowly)
Result: Charge at 10A (solar rate)
```

*Scenario 2: Battery below target*
```
Battery SOC: 70% (below target 80%)
Solar Export: 15A available
Result: No charging (prioritize battery charging)
```

---

## Excess Mode

**Available for:** EVSE, Smart Plug

### Purpose
Threshold-based charging that starts when excess export exceeds a configured threshold, preventing excessive solar export while managing battery capacity.

> For a **smart plug** this mode is a binary on/off — see [Smart Plug Modes](#smart-plug-modes) for how it is gated (battery near-full, or the site exporting). The sections below describe the EVSE's modulating behaviour.

### Without Battery

**Behavior:**
- Threshold-based charging that uses excess export above threshold
- Starts charging when `export_power > threshold`
- Charging rate: `max(min_current, (export_power - threshold) / voltage)`

**Logic:**
```
If export_power > threshold:
    charge_current = max(min_current, (export_power - threshold) / voltage)
Else:
    No charging
```

**When to use:**
- Want to prevent excessive export to grid
- Don't want to charge from minimal solar (wait for significant excess)
- Prefer longer, more stable charging sessions
- Battery-less solar systems with variable production

**Example:**
```
Threshold: 13000W (56A @ 230V)
Current Export: 15000W (65A @ 230V)
Min Current: 6A
Excess: 15000W - 13000W = 2000W (8.7A @ 230V)
Result: Charge at max(6A, 8.7A) = 8.7A
```

### With Battery

Excess means the site can no longer absorb its own production anywhere else. A
battery is a second sink alongside grid export, so both are summed into a single
comparison — one number decides Excess for every load:

```
margin = (grid export + battery charge power + our own managed load draws)
       - (export allowance + battery charge allowance)

Excess is on when  margin >= 0   — and the margin IS the excess pool, in watts
```

A sink contributes its allowance only while it can actually absorb:

| Sink | Allowance | Zeroed when |
| ---- | --------- | ----------- |
| Grid export | **Grid Export Limit − Excess Trigger Margin** | The site is off-grid — nothing can leave. (No limit configured = infinite allowance: the grid absorbs everything, so grid-side Excess never triggers.) |
| Battery charging | **Battery Max Charge Power** | No battery is configured, **or** SOC is at/above the **Battery Full SOC** |

The margin (default 500 W) exists because an inverter curtails slightly *under*
the export limit — a trigger exactly at the limit would never fire. Enter your
real physical/contract limit; the trigger takes care of itself.

The full-battery rule matters: a full battery draws no charge power, so leaving
its rating in the allowance would make the trigger unreachable exactly when the
site is dumping the most energy. Zeroing it is the same treatment as a battery
that isn't there.

**Multiple batteries** (inverter entries): each battery's charge rating counts
toward the allowance only while *that* battery is below *its own* Full SOC —
one full battery drops out of the allowance while an empty sibling keeps
absorbing. The SOC the SOC-gated modes read (Solar Priority bands, plug
above-min/above-target) is the capacity-weighted fleet SOC, and the discharge
capacity offered to loads excludes any battery below the site minimum SOC,
however high the fleet average sits.

Zero counts as on, because it is the saturated case — export sitting at the
allowance *and* the battery pulling its maximum charge rate is precisely "nothing
more can be absorbed".

A site with no allowance at all therefore sits exactly at the trigger: off-grid
with a full battery. That is correct — a full battery cannot take another watt,
and an off-grid inverter in that state is curtailing — and it is self-limiting,
because a margin of zero is a pool of zero, so EVSEs and plugs, which need a pool
above zero, still get nothing. Only a consumer reading the plain verdict acts on
it: the hot water tank's boost setpoint.

**Why a battery below its maximum blocks Excess.** If the battery still has
charge headroom, that surplus belongs in the battery, not in an opportunistic
load. On an ideal hybrid inverter the two are equivalent — the battery takes
everything first, so export only appears once it is saturated — but on real sites
export and an unsaturated battery do coexist (export-limit curtailment, SOC
tapering, AC-coupled inverters), and this is what stops a load from stealing
charge the battery wanted.

**Hysteresis.** Once Excess engages, `capacity` drops by 500 W until it
disengages, so a load doesn't chatter at the trigger point.

**Managed draws are added back.** Our own loads' consumption is restored before
the comparison, so a load already running on excess does not disqualify itself by
consuming the surplus that started it. Grid-tied the feedback loop does this by
adding the draws back into export, which makes the margin equal *production minus
household* — invariant to our loads, and invariant to whether the inverter serves
them by cutting export or by throttling battery charging. In meter terms a load
drawing *P* keeps running until the meter reads `allowance - 500 W - P`.

Off-grid there is no grid reading to add them back to, so the margin adds the
draws itself. That turns a running load into a **probe**: a curtailing inverter
ramps up to serve it, and the margin settles at the site's true surplus, which is
otherwise invisible — an off-grid inverter never reports the headroom it isn't
using. A 2 kW load on an array with 3 kW curtailed lifts the margin to 2 kW; on an
array with only 1 kW spare it settles at 1 kW, the amount that was genuinely free.

No SOC floor guards this, and none is needed. A **discharging** battery
contributes nothing to the absorbed side, so the moment a load pushes the battery
past charging and into discharge, the margin collapses and Excess clears on its
own. While the margin does hold, the worst a load can do is make the battery
charge more slowly — it can never drain it. Beyond the full-battery rule, SOC
plays no part in the Excess decision on any site.

A **smart plug** in Excess mode has one extra trigger: it also turns on at/above
the Battery Full SOC even with no export at all.

**Example Scenarios** (13000 W export allowance, 5000 W charge allowance):

*Scenario 1: Battery still charging, with headroom*
```
Battery SOC: 60%, charging at 2000W
Export at the CT: 13000W
absorbed = 13000 + 2000 = 15000W   capacity = 13000 + 5000 = 18000W
Result: No charging — the battery has 3000W of headroom, so this is not surplus
```

*Scenario 2: Both sinks saturated*
```
Battery SOC: 60%, charging at its full 5000W
Export at the CT: 13000W
absorbed = 18000W   capacity = 18000W   (equality counts)
Result: Charging starts
```

*Scenario 3: Battery full*
```
Battery SOC: 98% — absorbs nothing, so its allowance drops out
Export at the CT: 13600W
absorbed = 13600W   capacity = 13000W
Result: Charge on the 600W above the allowance
```

*Scenario 4: Off-grid, battery at maximum charge*
```
No grid CTs, so the export allowance is 0
Battery SOC: 90% (above the 80% target), charging at its full 5000W
margin = 5000 - 5000 = 0
Result: Excess triggers — previously impossible off-grid, where export is always 0
```

*Scenario 5: Off-grid, the load probes for curtailed production*
```
Array capable of 6000W but curtailed to 5000W by the battery's charge limit
A 2000W load starts; the inverter ramps to 6000W, so charging falls to 4000W
margin = 4000 (charge) + 2000 (our load) - 5000 = +1000W
Result: Stays on, and reports the 1000W that was genuinely spare. Without the
        add-back this would read -1000W and flip off, then on, every cycle
```

*Scenario 6: Off-grid, the load outruns production*
```
Production can no longer cover household + our 2000W load, so the battery is
discharging 1000W to help. Discharge counts as absorbing nothing.
margin = 0 (charge) + 2000 (our load) - 5000 = -3000W
Result: Excess clears — no SOC floor needed, the formula self-corrects
```

---

## Hot Water Tank Modes

**Available for:** Hot Water Tank

A hot water tank is a binary (on/off) load driven through a Home Assistant `climate` entity — for example a [Generic Thermostat](https://www.home-assistant.io/integrations/generic_thermostat/). The climate entity owns all temperature regulation (hysteresis, minimum cycle duration, the temperature sensor). Load Juggler only decides **when** heating is allowed and **which target temperature** to write.

### Setpoints

The tank has three configurable target temperatures — set during setup and adjustable afterwards as number sliders:

| Setpoint | Typical use |
| -------- | ----------- |
| **Away** | Minimal / frost-protection temperature |
| **Normal** | Everyday baseline temperature |
| **Boost** | High temperature, used when surplus energy is available |

### Modes

The operating mode decides which setpoint the tank targets, based on conditions:

| Mode | Target setpoint | Power source |
| ---- | --------------- | ------------ |
| **Freeze Protection** | `Away`, raised to `Boost` when there is surplus — the hub reports **Excess** (see [Excess Mode](#excess-mode)), or the home battery is above its target SOC | Any source (Continuous urgency at the floor, Excess urgency while boosting) |
| **Normal** | `Normal`, raised to `Boost` on the same surplus test | Any source (Continuous urgency at the floor, Excess urgency while boosting) |
| **Solar Priority** | `Away` below the battery minimum SOC, `Normal` up to the battery target SOC, `Boost` at/above the target SOC | Solar surplus, with a grid-backed minimum below target SOC (Solar Priority urgency) |

### How It Works

- Load Juggler reads the climate entity's `hvac_action`. When the thermostat reports `idle` (water already at temperature), the tank frees its reserved power for other loads.
- When heating is allowed, Load Juggler sets the climate entity to `heat` and writes the resolved setpoint; when not, it sets the entity to `off`.
- To the power-distribution engine the tank behaves like a smart load — a fixed-power binary draw — so it competes for power with EVSEs and smart plugs by mode urgency, then priority. Freeze Protection and Normal compete at **Continuous** urgency (must-run); Solar Priority competes at **Solar Priority** urgency, so it yields to must-run loads but still outranks Solar Only / Excess loads.
- **Surplus demotion:** whichever mode is selected, a tank aiming at its `Boost` setpoint drops to the **Excess** urgency tier for as long as it is boosting. Heating past the temperature the mode actually asks for is opportunistic, so it must not outrank must-run loads. The cold-tank promotion takes precedence: a Solar Priority tank below its Normal temperature keeps tier 1 even while boosting.
- Every tank mode always keeps heating *permitted* — the mode moves the target temperature, and the grid may cover the floor. A tank is only starved of power by contention (its tier losing out), never by its mode.
- **What counts as surplus:** the hub's single Excess verdict — grid export plus battery charging measured against what the site is allowed to absorb, hysteresis band included. The tank reads the same verdict an Excess-mode EVSE or plug triggers on, so "there is real surplus" is defined in exactly one place. See [Excess Mode](#excess-mode) for the comparison, including the managed-draw add-back that keeps a boosting tank from disqualifying itself.
- On an **off-grid** system there is no grid export, so the export allowance is zero and Excess is decided entirely by the battery — it triggers once the battery is charging at its maximum rate. The battery-above-target clause still lifts the boost as well, and the Solar Priority SOC bands work unchanged.
- On a **no-battery grid-tied** system every SOC clause drops out: Freeze Protection and Normal become purely export-driven (the battery term is zero on both sides of the Excess comparison), and Solar Priority has no band to follow — it stays at `Normal` and never boosts.

### Example Scenarios

*Scenario 1: Normal mode, sunny afternoon, battery full*
```
Mode: Normal | Battery SOC: 90% (target 80%)
Result: Target = Boost — the full battery signals surplus energy
```

*Scenario 2: Solar Priority, battery still charging*
```
Mode: Solar Priority | Battery SOC: 55% (min 20%, target 80%)
Result: Target = Normal — heat to the baseline, from solar surplus only
```

*Scenario 3: Solar Priority, battery depleted*
```
Mode: Solar Priority | Battery SOC: 15% (min 20%)
Result: Target = Away — frost protection only; let solar refill the battery first
```

---

## Power Station Modes

**Available for:** Portable Power Station

A portable power station (EcoFlow Delta and similar, via a local integration such
as [ha-ef-ble](https://github.com/rabits/ha-ef-ble)) is a battery you can charge
at a commandable rate. To the engine it is an EVSE without the OCPP: it modulates,
so it uses the **same four modes and the same behaviors** — see [Excess
Mode](#excess-mode), which is the default and the point of the device type.

### Two knobs, two jobs

| Knob | Entity | What Load Juggler does with it |
| ---- | ------ | ------------------------------ |
| **AC charging speed** | `number`, W | The engine's allocation, floored to the device's 100 W step |
| **Backup reserve** | `number`, % | The on/off gate — see below |

The charge-rate knob has no zero: a station with a 200 W minimum cannot be told
"don't charge". What stops it is the **backup reserve**. In the station's
self-powered mode the reserve is both the SOC it grid-charges *up to* and the
floor it discharges *down to*, so dropping it below the current battery level
stops the wall draw completely — and the station then spends what it stored on its
own loads until it reaches that floor.

So the reserve is resolved every command cycle, like a hot water tank's setpoint:

| State | Reserve written | Effect |
| ----- | --------------- | ------ |
| Nothing to absorb | **Normal Reserve** (default 30%) | Draws nothing; discharges into its own loads down to 30% |
| Engine allocated ≥ the minimum charge power | The station's own **Max Charge Limit** | Accepts the charge, at the allocated rate |
| **Storm Reserve** switch on | **Storm Reserve** (default 80%) | Charges from any source at full rate and holds it |

The reserve is never raised above the station's own Max Charge Limit — that is
your battery-health cap, read and respected rather than overwritten. Storm reserve
overrides the operating mode (the station competes as a must-run load while it is
on), because a backup reserve that may only be filled from surplus is not a
reserve.

### Pass-through is not this load's draw

Whatever is plugged into the station passes through to its outputs. That
consumption is ordinary household load, not something Load Juggler controls, so
only the *charging* component counts as the station's managed draw:
`AC input power − AC output power`. Counting the whole wall draw would let the
feedback loop add the pass-through back as available surplus. Without those two
sensors configured the commanded speed is used instead.

### Charge bounds are configured, not read

Minimum and Maximum Charge Power are set during configuration and adjustable as
sliders afterwards. They are deliberately *not* read from the device: a station
whose hardware accepts 2400 W can be held to less. The minimum matters more than
it sounds — an allocation below it cannot be expressed at all, so the mode drops
the reserve instead of writing a rate.

### How It Works

- Once the station reaches its Max Charge Limit it reports as inactive, and the
  engine hands its allocated power to other loads.
- These integrations typically talk **Bluetooth LE, one connection at a time**, so
  opening the vendor's phone app silently takes control away from Home Assistant.
  A station whose charge-speed entity goes unavailable is treated as unavailable
  rather than continuing to be allocated power it cannot accept.
- Writes are deadbanded to one device step (100 W), so a jittering allocation
  doesn't spam the BLE link with values the device would round to what it already has.
- The station is a **load, not a sink**: unlike the home battery in
  [Excess Mode](#excess-mode), its charging is ours to command, so it competes for
  the excess pool rather than contributing to the absorption allowance.

### Example Scenarios

*Scenario 1: Excess mode, clipping inverter*
```
Mode: Excess | Margin: 900W | Min/Max charge power: 200/2400W
Result: Charge at 900W (floored to 900), reserve raised to the 90% charge limit
```

*Scenario 2: Excess mode, margin too small*
```
Mode: Excess | Margin: 150W (below the 200W minimum)
Result: Not charging — reserve dropped to 30%, wall draw goes to zero, and the
        station runs its own loads from its battery
```

*Scenario 3: Storm reserve*
```
Storm Reserve switch: on | Storm level: 80%
Result: Reserve 80%, charge speed at maximum, competing as a must-run load
        regardless of the selected mode
```

### Automating the Storm Reserve

The Storm Reserve switch is deliberately a plain switch, so *when* to hold a
reserve is your decision rather than something baked into the integration. The
natural trigger is a severe-weather warning, which is issued precisely because
disruption is expected — a better predictor of an outage than any forecast
condition or gust threshold.

Be aware what you are automating: the switch forces the station to charge from
whatever source is available, at full rate, until it reaches the storm level. A
trigger-happy automation costs real grid import, which is the argument for
warning-based triggers over "the forecast mentions thunder".

**Official warnings (recommended).** In the EU, [MeteoAlarm](https://www.home-assistant.io/integrations/meteoalarm/)
covers most countries but is YAML-configured. Where a national integration exists
it is usually both UI-configurable and more detailed — for Slovenia,
[the ARSO integration](https://github.com/andrejs2/slovenian_weather_integration)
exposes a warning binary sensor whose `najvisja_stopnja` attribute is the highest
active warning level (1 minor → 4 extreme):

```yaml
alias: Storm reserve follows weather warnings
triggers:
  - trigger: numeric_state
    entity_id: binary_sensor.arso_opozorila_<location>_aktivno_opozorilo
    attribute: najvisja_stopnja
    above: 2
    id: arm
  - trigger: numeric_state
    entity_id: binary_sensor.arso_opozorila_<location>_aktivno_opozorilo
    attribute: najvisja_stopnja
    below: 3
    for: "02:00:00"
    id: stand_down
actions:
  - action: "switch.turn_{{ 'on' if trigger.id == 'arm' else 'off' }}"
    target:
      entity_id: switch.<station_name>_storm_reserve
mode: single
```

Level 3 (orange/severe) is roughly where outage risk becomes real; level 2 fires
often enough that you would be grid-charging on most summer afternoons. The
two-hour `for:` on stand-down matters — warnings flap at their edges, and each
flip swings the station between full-rate charging and idling.

**Forecast-based, without a warnings integration.** Forecasts are no longer
attributes, so scanning them means calling
[`weather.get_forecasts`](https://www.home-assistant.io/actions/weather.get_forecasts/)
and reading its response. Put that in a trigger-based template binary sensor and
point the automation above at it:

```yaml
template:
  - trigger:
      - trigger: time_pattern
        minutes: "/30"
      - trigger: homeassistant
        event: start
    action:
      - action: weather.get_forecasts
        target:
          entity_id: weather.forecast_home      # or weather.openmeteo
        data:
          type: hourly
        response_variable: fc
    binary_sensor:
      - name: Storm expected
        state: >
          {% set f = fc['weather.forecast_home'].forecast[:8] %}
          {% set stormy = f | selectattr('condition', 'in',
                              ['lightning', 'lightning-rainy']) | list %}
          {% set g = f | selectattr('wind_gust_speed', 'defined')
                       | map(attribute='wind_gust_speed') | list %}
          {{ stormy | count > 0 or (g and g | max > 70) }}
```

Check what your provider actually returns before trusting the gust test — call
`weather.get_forecasts` in Developer Tools → Actions and read the response.
`wind_gust_speed` is missing from some providers' hourly forecasts, and the
`defined` guard then silently leaves you with condition-matching only. Gusts are
in km/h.

Eight hours of lookahead is generous: filling from the normal reserve to the storm
level is well under an hour at full charge rate. The longer window mainly buys a
chance to fill from surplus before falling back to the grid.

Prefer keeping this in an **automation** rather than a template sensor if you want
to stay UI-managed — trigger-based template sensors cannot be created through
Helpers, but an automation can hold the same trigger, action and template
condition, and lives in Home Assistant's own storage.

---

## Configuration Parameters

### Hub-Level Configuration

| Parameter | Description | Default | Used By |
|-----------|-------------|---------|---------|
| **Main Breaker Rating** | Maximum current per phase (A) | 25A | All modes |
| **Phase Voltage** | Voltage per phase (V) | 230V | All modes |
| **Max Import Power** | Maximum grid import power (W) | - | All modes |
| **Grid Export Limit** | The site's physical/contract export ceiling (W); 0 = unlimited | 0 | Excess mode (trigger = limit − margin), PV clipping forecast |
| **Excess Trigger Margin** | How far below the export limit Excess engages (W) | 500W | Excess mode |
| **Battery SOC Min** | Minimum battery SOC for charging (%) | 20% | All modes (with battery) |
| **Battery SOC Target** | Target battery SOC (%) | 80% | Solar Priority, Solar Only |
| **Battery SOC Hysteresis** | SOC hysteresis to prevent oscillation (%) | 3% | Solar Priority, Solar Only |
| **Battery Full SOC** | SOC at/above which the battery counts as full (%) | 97% | Excess mode — zeroes the battery's absorption capacity; also the Smart Plug on-trigger |
| **Battery Max Charge Power** | Maximum battery charging power (W) | 5000W | Excess mode (the battery's share of the absorption capacity) |
| **Battery Max Discharge Power** | Maximum battery discharge power (W) | 5000W | Standard, Solar Priority |
| **Power Buffer** | Safety buffer in Standard mode (W) | 0W | Standard mode |
| **Allow Grid Charging** | Enable/disable grid import | ON | Standard, Solar Priority |
| **Distribution Mode** | How to allocate between loads | Priority | Multi-load |
| **Circuit Group Limit** | Max current per phase for a group of loads (A) | — | Multi-load |

### Off-Grid Sites

All operating modes work on off-grid sites (no grid CT entities configured). The system treats grid current as 0A and derives solar production from inverter output:
- **Series topology**: solar = inverter output - battery power
- **Parallel topology**: solar = inverter output

Standard and Solar Priority modes work identically — the grid portion of available power is simply 0. Solar Only and Excess modes rely on solar production, which is derived from inverter output sensors instead of grid export.

### Load-Level Configuration

| Parameter | Description | Default |
|-----------|-------------|---------|
| **Operating Mode** | Per-load operating mode | Standard (EVSE) / Continuous (Plug) |
| **Min Current** | Minimum charge rate (A) | 6A |
| **Max Current** | Maximum charge rate (A) | 16A |
| **Load Priority** | Priority for distribution (1-10, lower=higher) | 1 |
| **Update Frequency** | How often to recalculate (seconds) | 15s |
| **Charge Pause Duration** | Min time before restarting (minutes) | 3 |

---

## Practical Use Cases

### Scenario 1: No Battery, Maximum Solar Self-Consumption
**Setup:** Solar system without battery, want to use excess solar for EV

**Recommended Settings:**
- **Operating Mode**: Solar Priority
- **Why**: Uses available solar/export with minimum rate guarantee
- **Behavior**: Charges faster when sunny, minimum rate when cloudy

### Scenario 2: Battery System, Protect Home Battery
**Setup:** Battery system, prioritize home battery over EV

**Recommended Settings:**
- **Operating Mode**: Solar Only or Solar Priority
- **Battery SOC Min**: 30%
- **Battery SOC Target**: 80%
- **Why**: Solar Only won't charge EV until battery is satisfied; Solar Priority provides minimum charging

### Scenario 3: Large Solar System, Prevent Excessive Export
**Setup:** Large solar array, significant daily export

**Recommended Settings:**
- **Operating Mode**: Excess
- **Excess Threshold**: 10000W
- **Why**: Only charges when significant excess available, stable charging sessions

### Scenario 4: Two Chargers, One Priority Vehicle
**Setup:** Two chargers, one for main vehicle, one for guest

**Recommended Settings:**
- **Distribution Mode**: Priority
- **Main Charger Priority**: 1
- **Guest Charger Priority**: 2
- **Why**: Main vehicle gets priority, guest gets remainder

### Scenario 5: Mixed Loads — Daily Driver + Pool Heater
**Setup:** EV charger for daily commute, smart plug for pool heater

**Recommended Settings:**
- **EV Operating Mode**: Standard (morning), Solar Priority (daytime)
- **Pool Heater Operating Mode**: Solar Only
- **Distribution Mode**: Priority (EV priority 1, heater priority 2)
- **Why**: Car charges fast when needed, pool heater only uses surplus solar

### Scenario 6: Maximum Speed, Don't Care About Solar
**Setup:** Need fastest possible charging

**Recommended Settings:**
- **Operating Mode**: Standard
- **Why**: Uses all available power sources without limitations

---

## Battery SOC Management

### Hysteresis Explained

Hysteresis prevents rapid switching on/off when battery SOC hovers around thresholds.

**Example with target_soc = 80%, hysteresis = 3%:**
```
Rising:
  75% -> 78% -> 80% -> (triggers "above target")

Falling (once above):
  80% -> 78% -> 77% -> (still "above target")
  77% -> 75% -> (drops below 77% = target - hysteresis)
  Now "below target"
```

**Benefits:**
- Prevents charging oscillation
- Reduces wear on equipment
- More stable charging sessions

---

## Troubleshooting

### Solar Priority Charging Above Minimum

**Symptom:** Solar Priority is charging faster than minimum rate
**Cause:** Solar/export power available
**Solution:** This is correct behavior! Solar Priority uses available solar to prevent grid export

### Solar Priority Not Using Solar

**Symptom:** Solar exporting but Solar Priority charges at minimum
**Check:**
- Is battery below target SOC? (Battery gets priority)
- Is "Allow Grid Charging" disabled? (May limit calculation)
- Check logs for actual current calculation

### Solar Only Not Charging

**With Battery:**
- **Check:** Battery SOC — must be at or above target
- **Check:** Is solar actually producing? (battery charging or exporting?)

**Without Battery:**
- **Check:** Is solar exporting to grid?
- **Check:** Is export > minimum current threshold?

### Excess Mode Not Starting

**Check:**
- Export power vs. configured threshold
- With battery: Is threshold adjusted for battery charging?

### Hub Status Shows "Grid sensors unavailable"

**Cause:** Configured grid CT sensors are returning `unavailable` or `unknown` state.
**Behavior:** The system holds the last known reading for up to 60 seconds. After 60s, all chargers fall to minimum current as a safety measure. Recovery is automatic when sensors come back.

### Hub Status Shows "No power measurement"

**Cause:** No grid CTs, no inverter output entities, and no solar entity are configured. The system has no way to measure power flow.
**Solution:** Configure at least one power measurement source — grid CT entities, inverter output entities, or a solar production entity.

---

## Best Practices

1. **Start with Standard mode** to verify basic operation
2. **Set realistic battery SOC limits** — don't set min too high
3. **Use Power Buffer** in Standard mode if experiencing frequent stops
4. **Monitor for a full day** before adjusting thresholds
5. **Solar Priority** is usually the best general-purpose mode for solar systems
6. **Distribution mode** depends on your specific multi-load needs
7. **Update frequency** of 15s is a good balance between responsiveness and stability
