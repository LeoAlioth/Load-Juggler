# Load Juggler

Intelligent load management for Home Assistant. Dynamically distributes available power across your managed loads — EV chargers (via OCPP), smart plugs, and more — based on solar production, battery state, grid capacity, and per-load operating modes.

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
- **Auto-detection** of sensors, phase mapping, and charger settings
- **OCPP 1.6J** control for EV chargers (Amps or Watts, auto-detected)
- **Relative and absolute OCPP profile modes** for different charger compatibility
- **Current rate limiting** (ramp up/down) for stable operation
- **Failsafe operation** — loads revert to safe defaults if communication fails

## Supported Load Types

| Type | Control Method | Available Modes | Description |
|------|---------------|-----------------|-------------|
| **EVSE** | OCPP 1.6J current/power profiles | Standard, Solar Priority, Solar Only, Excess | EV chargers with variable current control |
| **Smart Plug** | On/off switch | Continuous, Solar Only, Excess | Any device behind a smart plug (heaters, pumps, etc.) |
| **Hot Water Tank** | *Planned* | Solar Only, Excess | Thermostat control with Normal/Boost |
| **SG Ready** | *Planned* | Automatic | 2-relay site-state mapping (Block/Normal/Recommend/Force) |

## Operating Modes

Each load has its own operating mode, set independently. This allows mixing modes across your loads — for example, your daily driver on Standard while a pool heater runs on Solar Only.

### EVSE Modes

- **Standard**: Charges as fast as possible from all available power sources (grid + solar + battery). Ideal for maximum charging speed.
- **Solar Priority**: Charges at minimum current, increases with solar production. Prevents grid export while maintaining minimum charge rate. With battery: graduates charging based on SOC thresholds.
- **Solar Only**: Only charges when sufficient solar power is available. Zero grid import — stops if import would be required.
- **Excess**: Starts charging only when solar export exceeds a configurable threshold. Designed for large solar systems to utilize excess power.

### Smart Plug Modes

- **Continuous**: Always on (when connected).
- **Solar Only**: Turns on only when solar surplus is available.
- **Excess**: Turns on only when export exceeds the configured threshold.

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

## Battery System Support

- **Battery SOC monitoring** — tracks current battery state of charge
- **SOC target management** — respects minimum battery charge levels
- **Intelligent discharge control** — allows battery power to supplement loads when SOC is above target
- **Charge/discharge power limits** — configurable maximum battery charge and discharge rates
- **Grid charging control** — optional switch to allow/disallow charging from grid power
- **SOC hysteresis** — prevents oscillation when battery SOC hovers near thresholds

Battery entities (SOC Target, SOC Min, Allow Grid Charging) are only shown when a battery sensor is configured.

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
   - Select your phase current/power sensors (Phase A required, B and C optional for multi-phase)
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
| Excess export threshold | Solar export threshold for Excess mode (W) | 13000W |
| Invert phases | Flip CT polarity if installed backwards | Off |
| Distribution mode | How to allocate power between loads | Priority |
| Battery SOC entity | Battery state of charge sensor | — |
| Battery power entity | Battery charge/discharge power sensor (W) | — |
| Battery max charge/discharge power | Battery power limits (W) | 5000W |
| Battery SOC hysteresis | SOC change before triggering mode switches (%) | 5% |
| Solar production entity | Dedicated solar power sensor (optional) | — |
| Inverter max power | Total inverter capacity (W) | — |
| Inverter max power per phase | Per-phase inverter limit (W) | — |
| Inverter supports asymmetric | Can inverter balance power across phases | Off |
| Inverter output phase A/B/C entity | Per-phase inverter output sensors (optional) | — |

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
