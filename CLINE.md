# CLINE.md - AI Development Guide for Dynamic OCPP EVSE

This document describes the Dynamic OCPP EVSE repository structure, architecture, and development guidelines to help AI assistants (like Cline) effectively develop and maintain this project.

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

1. **Standard**: Maximum charging speed, can import from grid
2. **Eco**: Gentle battery protection, charge at minimum when battery between min-target
3. **Solar**: Only use solar export (+ battery discharge if SOC > target)
4. **Excess**: Only charge when export exceeds threshold

### Distribution Modes

1. **Priority**: Two-pass (min first, then remainder by priority)
2. **Shared**: Two-pass (min first, then split remainder equally)
3. **Strict**: Sequential (priority 1 gets all, then 2, etc.)
4. **Optimized**: Smart reduction to fit more chargers

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

### Known Issues

🔴 **CRITICAL - Dual Constraint Problem**: The current architecture only tracks ONE constraint (either per-phase OR total), but asymmetric inverters have BOTH:
  - **Symmetric Inverter**: Per-phase [10A, 10A, 10A] → Total 30A (sum of phases)
  - **Asymmetric Inverter**: Per-phase [10A, 10A, 10A] + Total 20A (independent limit!)
  
  **Impact**: Asymmetric systems can violate total inverter power limit even if per-phase limits are met.
  
  **Solution Required**:
  - Track both `available_per_phase[]` AND `available_total` throughout calculation pipeline
  - All calculation steps must return tuple: `(per_phase[], total)`
  - Distribution logic must enforce BOTH constraints simultaneously
  - Affects: `_calculate_solar_available()`, `_determine_target_power()`, all distribution modes
  
  **Status**: 🔄 Refactoring in progress

⚠️ **Per-phase distribution with asymmetric inverters**: `_distribute_power_per_phase()` uses raw phase exports, doesn't include battery discharge capability. This affects scenarios where:
  - Single-phase charger has explicit phase assignment
  - Inverter supports asymmetric distribution
  - Battery SOC > target (can discharge)
  - Expected: charger should access total power pool (solar + battery)
  - Actual: charger only sees phase export (solar only)
  
  **Note**: This may be resolved by the dual constraint refactoring above.

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