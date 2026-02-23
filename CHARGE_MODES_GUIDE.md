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
7. [Configuration Parameters](#configuration-parameters)

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
| **Excess** | Charge when export > threshold | Battery-aware threshold charging | No | No (until 97-98% SOC) |

### Smart Plug Modes

| Mode | Behavior |
|------|----------|
| **Continuous** | Always on |
| **Solar Only** | On only when solar surplus is available |
| **Excess** | On only when export exceeds threshold |

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

**Available for:** EVSE

### Purpose
Economical charging that prioritizes solar production and minimizes grid export while maintaining a minimum charging rate. Formerly known as "Eco" mode.

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

**Behavior:**
- Battery-aware dynamic threshold
- Adjusts threshold based on battery state
- Complex threshold calculation:

```
If battery_soc < target_soc:
    threshold = base_threshold + battery_max_charge_power
Else:
    threshold = base_threshold

If battery_soc >= 98%:
    Behave like Solar Only mode (match solar production)
```

**Why adjust threshold?**
- When battery is below target: Allow battery to charge from solar first
- Threshold increased by battery charging capacity
- Prevents EV from competing with battery for solar

**When to use:**
- Battery installed and want smart coordination
- Prevent excessive export while respecting battery needs
- Charge EV only when battery is satisfied OR excess is very high

**Example Scenarios:**

*Scenario 1: Battery low*
```
Battery SOC: 60% (below target 80%)
Battery Max Charge: 5000W
Base Threshold: 13000W
Effective Threshold: 18000W
Current Export: 16000W
Result: No charging (battery needs charging first)
```

*Scenario 2: Battery at target*
```
Battery SOC: 80% (at target)
Base Threshold: 13000W
Effective Threshold: 13000W
Current Export: 14000W
Result: Start charging at available current
```

*Scenario 3: Battery nearly full*
```
Battery SOC: 98%
Current Export: 8000W
Result: Charge at solar rate (like Solar Only mode)
```

---

## Configuration Parameters

### Hub-Level Configuration

| Parameter | Description | Default | Used By |
|-----------|-------------|---------|---------|
| **Main Breaker Rating** | Maximum current per phase (A) | 25A | All modes |
| **Phase Voltage** | Voltage per phase (V) | 230V | All modes |
| **Max Import Power** | Maximum grid import power (W) | - | All modes |
| **Excess Export Threshold** | Threshold for Excess mode (W) | 13000W | Excess mode |
| **Battery SOC Min** | Minimum battery SOC for charging (%) | 20% | All modes (with battery) |
| **Battery SOC Target** | Target battery SOC (%) | 80% | Solar Priority, Solar Only |
| **Battery SOC Hysteresis** | SOC hysteresis to prevent oscillation (%) | 3% | Solar Priority, Solar Only |
| **Battery Max Charge Power** | Maximum battery charging power (W) | 5000W | Excess mode |
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
