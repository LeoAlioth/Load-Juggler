# AGENTS.md

This file provides guidance to LLM Agents when working with code in this repository.

## Project Overview

Load Juggler is a Home Assistant custom component for intelligent load management. It dynamically distributes available power across managed loads — EV chargers (via OCPP 1.6J), smart plugs, and more — based on solar production, battery state, grid capacity, and per-load operating modes.

**Key Capabilities:**

- Per-load operating modes (Standard, Solar Priority, Solar Only, Excess for EVSE; Continuous, Solar Only, Excess for plugs)
- Multi-load support with priority-based distribution and mode urgency sorting
- Battery integration with SOC thresholds
- Phase-aware handling (1-phase, 2-phase, 3-phase installations)
- Symmetric and asymmetric inverter support

**Version 2.0** — disregard backwards compatibility. No migration processes needed.

**Bug tracking**: Open issues live in `dev/ISSUES.md`. Claude picks them up automatically at the start of each session.

**Improvement Ideas** `dev/IMPROVEMENTS.md` List of ideas for future imporovements and changes. Developer will prompt Claude to discuss and refine them.

**TODOs** Keep track of TODOs as an ordered numbered list with checkmarks in `dev/TODO.md`. Before and after making code changes, make sure that the TODO is up to date. Mark steps completed as soon as they are done. Split TODOs into 4 parts:

- **Completed**: Short one-liners (title only, no implementation details). Periodically consolidate related items and remove entries that are no longer useful context.
- **In Progress**: Clearly defined tasks to finish before reaching out to the developer. Include enough detail to implement without ambiguity.
- **Backlog**: Upcoming work. More general — make more detailed when transitioning to In Progress.
- **Other**: Non-code tasks (e.g., icon submissions, external PRs).

Each In Progress and Backlog TODO must be tagged **[BUG]** or **[FEATURE]**. Bugs are prioritized over features.

## Architecture

### Code Structure

```text
custom_components/dynamic_ocpp_evse/
├── __init__.py                    # HA component initialization
├── manifest.json                  # Component metadata
├── const.py                       # Constants and defaults
├── config_flow.py                 # HA configuration flow
├── dynamic_ocpp_evse.py          # Main entry point — reads HA states, builds SiteContext, calls engine
├── [button|number|select|sensor|switch].py  # HA entities
├── calculations/                  # Core calculation logic (PURE PYTHON - no HA dependencies)
│   ├── models.py                  # Data models (SiteContext, LoadContext)
│   ├── context.py                 # Context builder (HA → models)
│   ├── target_calculator.py       # Main calculation engine
│   └── utils.py                   # Utility functions (is_number)
└── translations/                  # Localization files
```

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

**Why**: Physical reality — inverters and breakers have limits for EACH phase combination, not just individual phases.

### Calculation Flow

The calculation engine follows a 5-step process (see `target_calculator.py`):

```text
0. Refresh SiteContext (done externally in HA integration)
   → Subtract charger draws from consumption (feedback loop correction)
   ↓
1. Calculate absolute site limits (per-phase physical constraints)
   → _calculate_site_limit()
     ├─ _calculate_grid_limit()      (grid capacity based on breaker rating)
     └─ _calculate_inverter_limit()  (solar + battery)
   ↓
2. Calculate solar surplus power (includes battery charge/discharge)
   → _calculate_solar_surplus()
   ↓
3. Calculate excess available power
   → _calculate_excess_available()
   ↓
4. Compute per-load ceilings based on each load's operating mode
   → _compute_charger_ceiling() per load (mode-aware, uses solar/excess pools)
   ↓
5. Distribute power among loads (sorted by mode urgency + priority)
   → _distribute_power()
```

### Data Models

**PhaseValues** (`calculations/models.py`) — Per-phase values (a, b, c) with `.total` property.

**PhaseConstraints** (`calculations/models.py`) — Per-phase + combination power constraints (A, B, C, AB, AC, BC, ABC). Methods: `from_per_phase()`, `from_pool()`, `get_available(mask)`, `deduct()`, `normalize()`, arithmetic operators.

**SiteContext** (`calculations/models.py`) — Represents the entire electrical site:

- Electrical: voltage, num_phases, main_breaker_rating
- Per-phase: consumption (PhaseValues), export_current (PhaseValues), grid_current (PhaseValues)
- Solar: solar_production_total (derived from grid CT export, or from dedicated entity), solar_is_derived, household_consumption_total
- Derived: total_export_current, total_export_power (computed properties)
- Battery: battery_soc, battery_soc_min, battery_soc_target, battery_max_charge/discharge_power
- Inverter: inverter_max_power, inverter_max_power_per_phase, inverter_supports_asymmetric
- Charging: distribution_mode, chargers[]

**LoadContext** (`calculations/models.py`) — Represents a single managed load (EVSE or smart plug):

- Config: charger_id, min_current, max_current, phases, car_phases, priority, device_type, operating_mode
- Status: connector_status (Available, Charging, etc.)
- Phase tracking: active_phases_mask ("A", "B", "C", "AB", "BC", "AC", "ABC")
- Current: l1_current, l2_current, l3_current (actual OCPP draw)
- Calculated: target_current (output of calculation)

### HA Integration Layer

The `calculations/` directory is pure Python and can be imported/tested independently. The HA integration layer:

1. **dynamic_ocpp_evse.py**: Reads HA entity states, builds SiteContext/LoadContext, calls calculation engine
2. **sensor.py**: Uses engine output (charger_targets) to set OCPP charging profiles via service calls
3. **Entities** (button.py, number.py, select.py, etc.): Expose controls and sensors to HA UI

### Asymmetric vs Symmetric Inverters

**Symmetric Inverter** (`inverter_supports_asymmetric=False`):

- Solar/battery power is fixed per-phase
- Each phase operates independently
- 3-phase chargers limited by minimum available phase

**Asymmetric Inverter** (`inverter_supports_asymmetric=True`):

- Solar/battery power can be distributed across any phase
- Inverter can balance load dynamically
- Total power pool available (not per-phase limited, respecting inverter limits)

**Important**: Regardless of inverter type, chargers are physically connected to specific phases and can only draw from those phases. The inverter asymmetric capability affects power SUPPLY flexibility, not charger DRAW flexibility.

### Phase-Specific Allocation

When chargers have explicit phase assignments (e.g., `l1_phase: "B"`):

- All distribution uses PhaseConstraints — per-phase limits are enforced automatically
- Each phase is allocated independently via `_distribute_power()`
- 3-phase chargers limited by minimum available phase

## Operating & Distribution Modes

Per-load operating modes (set independently per load): **Standard** (EVSE: max speed from all sources), **Continuous** (Plug: always on), **Solar Priority** (solar-first with min rate fallback), **Solar Only** (pure solar only), **Excess** (threshold-based export charging). Mode urgency: Standard/Continuous > Solar Priority > Solar Only > Excess. See [CHARGE_MODES_GUIDE.md](CHARGE_MODES_GUIDE.md) for full details.

Four distribution modes for multi-load setups: **Shared** (equal split), **Priority** (higher priority first), **Optimized** (sequential with leftover sharing), **Strict** (sequential, no sharing). See [DISTRIBUTION_MODES_GUIDE.md](DISTRIBUTION_MODES_GUIDE.md) for full details.

## Development

### Guidelines

1. **Understand the Flow**: Always trace through the 5-step calculation process
2. **Pure Python**: `calculations/` directory has no HA dependencies for testability
3. **Data Models**: Use SiteContext and LoadContext — don't pass raw values
4. **Logging**: Use `_LOGGER.debug()` extensively for troubleshooting
5. **Test First**: Run relevant tests before and after changes
6. **Helper Functions**: Prefer helper functions over inline logic for maintainability

### Adding New Features

1. **Operating Mode**: Add ceiling logic in `_compute_charger_ceiling()` in `target_calculator.py`
2. **Distribution Mode**: Add to `target_calculator.py` as `_distribute_<mode>()`
3. **Test Scenarios**: Create YAML scenarios in `dev/tests/scenarios/`
4. **Documentation**: Update CHARGE_MODES_GUIDE.md, README.md

### Common Pitfalls

1. **Asymmetric vs Symmetric confusion**: Remember inverter capability affects SUPPLY, not charger DRAW
2. **Per-phase vs total power**: Track carefully whether working with per-phase (A) or total (A*3)
3. **Battery priority**: Battery charges BEFORE EVs when SOC < target (Standard mode being the exception)
4. **Minimum current**: Chargers need >= min_current or get 0 (can't charge below minimum)
5. **Phase assignment defaults**: Don't default to "A" — only set when explicitly specified
6. **Legacy code**: This is version 2.0.0 — legacy compatibility should be removed as users are expected to reconfigure the integration
7. **Grid CT consumption includes charger draws**: Grid current sensors measure TOTAL site import, which includes charger power. `dynamic_ocpp_evse.py` subtracts each charger's l1/l2/l3_current from `site.consumption` before calling the engine (step 0). Without this, the engine double-counts charger power as both "consumption" and "charger demand", leading to under-allocation or false pauses. Hub sensor display values intentionally show the raw (unadjusted) grid readings.

## Testing and Debugging

**Test procedure**: Do not combine multiple shell commands to one line. Always run one test at a time.

### Calculation Scenario Tests (Pure Python)

YAML-driven tests that validate the calculation engine directly. Run on any platform.

```bash
# Run all scenarios
python3 dev/tests/run_tests.py dev/tests/scenarios

# Run only verified or unverified
python3 dev/tests/run_tests.py --verified dev/tests/scenarios
python3 dev/tests/run_tests.py --unverified dev/tests/scenarios

# Run a single scenario by name
python3 dev/tests/run_tests.py "scenario-name"

# Run a single test with a detailed output
python3 dev/tests/run_tests.py "scenario-name" --trace

```

Test results are written to `dev/tests/test_results.log`.

**IMPORTANT**: When creating new or modifying existing test scenarios, always set `human_verified: false`. Only the developer marks scenarios as verified after manual review.

Scenario YAML format:

```yaml
scenarios:
  - name: "test-name"
    description: "What this tests"
    human_verified: false
    site:
      voltage: 230
    chargers:
      - entity_id: "charger_1"
        min_current: 6
        max_current: 16
        phases: 3
        priority: 1
        l1_phase: "A"
        operating_mode: "Solar Only"
    expected:
      charger_1:
        allocated: 10.0
```

Scenario files in `dev/tests/scenarios/` (organized by site type × charging mode):

```
1ph/            — Single-phase, no battery (test_solar, test_eco, test_standard, test_excess)
1ph_battery/    — Single-phase with battery (test_solar, test_eco, test_standard, test_excess)
3ph/            — Three-phase, no battery (test_solar, test_eco, test_standard, test_excess)
3ph_battery/    — Three-phase with battery (test_solar, test_eco, test_standard, test_excess)
features/       — Cross-cutting tests (test_available, test_plugs, test_phase_mapping)
```

### HA Integration Tests (WSL/Linux)

Integration tests use `pytest-homeassistant-custom-component` and run under WSL (HA core requires `fcntl`, Unix-only):

```bash
# All integration tests
wsl -- bash -c "source ~/ha-test-venv/bin/activate && cd /mnt/c/Users/anzek/Documents/Dynamic_OCPP_EVSE && python3 -m pytest dev/tests/test_init.py dev/tests/test_config_flow.py dev/tests/test_config_flow_e2e.py dev/tests/test_sensor_update.py -v"

# Individual test file
wsl -- bash -c "source ~/ha-test-venv/bin/activate && cd /mnt/c/Users/anzek/Documents/Dynamic_OCPP_EVSE && python3 -m pytest dev/tests/test_sensor_update.py -v"
```

**Integration test files:**

- `test_init.py` — Setup, teardown, migration (v1->v2, v2.0->v2.1)
- `test_config_flow.py` — Config flow step navigation and validation
- `test_config_flow_e2e.py` — Full hub/charger creation flows, options flow, discovery
- `test_sensor_update.py` — Sensor initialization, update cycle, OCPP calls, charge pause, profile formats

### Linting and Type Checking

```bash
pip install -r requirements_dev.txt
black custom_components/dynamic_ocpp_evse
flake8 custom_components/dynamic_ocpp_evse
pylint custom_components/dynamic_ocpp_evse
mypy custom_components/dynamic_ocpp_evse
```

### Debugging

1. **Enable verbose logging** in HA: `custom_components.dynamic_ocpp_evse: debug`
2. **Run specific test**: `python3 dev/tests/run_tests.py "test-name"`
3. **Debug a single scenario**: `python3 dev/debug_scenario.py "scenario-name" --verbose`
4. **Check calculation steps**: Each step logs its output (site_limit, solar_available, target_power, etc.)
5. **Per-phase values**: Log phase_a/b/c_export, consumption, available

## Useful Resources

- Charging Modes Guide: `CHARGE_MODES_GUIDE.md`
- Distribution Modes Guide: `DISTRIBUTION_MODES_GUIDE.md`
- Release notes: `RELEASE_NOTES.md`
- YAML Test Scenarios: `dev/tests/scenarios/*.yaml`
- OCPP 1.6J Specification: <https://www.openchargealliance.org/>
- Home Assistant Developer Docs: <https://developers.home-assistant.io/>
