# CLINE.md - AI Development Guide for Dynamic OCPP EVSE

This document describes the Dynamic OCPP EVSE repository structure, architecture, and development guidelines to help AI assistants (like Cline) effectively develop and maintain this project.

## Design Principles

### Generality Over Special Cases
**⭐ CRITICAL: Always strive for the most general solution possible. Minimize unnecessary distinctions.**

- **Don't create separate code paths** for 1-phase vs 3-phase unless absolutely necessary
- **Don't distinguish** between "explicit" and "default" configurations - treat them the same
- **Use per-phase calculations universally** instead of creating special logic for each site type
- **The same algorithm should handle all cases**: 1-phase, 2-phase, 3-phase, symmetric, asymmetric
- **Make use of helper functios** make code more readable, maintainable and most importantly reduces errors

**Example**: Instead of checking `if site.num_phases == 3:` and branching, use per-phase arrays `[A, B, C]` where unused phases are 0. This handles all cases uniformly.

**Why**: Less code, fewer bugs, easier to maintain, more predictable behavior.

### Multi-Phase Constraint Principle  
**⭐ CRITICAL: Track constraints for ALL phase combinations using a constraint dict.**

**ALL calculation functions** must return a constraint dict with keys:
- `'A'`, `'B'`, `'C'` - Single-phase limits
- `'AB'`, `'AC'`, `'BC'` - Two-phase limits (for 2-phase chargers)
- `'ABC'` - Three-phase limit (total)

**Why this matters:**
- **1-phase charger on phase A**: Uses `constraints['A']`
- **2-phase charger on AB**: Uses `min(constraints['A'], constraints['B'], constraints['AB'])`
- **3-phase charger**: Uses `min(constraints['A'], constraints['B'], constraints['C'], constraints['ABC'])`

This properly enforces the Dual Constraint Principle for **every** charger configuration:
- Single-phase limits are respected
- Multi-phase combinations have independent limits
- Total system limit (ABC) is always enforced

**Example**: 
```python
constraints = {
    'A': 25.0,  # Phase A: 25A available
    'B': 25.0,  # Phase B: 25A available  
    'C': 25.0,  # Phase C: 25A available
    'AB': 40.0,  # Phases A+B combined: 40A max (not 50A!)
    'AC': 40.0,  # Phases A+C combined: 40A max
    'BC': 40.0,  # Phases B+C combined: 40A max
    'ABC': 52.0 # All phases: 52A total max (12kW / 230V)
}
```

A 3-phase charger drawing 20A per phase (60A total) would violate the ABC constraint (52A), even though individual phases are fine.

**Why**: Physical reality - inverters and breakers have limits for EACH phase combination, not just individual phases.

### Remove legacy code and compatibility with previous version
**Why**: we are working on a new version, that is expected to be needed to be reconfigured. So this is essentially a fresh start.

## Project Overview

**Dynamic OCPP EVSE** is a Home Assistant custom component that provides intelligent EV charging control via OCPP 1.6J protocol. It dynamically adjusts charging current based on solar production, battery state, grid capacity, and user-defined charging modes.

### Key Features
- **Multiple Charging Modes**: Standard, Eco, Solar, Excess
- **Multi-Charger Support**: Priority-based, Shared, Strict, Optimized distribution
- **Battery Integration**: Respects battery SOC thresholds and charge/discharge limits
- **Phase-Aware**: Handles 1-phase and 3-phase installations, symmetric and asymmetric inverters
- **Per-Phase Allocation**: Supports single-phase chargers on specific phases (A, B, or C)
- **2-Phase OBC Support**: VW eGolf, eUp, ID.3 base, Seat, Škoda, Cupra (implementation in progress)

## Repository Structure

```
Dynamic_OCPP_EVSE/
├── custom_components/dynamic_ocpp_evse/
│   ├── __init__.py                    # HA component initialization
│   ├── manifest.json                  # Component metadata
│   ├── const.py                       # Constants and defaults
│   ├── config_flow.py                 # HA configuration flow
│   ├── dynamic_ocpp_evse.py          # Core OCPP charger manager
│   ├── services.yaml                  # HA service definitions
│   ├── strings.json                   # UI strings
│   ├── button.py                      # HA button entities
│   ├── number.py                      # HA number entities
│   ├── select.py                      # HA select entities
│   ├── sensor.py                      # HA sensor entities
│   ├── switch.py                      # HA switch entities
│   ├── calculations/                  # Core calculation logic (pure Python)
│   │   ├── __init__.py
│   │   ├── models.py                  # Data models (SiteContext, ChargerContext)
│   │   ├── context.py                 # Context builder (HA → models)
│   │   ├── target_calculator.py       # Main calculation engine
│   │   ├── max_available.py           # Max available power calculations
│   │   ├── utils.py                   # Utility functions
│   │   └── modes/                     # Charging mode implementations
│   │       ├── base.py
│   │       ├── standard.py
│   │       ├── eco.py
│   │       ├── solar.py
│   │       └── excess.py
│   └── translations/                  # Localization files
│       ├── en.json
│       └── sl.json
├── tests/                             # Test suite
│   ├── run_tests.py                   # Test runner (uses REAL production code)
│   ├── scenarios/                     # YAML test scenarios
│   │   ├── test_scenarios_1ph.yaml
│   │   ├── test_scenarios_1ph_battery.yaml
│   │   ├── test_scenarios_3ph.yaml
│   │   └── test_scenarios_3ph_battery.yaml
│   └── CSV_GRAPHING_GUIDE.md         # Guide for CSV output analysis
├── README.md                          # User documentation
├── CHARGE_MODES_GUIDE.md             # Charging modes explained
├── CLINE.md                          # This file - AI development guide
└── LICENSE
```

## Architecture

### Calculation Flow

The calculation engine follows a clear 5-step process (see `target_calculator.py`):

```
0. Refresh SiteContext (done externally in HA integration)
   ↓
1. Calculate absolute site limits (per-phase physical constraints)
   → _calculate_site_limit()
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

**SiteContext** (`calculations/models.py`):
- Electrical: voltage, num_phases, main_breaker_rating
- Consumption: phase_a/b/c_consumption, phase_a/b/c_export
- Solar: solar_production_total
- Battery: battery_soc, battery_soc_min, battery_soc_target, battery_max_charge/discharge_power
- Inverter: inverter_max_power, inverter_max_power_per_phase, inverter_supports_asymmetric
- Charging: charging_mode, distribution_mode, chargers[]

**ChargerContext** (`calculations/models.py`):
- Config: entity_id, min_current, max_current, phases, car_phases, priority
- Status: connector_status (Available, Charging, etc.)
- Phase tracking: active_phases_mask ("A", "B", "C", "AB", "BC", "AC", "ABC")
- Current: l1_current, l2_current, l3_current (actual draw)
- Calculated: target_current (output of calculation)

### Charging Modes

1. **Standard**: Maximum charging speed, can charge from grid, battery and solar
2. **Eco**: match charging speed with solar production whetn at target soc, but still keep slowly charging even if not enough solar is available
3. **Solar**: Only use solar power (+ battery discharge if SOC > target)
4. **Excess**: Only charge when export exceeds threshold

### Distribution Modes

1. **Shared**: Two-pass (min first, then split remainder equally)
2. **Priority**: Two-pass (min first, then remainder by priority)
3. **Optimized**: Smart Sequential charge one at a time but reduce higher priority charger if needed to consume all available power based on charging modes.
4. **Strict**: simple Sequential (priority 1 gets all, then 2, etc.)

### Asymmetric vs Symmetric Inverters

**Symmetric Inverter** (`inverter_supports_asymmetric=False`):
- Solar/battery power is fixed per-phase
- Each phase operates independently
- 3-phase chargers limited by minimum available phase
- Calculations use per-phase values

**Asymmetric Inverter** (`inverter_supports_asymmetric=True`):
- Solar/battery power can be distributed across any phase
- Inverter can balance load dynamically
- Total power pool available (not per-phase limited)
- Calculations use total values, then distribute

**Important**: Regardless of inverter type, chargers are still physically connected to specific phases and can only draw from those phases. The inverter asymmetric capability affects power SUPPLY flexibility, not charger DRAW flexibility.

### Phase-Specific Allocation

When chargers have explicit phase assignments (e.g., `connected_to_phase: "B"`):
- Triggers per-phase distribution logic (`_distribute_power_per_phase()`)
- Each phase is allocated independently
- 3-phase chargers limited by minimum available phase
- Currently uses raw phase export values (⚠️ Known issue: doesn't include battery discharge for asymmetric inverters)

## Testing

### Test Framework

The test suite (`tests/run_tests.py`) uses **REAL production code** - no duplicates or mocks. Tests are defined in YAML files with:

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
        connected_to_phase: "A"  # Optional, for explicit phase assignment
    expected:
      charger_1:
        target: 10.0  # Expected current in Amps
```

### Running Tests

```bash
# Run all scenarios
python tests/run_tests.py tests/scenarios

# Run only verified scenarios
python tests/run_tests.py --verified tests/scenarios

# Run only unverified scenarios
python tests/run_tests.py --unverified tests/scenarios

# Run single scenario
python tests/run_tests.py "scenario-name"
```

**Test Output**: All test runs automatically output results to `tests/test_results.log` for review. This log file contains the complete test summary and is updated with each test run.

### Test Status

Current verified test results: **28/29 passing (97%)**

Failing test:
- `1ph-1c-solar-prio-with-bat-oscillation`: Single-phase charger with explicit phase on asymmetric inverter system - per-phase distribution doesn't account for battery discharge

## Current Development Status

### Recently Completed
✅ Fixed phase mapping for chargers with explicit phase assignments
✅ Added verified/unverified test filtering (`--verified`, `--unverified` flags)
✅ Implemented per-phase distribution for single-phase chargers on specific phases
✅ Dual-path distribution (standard vs per-phase) based on explicit phase assignments
✅ Handles mixed 1-phase/3-phase charger scenarios

### Completed Refactoring

✅ **Phase 1 & 2 Refactoring: COMPLETE** - Design principle violations resolved:

1. ✅ **Excessive Phase-Specific Branching** - All functions now use per-phase arrays uniformly
2. ✅ **Semantic Switching** - Dual constraint tuples `(per_phase[], total)` handle all cases
3. ✅ **Eco Mode Logic** - Simplified and consistent
4. ✅ **Helper Functions** - No special cases

**Infrastructure Status**: ✅ Complete
- All calculation functions return dual constraint tuples
- Per-phase arrays used throughout
- Both constraints tracked and enforced

### Current Issues

🔴 **Phase 3: Mode Calculation Logic Refinement** (8 failing tests - all 3-phase + battery scenarios)

**Problem Pattern**: Chargers receiving `max_current (16A)` instead of constrained values in battery scenarios.

**Failing Tests** (all 3-phase with asymmetric inverters + battery):
1. `3ph-2c-solar-prio-with-bat-shared` - expects 8A/0A, getting 16A/8A
2. `3ph-1c-solar-prio-with-bat-unbalanced` - expects 9.3A, getting 28A
3. `3ph-1c-eco-prio-with-bat-high-soc` - expects 10A, getting 16A  
4. `3ph-1c-eco-prio-with-bat-mid-soc` - expects 6A, getting 16A
5. `3ph-3c-standard-prio-with-bat-normal` - chargers 2&3 wrong values
6. `3ph-2c-solar-prio-with-bat-mixed-phases` - expects 8A/6A, getting 16A/14A
7. `3ph-2c-eco-prio-with-bat-no-solar` - expects 6A/6A, getting 16A/16A
8. `3ph-1c-solar-prio-with-bat-oscillation` - expects 8A, getting 16A

**Phase 2C: Multi-Phase Constraint System Implementation Plan**

🔄 **IN PROGRESS** - Refactoring to use constraint dicts for all phase combinations

**Implementation Steps**:

1. **Update `_calculate_site_limit()`** to return constraint dict:
   ```python
   return {
       'A': phase_a_limit,
       'B': phase_b_limit,
       'C': phase_c_limit,
       'AB': min(phase_a_limit + phase_b_limit, inverter_total_limit),
       'AC': min(phase_a_limit + phase_c_limit, inverter_total_limit),
       'BC': min(phase_b_limit + phase_c_limit, inverter_total_limit),
       'ABC': total_limit  # Already accounts for inverter_max_power
   }
   ```

2. **Update `_calculate_solar_available()`** to return constraint dict:
   - Calculate per-phase solar after consumption & battery
   - Calculate all 2-phase combinations (sum of individuals)
   - Calculate total (ABC) with inverter limit applied

3. **Update `_calculate_excess_available()`** to return constraint dict:
   - Similar structure to solar_available

4. **Update `_determine_target_power()`** to work with constraint dicts:
   - Accept constraint dicts as input
   - Return constraint dict as output
   - Each mode calculates constraints for all phase combinations

5. **Update `_distribute_power_per_phase_priority()`** to use constraint dict:
   - For each charger, look up the appropriate constraint key based on `active_phases_mask`
   - Example: 3-phase charger uses `min(constraints['A'], constraints['B'], constraints['C'], constraints['ABC'])`
   - Example: 2-phase AB charger uses `min(constraints['A'], constraints['B'], constraints['AB'])`
   - Example: 1-phase A charger uses `constraints['A']`

6. **Key Insight**: The constraint dict naturally handles asymmetric vs symmetric:
   - **Asymmetric**: All phase combinations get the total pool divided appropriately
   - **Symmetric**: Each combination is limited by actual per-phase availability

**Expected Outcome**: All 8 failing battery tests should pass as we now properly enforce all constraints.

**Status**: 🔄 Implementation starting

### Planned Features
🔄 **2-Phase OBC Support**: Many European EVs (VW eGolf, eUp, ID.3 base, Seat, Škoda, Cupra) use 2-phase onboard chargers. Need to:
  - Support `phases=2` in ChargerContext
  - Add `active_phases_mask` patterns: "AB", "BC", "AC"
  - Create test scenarios for 2-phase chargers on 3-phase sites
  - Ensure distribution logic handles 2-phase correctly

## Development Guidelines

### Making Changes

1. **Understand the Flow**: Always trace through the 5-step calculation process
2. **Test First**: Check existing tests, understand what they expect
3. **Pure Python**: `calculations/` directory is pure Python (no HA dependencies) for testability
4. **Data Models**: Use SiteContext and ChargerContext - don't pass raw values
5. **Logging**: Use `_LOGGER.debug()` extensively for troubleshooting

### Adding New Features

1. **Charging Mode**: Add to `calculations/modes/`, inherit from `base.py`
2. **Distribution Mode**: Add to `target_calculator.py` as `_distribute_<mode>()`
3. **Test Scenarios**: Create YAML scenarios in `tests/scenarios/`
4. **Documentation**: Update CHARGE_MODES_GUIDE.md, README.md

### Debugging

1. **Enable verbose logging** in HA: `custom_components.dynamic_ocpp_evse: debug`
2. **Run specific test**: `python tests/run_tests.py "test-name"`
3. **Check calculation steps**: Each step logs its output (site_limit, solar_available, target_power, etc.)
4. **Per-phase values**: Log phase_a/b/c_export, consumption, available

### Common Pitfalls

1. **Asymmetric vs Symmetric confusion**: Remember inverter capability affects SUPPLY, not charger DRAW
2. **Per-phase vs total power**: Track carefully whether working with per-phase (A) or total (A×3)
3. **Battery priority**: Battery charges BEFORE EVs when SOC < target
4. **Minimum current**: Chargers need ≥ min_current or get 0 (can't charge below minimum)
5. **Phase assignment defaults**: Don't default to "A" - only set when explicitly specified

## Code Style

- **Type hints**: Use when helpful, but not required (for Python 3.9 compatibility)
- **Comments**: Explain WHY, not WHAT
- **Logging**: Use appropriate levels (debug for details, warning for issues, error for failures)
- **Formatting**: Follow existing style (4-space indent, clear spacing)

## Integration with Home Assistant

The `calculations/` directory is pure Python and can be imported/tested independently. The HA integration:

1. **context.py**: Builds SiteContext from HA entities
2. **dynamic_ocpp_evse.py**: Manages OCPP charger connections, calls calculation engine
3. **Entities** (button.py, number.py, etc.): Expose controls and sensors to HA UI

## Useful Resources

- OCPP 1.6J Specification: https://www.openchargealliance.org/
- Home Assistant Developer Docs: https://developers.home-assistant.io/
- YAML Test Scenarios: `tests/scenarios/*.yaml`
- Charging Modes Guide: `CHARGE_MODES_GUIDE.md`

## Getting Help

When asking for help or reporting issues:
1. Include relevant test scenario (YAML)
2. Share logs with `debug` level enabled
3. Describe expected vs actual behavior
4. Mention if it affects verified scenarios

---

**Last Updated**: 2026-02-11  
**Current Version**: Active Development  
**Test Pass Rate**: 28/29 (97%)