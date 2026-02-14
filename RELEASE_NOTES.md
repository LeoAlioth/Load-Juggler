# Release Notes

## 2.0.0 (Pre-release)

**BREAKING**: Existing 1.x users must **remove and re-add** the integration. Legacy migrations have been removed.

### Migration
- Entity/device layout changed: chargers are organized into devices using device IDs.
- Charging modes remain site-level (not per-charger), but configuration flow and entities changed.

### New Features
- **Multi-charger support** with 4 distribution modes:
  - Shared (equal split), Priority (highest-priority first), Optimized (smart reduction to fit more chargers), Strict (sequential allocation).
- **Smart plug / relay support**: new device type for non-OCPP controllable loads (e.g., granny charger plugged into a Shelly smart plug). Binary on/off control via `switch.turn_on`/`switch.turn_off`. Configurable power rating, phase assignment, and optional power monitoring sensor. The calculation engine treats plugs like any other charger — the fixed power rating naturally produces binary behavior through the min-current threshold.
- **Dedicated solar power entity**: optional config field that points to a direct solar production sensor (W) instead of deriving solar from `consumption + export`. Improves accuracy for sites with inverter-side metering. Auto-detection for common solar sensor naming patterns.
- **Available current display**: idle chargers now show how much current they would receive if they started charging, giving users visibility into headroom without an active session.
- **Asymmetric inverter support**: flexible power pool across phases for solar, battery, and excess modes.
- **Standard mode battery discharge**: combines grid + solar + battery when SOC >= minimum threshold.
- **2-phase OBC charger support** for vehicles like VW eGolf, eUp, ID.3 base.
- **Power-based charging**: send watts instead of amps with OCPP charge rate unit detection via `GetConfiguration` for `ChargingScheduleAllowedChargingRateUnit`. If detection fails, the field is left empty for the user to choose manually. Detection available during initial setup, reconfigure, and options flows.
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
- Phase handling uses `None` for non-existent phases (vs `0.0` for exists-with-no-load), enabling correct behavior for 1-phase and 2-phase sites without special-case code.
- `SiteContext.num_phases` is a derived property from active consumption phases, eliminating redundant configuration.

### Testing
- 70 YAML-based test scenarios covering:
  - 1-phase / 3-phase with and without battery
  - Asymmetric inverters, mixed-phase chargers, 2-phase OBC
  - All 4 distribution modes, oscillation stability
  - Smart plug scenarios (binary on/off, mixed EVSE + plug)
  - Direct solar entity vs derived solar
  - Available current for idle chargers
- HA integration tests: config flow, end-to-end setup, sensor update cycle, OCPP profile formats, charge pause logic.
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
