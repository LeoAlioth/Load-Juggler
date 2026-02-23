# Release Notes

## 2.0.3 (Pre-release)

**BREAKING**: Existing 1.x users must **remove and re-add** the integration.

### New Features
- **Per-load operating modes**: each charger/load has its own operating mode instead of a site-wide setting. EVSE modes: Standard, Solar Priority, Solar Only, Excess. Smart Load modes: Continuous, Solar Only, Excess.
- **Mixed-mode operation**: run different chargers in different modes simultaneously (e.g., one charger in Standard while another waits for Solar Only).
- **Source-aware dual-pool distribution**: physical (grid + inverter), solar, and excess power pools tracked independently — each charger draws from the pools its mode allows.
- **Multi-charger support** with 4 distribution modes (Shared, Priority, Optimized, Strict).
- **Smart Load support**: non-OCPP controllable loads (e.g., granny charger behind a Shelly smart plug) with binary on/off control, configurable power rating, and phase assignment.
- **Asymmetric inverter support**: flexible power pool across phases.
- **Battery integration**: SOC thresholds, charge/discharge power limits, battery-aware solar derivation.
- **2-phase OBC charger support** (e.g., VW eGolf, eUp, ID.3 base).
- **Charger phase mapping (L1/L2/L3 → A/B/C)** with per-phase constraint enforcement.
- **Power-based charging**: send watts instead of amps via OCPP charge rate unit auto-detection.
- **Dedicated solar power entity**: direct inverter-side solar sensor instead of CT-derived.
- **Per-phase inverter output entities**: optional sensors for each inverter phase with parallel/series wiring topology.
- **Entity auto-detection**: battery, solar, inverter, power monitoring, and wiring topology entities for 12 inverter brands.
- **Max import power limiter**: cap grid import independently of breaker rating.
- **HA service actions**: `set_operating_mode`, `set_distribution_mode`, `set_max_current`, `set_min_current`.
- **Available current display** for idle chargers.
- **EVSE charging status sensor** with mode-aware status messages (Battery Priority, Insufficient Solar, No Excess, etc.).
- **Solar/Excess grace period**: configurable hold-at-minimum timer before pausing when conditions drop, preventing rapid on/off cycling.
- **Auto-detect grid CT inversion**: correlates charger draw vs grid current direction, fires persistent notification after repeated inverted readings.
- **Auto-detect phase mapping**: correlates charger draw vs per-phase grid deltas, notifies on wiring mismatch (opt-in).
- **Auto-detect OCPP meter sample interval**: uses charger's `MeterValueSampleInterval` as default update frequency.
- **Auto-detect smart plug power monitor**: discovers power sensors for Shelly, Sonoff, Tasmota, Kasa, Tuya plugs.

### Bug Fixes
- Fixed charging instability from feedback loop oscillation (EMA smoothing + Schmitt trigger dead band + dual-frequency updates).
- Fixed grid CT feedback loop overcorrection when charger reports inflated per-phase draws (per-phase draw clamping).
- Fixed battery power sensor false-positive detection matching phone batteries.
- Fixed entity state lookup using shared `hass.data` store instead of entity ID guessing.
- Fixed battery SOC target/min sliders not feeding into calculation engine.
- Fixed EVSE min/max current sliders missing value clamping.
- Fixed connector status "Finishing"/"Faulted" treated as active — now correctly stops allocation and skips OCPP profiles.
- Fixed entity selector clearing (`suggested_value` instead of `default` so X button truly clears).
- Fixed options flow Submit → Next button on non-final steps.
- Fixed config entry not reloading on options change (stale battery sliders persisting).

### UX Improvements
- Redesigned configuration flow with contextual help text, entity pickers, and number selectors.
- Phase mapping fields hidden on single-phase sites to prevent misconfiguration.
- Charger names auto-prettified from OCPP entity IDs.
- Phase B/C sensors hidden on single-phase sites.
- Hub sensor renames: shorter, consistent naming (Current X Power / Available X Power), added Current Solar Power sensor.
- Structured debug logging with human-readable charger names, raw+smoothed value display.
- Charge pause duration in minutes (was seconds) for consistency.

---

## 1.2.1

### Improvements
- Added configurable power buffer for grid protection.
- Default charging mode selection.
- Single-phase operation support.
- Configurable OCPP profile stack level.
- Fixed charging instability on standard charge modes.
- Fixed missing entities during updates.
- Input number slider definitions updated.

---

## 1.1.0

### New Features
- **Excess charging mode**: charge only when total export exceeds a configurable power threshold.
- **Battery configuration**: multi-step config flow with battery SOC entity, power entity, and min/target SOC.
- **Grid charging toggle**: switch to allow/disallow grid charging when battery is present.
- **Grid power entities**: support for selecting power entities (W) instead of only current (A) for grid measurements.

### Improvements
- Additional pattern matching for Deye CT current measurements.
- Added None/NoneType safety checks.
- Config flow refactored into multiple steps.
- Updated config descriptions.

---

## 1.0.7

### Improvements
- Faster detection/refreshing of number of charging phases.
- Fixed initial setup and added reset notice on first configuration.
- Updated English descriptions.
- Added Slovenian translation.

---

## 1.0.0

First public HACS release.

### Features
- OCPP 1.6J charger management via Home Assistant.
- Dynamic current adjustment based on solar production and grid capacity.
- Solar and Eco charging modes.
- Automatic charging phase detection.
- Pause timer functionality.
- Reset/reconfigure button (clears OCPP profiles).
- Minimum current start to prevent oscillation from unknown phase count.
