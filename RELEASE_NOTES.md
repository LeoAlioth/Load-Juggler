# Release Notes

## 2.0.1 (Pre-release)

**BREAKING**: Existing 1.x users must **remove and re-add** the integration.

### New Features
- **Multi-charger support** with 4 distribution modes (Shared, Priority, Optimized, Strict).
- **Smart Load support**: non-OCPP controllable loads (e.g., granny charger behind a Shelly smart plug) with binary on/off control, configurable power rating, and phase assignment.
- **4 charging modes**: Standard (grid + solar + battery), Eco (solar-first with grid fallback), Solar (solar-only), Excess (threshold-based export charging).
- **Asymmetric inverter support**: flexible power pool across phases.
- **Battery integration**: SOC thresholds, charge/discharge power limits, battery-aware solar derivation.
- **2-phase OBC charger support** (e.g., VW eGolf, eUp, ID.3 base).
- **Charger phase mapping (L1/L2/L3 → A/B/C)** with per-phase constraint enforcement.
- **Power-based charging**: send watts instead of amps via OCPP charge rate unit auto-detection.
- **Dedicated solar power entity**: direct inverter-side solar sensor instead of CT-derived.
- **Entity auto-detection**: battery, solar, inverter, and wiring topology entities for 12 inverter brands.
- **Max import power limiter**: cap grid import independently of breaker rating.
- **HA service actions**: `set_charging_mode`, `set_distribution_mode`, `set_max_current`.
- **Available current display** for idle chargers.
- **EVSE charging status sensor**.

### Bug Fixes
- Fixed charging instability from feedback loop oscillation (current rate limiting + dual-frequency updates).
- Fixed grid CT feedback loop overcorrection when charger reports inflated per-phase draws.

### UX Improvements
- Redesigned configuration flow with contextual help text, entity pickers, and number selectors.
- Phase mapping fields hidden on single-phase sites to prevent misconfiguration.
- Charger names auto-prettified from OCPP entity IDs.
- Phase B/C sensors hidden on single-phase sites.
- Structured debug logging for easier remote troubleshooting.

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
