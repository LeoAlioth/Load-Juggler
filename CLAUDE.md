# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Dynamic OCPP EVSE is a Home Assistant custom component that provides intelligent EV charging control via OCPP 1.6J protocol. It dynamically adjusts charging current based on solar production, battery state, grid capacity, and user-defined charging modes.

**Key Capabilities:**

- Multiple charging modes (Standard, Eco, Solar, Excess)
- Multi-charger support with priority-based distribution
- Battery integration with SOC thresholds
- Phase-aware handling (1-phase, 2-phase, 3-phase installations)
- Symmetric and asymmetric inverter support

## Development Commands

### Running Tests

```bash
# Run all test scenarios
python tests/run_tests.py tests/scenarios

# Run only verified scenarios
python tests/run_tests.py --verified tests/scenarios

# Run only unverified scenarios
python tests/run_tests.py --unverified tests/scenarios

# Run specific scenario by name
python tests/run_tests.py "scenario-name"
```

**Test results** are automatically written to `tests/test_results.log` after each run.
**Test procedure** Do not combine multiple shell commands to one line. Always run one test at a time.

### Linting and Type Checking

Development dependencies are in `requirements_dev.txt`:

```bash
pip install -r requirements_dev.txt

# Code formatting
black custom_components/dynamic_ocpp_evse

# Linting
flake8 custom_components/dynamic_ocpp_evse
pylint custom_components/dynamic_ocpp_evse

# Type checking
mypy custom_components/dynamic_ocpp_evse
```

## Architecture

### Core Design Principle: Generality Over Special Cases

**CRITICAL**: Always strive for the most general solution possible. Minimize unnecessary distinctions.

- **Don't create separate code paths** for 1-phase vs 3-phase unless absolutely necessary
- **Use per-phase calculations universally** instead of creating special logic for each site type
- **The same algorithm should handle all cases**: 1-phase, 2-phase, 3-phase, symmetric, asymmetric
- **Make use of helper functions** for readability and error reduction

**Example**: Instead of `if site.num_phases == 3:` and branching, use per-phase arrays `[A, B, C]` where unused phases are 0.

### Multi-Phase Constraint Principle

**CRITICAL**: ALL calculation functions must return a constraint dict with keys:

- `'A'`, `'B'`, `'C'` - Single-phase limits
- `'AB'`, `'AC'`, `'BC'` - Two-phase limits (for 2-phase chargers)
- `'ABC'` - Three-phase limit (total)

This properly enforces constraints for every charger configuration:

- 1-phase charger on phase A: Uses `constraints['A']`
- 2-phase charger on AB: Uses `min(constraints['A'], constraints['B'], constraints['AB'])`
- 3-phase charger: Uses `min(constraints['A'], constraints['B'], constraints['C'], constraints['ABC'])`

**Why**: Physical reality - inverters and breakers have limits for EACH phase combination, not just individual phases.

### Code Structure

```text
custom_components/dynamic_ocpp_evse/
├── __init__.py                    # HA component initialization
├── manifest.json                  # Component metadata
├── const.py                       # Constants and defaults
├── config_flow.py                 # HA configuration flow
├── dynamic_ocpp_evse.py          # Core OCPP charger manager
├── [button|number|select|sensor|switch].py  # HA entities
├── calculations/                  # Core calculation logic (PURE PYTHON - no HA dependencies)
│   ├── models.py                  # Data models (SiteContext, ChargerContext)
│   ├── context.py                 # Context builder (HA → models)
│   ├── target_calculator.py       # Main calculation engine
│   ├── max_available.py           # Max available power calculations
│   ├── utils.py                   # Utility functions
│   └── modes/                     # Charging mode implementations
│       ├── base.py
│       ├── standard.py
│       ├── eco.py
│       ├── solar.py
│       └── excess.py
└── translations/                  # Localization files
```

**Important**: The `calculations/` directory contains pure Python code with NO Home Assistant dependencies. This enables direct testing without mocking HA.

### Calculation Flow

The calculation engine follows a 5-step process (see `target_calculator.py`):

```text
0. Refresh SiteContext (done externally in HA integration)
   ↓
1. Calculate absolute site limits (per-phase physical constraints)
   → _calculate_site_limit()
     ├─ _calculate_grid_limit()      (grid capacity based on breaker rating)
     ├─ _calculate_inverter_limit()  (solar + battery for Standard mode)
     └─ _sum_constraint_dicts()      (combines grid + inverter for Standard mode)
   ↓
2. Calculate solar available power (includes battery charge/discharge)
   → _calculate_solar_available()
   ↓
3. Calculate excess available power (Excess mode only)
   → _calculate_excess_available()
   ↓
4. Determine target power based on charging mode
   → _determine_target_power()
   ↓
5. Distribute power among chargers
   → _distribute_power()
```

### Data Models

**SiteContext** (`calculations/models.py`) - Represents the entire electrical site:

- Electrical: voltage, num_phases, main_breaker_rating
- Consumption: phase_a/b/c_consumption, phase_a/b/c_export
- Solar: solar_production_total
- Battery: battery_soc, battery_soc_min, battery_soc_target, battery_max_charge/discharge_power
- Inverter: inverter_max_power, inverter_max_power_per_phase, inverter_supports_asymmetric
- Charging: charging_mode, distribution_mode, chargers[]

**ChargerContext** (`calculations/models.py`) - Represents a single EVSE:

- Config: entity_id, min_current, max_current, phases, car_phases, priority
- Status: connector_status (Available, Charging, etc.)
- Phase tracking: active_phases_mask ("A", "B", "C", "AB", "BC", "AC", "ABC")
- Current: l1_current, l2_current, l3_current (actual draw)
- Calculated: target_current (output of calculation)

### Asymmetric vs Symmetric Inverters

**Symmetric Inverter** (`inverter_supports_asymmetric=False`):

- Solar/battery power is fixed per-phase
- Each phase operates independently
- 3-phase chargers limited by minimum available phase

**Asymmetric Inverter** (`inverter_supports_asymmetric=True`):

- Solar/battery power can be distributed across any phase
- Inverter can balance load dynamically
- Total power pool available (not per-phase limited)

**Important**: Regardless of inverter type, chargers are physically connected to specific phases and can only draw from those phases. The inverter asymmetric capability affects power SUPPLY flexibility, not charger DRAW flexibility.

### Phase-Specific Allocation

When chargers have explicit phase assignments (e.g., `connected_to_phase: "B"`):

- Triggers per-phase distribution logic (`_distribute_power_per_phase()`)
- Each phase is allocated independently
- 3-phase chargers limited by minimum available phase

## Testing Framework

Tests use **REAL production code** - no duplicates or mocks. Test scenarios are defined in YAML files:

```yaml
scenarios:
  - name: "test-name"
    description: "What this tests"
    verified: true  # Manually verified by maintainer
    iterations: 1   # For oscillation tests
    site:
      voltage: 230
      num_phases: 3
      charging_mode: Solar
      # ... all site parameters
    chargers:
      - entity_id: "charger_1"
        min_current: 6
        max_current: 16
        phases: 3
        priority: 1
        connected_to_phase: "A"  # Optional explicit phase assignment
    expected:
      charger_1:
        target: 10.0  # Expected current in Amps
```

Test scenarios are organized in `tests/scenarios/`:

- `test_scenarios_1ph.yaml` - Single-phase scenarios
- `test_scenarios_1ph_battery.yaml` - Single-phase with battery
- `test_scenarios_3ph.yaml` - Three-phase scenarios
- `test_scenarios_3ph_battery.yaml` - Three-phase with battery

### Running HA Integration Tests

Integration tests use `pytest-homeassistant-custom-component` and run under WSL (HA core requires `fcntl`, Unix-only):

```bash
# All integration tests
wsl -- bash -c "source ~/ha-test-venv/bin/activate && cd /mnt/c/Users/anzek/Documents/Dynamic_OCPP_EVSE && python -m pytest dev/tests/test_init.py dev/tests/test_config_flow.py dev/tests/test_config_flow_e2e.py dev/tests/test_sensor_update.py -v"

# Individual test file
wsl -- bash -c "source ~/ha-test-venv/bin/activate && cd /mnt/c/Users/anzek/Documents/Dynamic_OCPP_EVSE && python -m pytest dev/tests/test_sensor_update.py -v"
```

**Integration test files:**
- `test_init.py` — Setup, teardown, migration (v1→v2, v2.0→v2.1)
- `test_config_flow.py` — Config flow step navigation and validation
- `test_config_flow_e2e.py` — Full hub/charger creation flows, options flow, discovery
- `test_sensor_update.py` — Sensor initialization, update cycle, OCPP calls, charge pause, profile formats

## Current Development Status

**IMPORTANT**: we are developing a 2.0 version. Disregard any backwards compatibility. No migration processes created.

**ERRORS**: a developer will paste any new problems into  dev/ISSUES.md
When working on problems, alway look if there is anytinhg in that file, analyze it, and note in in this file in a numbered list with checkboxes, so we can track its progress when working on solving it. Once it is noted in this file, delete it from ISSUES.md

**Multi-Phase Constraint System**: ✅ Implemented

The codebase uses constraint dicts with all phase combinations ('A', 'B', 'C', 'AB', 'AC', 'BC', 'ABC') to properly enforce physical constraints for 1-phase, 2-phase, and 3-phase chargers.

**Recent Major Fix** (2026-02-12): Asymmetric inverter support for solar/battery/excess modes

- Single-phase chargers on asymmetric systems can now access the full flexible power pool
- Symmetric inverters preserved with per-phase calculations

See `dev/tests/test_results.log` for the latest calculation test run output.

**Known Issues**

1. **Site entities not do not get populated with values at all**. I am first trying to see the sites values without any chargers, as that should also work.

2. **Charge mode selector location**: ✅ RESOLVED - Verified in `select.py` that charging_mode and distribution_mode selectors are correctly placed at hub level only (ENTRY_TYPE_HUB), not at charger level.
4. **Site info sensor status**: ✅ RESOLVED - Changed `DynamicOcppEvseHubSensor.state` to return `0.0` instead of `None` when no data is available, preventing "unknown" state display in HA.
5. **Attribute accessibility**: ✅ RESOLVED - Split site-level attributes into individual sensors for better HA UI visibility:
   - `DynamicOcppEvseBatterySocSensor`
   - `DynamicOcppEvseBatteryPowerSensor`
   - `DynamicOcppEvseAvailableBatteryPowerSensor`
   - `DynamicOcppEvseTotalSiteAvailablePowerSensor`
   - `DynamicOcppEvseNetSiteConsumptionSensor`
6. **TimerEntity import crash**: ✅ RESOLVED - `homeassistant.components.timer.TimerEntity` doesn't exist. Deleted `timer.py`, replaced with internal `datetime`-based pause tracking in sensor.

**Fixes Applied** (2026-02-13)

1. **dynamic_ocpp_evse.py**: Added missing imports (`CONF_EVSE_MINIMUM_CHARGE_CURRENT`, `CONF_EVSE_MAXIMUM_CHARGE_CURRENT`, `CONF_CHARGER_PRIORITY`, `CONF_PHASES`, `CONF_ENTITY_ID`, `DEFAULT_MIN_CHARGE_CURRENT`, `DEFAULT_MAX_CHARGE_CURRENT`, `DEFAULT_CHARGER_PRIORITY`)
2. **sensor.py**: Fixed `DynamicOcppEvseHubSensor.state` to return `0.0` instead of `None`
3. **sensor.py**: Added individual site-level sensors to replace compound attributes in extra_state_attributes
4. **timer.py**: Deleted — `TimerEntity` does not exist as a public HA API (`homeassistant.components.timer` is a user helper, not a base class). Charge pause timer replaced with internal `datetime` tracking in `sensor.py`.
5. **__init__.py**: Removed broken `TimerEntity` import and `"timer"` from charger platform setup/unload lists

**Test Status**: 90 tests passing (100%) - as of 2026-02-13
- 52 calculation scenario tests ✅
- 16 config flow tests (test_config_flow.py + test_config_flow_e2e.py) ✅
- 8 init/migration tests ✅
- 14 sensor update cycle tests ✅

### Recent Changes (2026-02-12)

- **dev/tests/test_config_flow_validation.py**: Added 5 validation test cases including edge cases for min/max current validation
- **custom_components/dynamic_ocpp_evse/helpers.py**: Added `validate_charger_settings()` function for pure Python config validation
- **custom_components/dynamic_ocpp_evse/config_flow.py**: Replaced inline `_validate_charger_settings()` with import from helpers; implemented `_build_hub_grid_schema()` and `_build_hub_battery_schema()` reusable field builders to reduce schema duplication

## Common Pitfalls

1. **Asymmetric vs Symmetric confusion**: Remember inverter capability affects SUPPLY, not charger DRAW
2. **Per-phase vs total power**: Track carefully whether working with per-phase (A) or total (A×3)
3. **Battery priority**: Battery charges BEFORE EVs when SOC < target (Standard mode being the exception)
4. **Minimum current**: Chargers need ≥ min_current or get 0 (can't charge below minimum)
5. **Phase assignment defaults**: Don't default to "A" - only set when explicitly specified
6. **Legacy code**: This is version 2.0.0 - legacy compatibility should removed as users are expected to reconfigure the integration

## Charging Modes

1. **Standard**: Maximum charging speed from grid + solar + battery (when SOC >= min)
   - Includes ALL available power sources simultaneously
   - Battery discharge allowed when `battery_soc >= battery_soc_min`
   - No solar requirement - works day and night
2. **Eco**: Match charging speed with solar production when at target SOC, but continue slow charging even without sufficient solar
3. **Solar**: Only use solar power (+ battery discharge if SOC > target)
   - Battery discharge allowed when `battery_soc > battery_soc_target`
4. **Excess**: Only charge when export exceeds threshold (with 15-minute continuation after threshold drop)

## Distribution Modes

1. **Shared**: Two-pass (min first, then split remainder equally)
2. **Priority**: Two-pass (min first, then remainder by priority)
3. **Optimized**: Smart sequential - charge one at a time but reduce higher priority charger to consume all available power
4. **Strict**: Simple sequential (priority 1 gets all, then 2, etc.)

## Development Guidelines

1. **Understand the Flow**: Always trace through the 5-step calculation process
2. **Pure Python**: `calculations/` directory has no HA dependencies for testability
3. **Data Models**: Use SiteContext and ChargerContext - don't pass raw values
4. **Logging**: Use `_LOGGER.debug()` extensively for troubleshooting
5. **Test First**: Run relevant tests before and after changes
6. **Helper Functions**: Prefer helper functions over inline logic for maintainability

### Adding New Features

1. **Charging Mode**: Add to `calculations/modes/`, inherit from `base.py`
2. **Distribution Mode**: Add to `target_calculator.py` as `_distribute_<mode>()`
3. **Test Scenarios**: Create YAML scenarios in `tests/scenarios/`
4. **Documentation**: Update CHARGE_MODES_GUIDE.md, README.md

## Integration with Home Assistant

The `calculations/` directory is pure Python and can be imported/tested independently. The HA integration:

1. **context.py**: Builds SiteContext from HA entities
2. **dynamic_ocpp_evse.py**: Manages OCPP charger connections, calls calculation engine
3. **Entities** (button.py, number.py, etc.): Expose controls and sensors to HA UI

## Debugging

1. **Enable verbose logging** in HA: `custom_components.dynamic_ocpp_evse: debug`
2. **Run specific test**: `python tests/run_tests.py "test-name"`
3. **Check calculation steps**: Each step logs its output (site_limit, solar_available, target_power, etc.)
4. **Per-phase values**: Log phase_a/b/c_export, consumption, available

## Useful Resources

- OCPP 1.6J Specification: <https://www.openchargealliance.org/>
- Home Assistant Developer Docs: <https://developers.home-assistant.io/>
- YAML Test Scenarios: `tests/scenarios/*.yaml`
- Charging Modes Guide: `CHARGE_MODES_GUIDE.md`
- Release notes `RELEASE_NOTES.md`
