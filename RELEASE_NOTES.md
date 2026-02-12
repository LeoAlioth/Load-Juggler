# Release Notes

## 2.0.0 (Pre-release)

**BREAKING**: Existing 1.x users must **remove and re-add** the integration. Legacy migrations have been removed.

### Migration
- Entity/device layout changed: chargers are organized into devices using device IDs.
- Charging modes remain site-level (not per-charger), but configuration flow and entities changed.

### New Features
- **Multi-charger support** with 4 distribution modes:
  - Shared (equal split), Priority (highest-priority first), Optimized (smart reduction to fit more chargers), Strict (sequential allocation).
- **Asymmetric inverter support**: flexible power pool across phases for solar, battery, and excess modes.
- **Standard mode battery discharge**: combines grid + solar + battery when SOC >= minimum threshold.
- **2-phase OBC charger support** for vehicles like VW eGolf, eUp, ID.3 base.
- **Power-based charging**: send watts instead of amps (auto-detection or manual selection).
- **Per-phase constraint system** enforcing physical limits across all charger types (1ph, 2ph, 3ph) using constraint dicts with keys A, B, C, AB, AC, BC, ABC.
- 4 charging modes with distinct behaviors:
  - **Standard**: max speed from grid + solar + battery (when SOC >= min).
  - **Eco**: prefer solar, fall back to grid minimum.
  - **Solar**: solar-only (+ battery discharge when SOC > target).
  - **Excess**: charge only when export exceeds configurable threshold.
  - See `CHARGE_MODES_GUIDE.md` for full details.
- 0.5A step sliders for fine-grained current control.
- Updated configuration flow with profile validity modes.

### Architecture
- New pure Python calculation engine (`calculations/`) with no Home Assistant dependencies, enabling standalone testing and debugging.
- 5-step calculation pipeline: site limits, solar available, excess available, target power, distribution.

### Testing
- 53 YAML-based test scenarios covering 1ph/2ph/3ph, battery, asymmetric inverters, mixed-phase, oscillation stability.
- Automated test suite with CI integration (runs on every push to dev/pre-release).

### Other
- Slovenian translation updated.
- Gitea-based release automation for pre-release and release workflows.

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
