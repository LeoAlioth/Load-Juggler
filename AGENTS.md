# AGENTS.md

This file provides guidance to LLM Agents when working with code in this repository.

## Project Overview

Load Juggler is a Home Assistant custom component for intelligent load management. It dynamically distributes available power across managed loads - EV chargers (via OCPP 1.6J), smart plugs, and more - based on solar production, battery state, grid capacity, and per-load operating modes.

**Key Capabilities:**

- Per-load operating modes (Standard, Solar Priority, Solar Only, Excess for EVSE; Continuous, Solar Only, Excess for plugs)
- Multi-load support with priority-based distribution and mode urgency sorting
- Circuit groups - shared breaker limits for co-located loads (post-distribution capping)
- Battery integration with SOC thresholds
- Phase-aware handling (1-phase, 2-phase, 3-phase installations)
- Symmetric and asymmetric inverter support
- Off-grid support (no grid CTs required - infers phases from inverter output)

**Versioning** - `manifest.json` carries the version being worked on, not the last one released: the first change after a release opens its `RELEASE_NOTES.md` section and bumps the manifest to match in the same commit, so every pre-release built from `dev` is named after the release it leads to (`2.1.4-dev.<timestamp>`, never `2.1.3-dev` after 2.1.3 shipped). A merge to `main` then releases that version (Anze, 2026-09-25).

**Backwards compatibility** - stored config entries migrate via the step chain in `async_migrate_entry` (`__init__.py`, currently minor version 5); any change to stored keys/values needs a new idempotent step there plus a bump of `MINOR_VERSION` in `config_flow/flow.py`, covered by tests in `test_config_flow_e2e.py`. Published entity ids, unique_ids, attribute names and service fields are user-facing API - keep them stable unless a break is deliberate and called out in `RELEASE_NOTES.md`.

**Improvement Ideas** `dev/IMPROVEMENTS.md` List of ideas for future imporovements and changes. Developer will prompt Claude to discuss and refine them.

**TODOs** Keep track of TODOs as a checkbox list in `dev/TODO.md`. Before and after making code changes, make sure that the TODO is up to date. Completed items are removed - history lives in `git log` and `RELEASE_NOTES.md`. Two parts:

- **Backlog**: Upcoming work.
- **Other**: Non-code tasks (e.g., icon submissions, external PRs).

Each Backlog TODO must be tagged **[BUG]** or **[FEATURE]**. Bugs are prioritized over features.

## Architecture

### Code Structure

```text
custom_components/dynamic_ocpp_evse/
├── __init__.py                    # HA setup/unload, services (re-exports the registry helpers)
├── registry.py                    # Entry-relationship lookups (get_hub_for_load, get_*_for_hub) -
│                                  #   HA-import-free, so it sits outside the package root without an import cycle
├── manifest.json                  # Component metadata
├── config_flow/                   # HA configuration flow (initial setup + options "Configure" flow - the single
│   │                              #   edit path; no reconfigure flow. Options menu: settings / overview / summary)
│   ├── flow.py                    # LoadJugglerConfigFlow - the initial-setup step methods
│   ├── options.py                 # LoadJugglerOptionsFlow - the "Configure" edit steps
│   ├── schemas.py                 # Every voluptuous schema builder, as module functions - no handler
│   │                              #   state; `hass` is a parameter only where a form offers entity selectors
│   ├── pages.py                   # Overview / "How it decides" read-only page text builders
│   └── helpers.py                 # Everything both handlers share: unit validation, the optional-entity
│                                  #   key groups and their normalizers, entity auto-detection, priority
│                                  #   ordering, the OCPP capability probes, phase count
├── const/                         # Constants per area: common, hub, evse, plug, hot_water_tank, group,
│                                  #   inverter, modes, power_station
├── engine/                        # HA → SiteContext bridge (reads HA states, drives the calculation)
│   ├── hub_calculation.py         # Main entry point - run_hub_calculation() builds SiteContext, calls engine
│   │                              #   Keeps the core cycle: _apply_feedback_loop(), the SOC/Excess latches,
│   │                              #   household figures; everything else lives in the siblings below
│   ├── readers.py                 # HA-state edge: _read_entity() (returns _UNAVAILABLE sentinel),
│   │                              #   _smooth() (EMA) + _stale_guard() (holdover), _coerce(),
│   │                              #   grid/inverter/fleet-member reading
│   ├── load_builders.py           # _build_[evse|plug|power_station|hot_water_tank]_load(),
│   │                              #   _add_loads_to_site(), _build_circuit_groups()
│   ├── readout_watch.py           # Pure: judges an EVSE's reported draw STUCK - it claims more than
│   │                              #   the limit in force for 2x its learned reporting gap, OR the
│   │                              #   reconstructed household steps with our commands to it (both
│   │                              #   ways, repeatedly) while it reads nothing; load_builders then
│   │                              #   controls it blind (assumed draw = the accepted command).
│   │                              #   The same assumption, with no verdict, while the reading was
│   │                              #   last reported before the limit in force last changed (the
│   │                              #   connector entering Charging, or a command that moved it)
│   ├── hub_result.py              # _compute_forecast_advice(), _build_hub_result() (the published dict)
│   ├── fleet.py                   # Multi-inverter fleet aggregation (solar_total, weighted_soc, inverter_limits)
│   ├── auto_detect.py             # Grid CT inversion + phase mapping auto-detection
│   └── forecast_reader.py         # Solar forecast sensor reading
├── calculations/                  # Core calculation logic (PURE PYTHON - no HA dependencies)
│   ├── models.py                  # Data models (SiteContext, LoadContext, CircuitGroup, PhaseConstraints, PhaseValues)
│   ├── target_calculator.py       # Main calculation engine
│   ├── forecast.py                # Forecast-based charging advice
│   ├── calibration.py             # Forecast calibration (level-bias gain, peakiness)
│   └── utils.py                   # Utility functions (is_number, compute_household_per_phase)
├── control/                       # Actuation layer (imports only const/helpers/units - never entities or engine)
│   ├── ocpp.py                    # OCPP charging-profile service calls
│   ├── compliance.py              # Charger compliance checks, escalating resets
│   ├── inverter.py                # Inverter register writes (deadband-guarded)
│   ├── plug.py                    # Smart plug switching
│   ├── power_station.py           # Power station control
│   ├── hot_water_tank.py          # Tank climate control
│   ├── smoothing.py               # Output smoothing (EMA / Schmitt trigger / ramp limits)
│   └── status.py                  # Charging status determination
├── entities/                      # Entity classes shared by the platform files (ALL push-driven - nothing polls)
│   ├── load.py                    # LoadJugglerDeviceSensor - per-load processing + dispatch, driven by the hub coordinator
│   ├── load_sensors.py            # Per-load diagnostic sensors
│   ├── hub.py / inverter.py / circuit_group.py  # Hub, inverter, and group sensors
│   ├── freshness.py               # Pure producer-freshness predicate behind every sensor's `available`
│   └── mixins.py                  # LoadJugglerEntity base + device mixins + SiteFreshnessMixin /
│                                  #   SiteCycleConsumerMixin (push readers) / SiteCycleWorkerMixin (async per-cycle actuators)
├── detection_patterns.py          # Per-brand entity-naming patterns for grid CT auto-detection (GRID_CT, brand
│                                  #   priority order, watts first) and plug power monitors
├── [button|number|select|sensor|switch].py  # HA platform files (thin wiring around entities/)
├── units.py                       # Unit conversion helpers
├── phases.py                      # Pure, stdlib only: which of a device's sensors are its per-phase
│                                  #   readings (match_meter_entities, beside) and which phase each line
│                                  #   carries (phase_mapping, support - engine/auto_detect.py). A VERBATIM
│                                  #   copy: canonical in Load Insights (insights/phases.py), synced by hand -
│                                  #   never edit it here; LI's tests/test_phases.py checks this copy
│                                  #   byte-for-byte when both repos sit side by side
├── helpers.py                     # get_entry_value() and misc helpers
├── ocpp_discovery.py              # The ONE OCPP registry derivation, at the package root so BOTH the
│                                  #   flows and engine/ can reach it (engine must not import config_flow).
│                                  #   Sensors are grouped by charge point and classified by the ocpp
│                                  #   integration's own metric keys (unique_id → original_name →
│                                  #   entity_id suffix), never by guessing entity_id prefixes:
│                                  #   scan_ocpp_chargers() (discovery + the manual wizard),
│                                  #   ocpp_charger_for_device() / ocpp_device_for_charge_point() (the two
│                                  #   directions the device pickers need), ocpp_entry_fields() (the stored
│                                  #   OCPP field set both edit paths write), ocpp_connector_status_entity()
│                                  #   (the runtime's status sensor, resolved once per load setup and cached
│                                  #   in that load's hass.data bucket, legacy composed name as fallback)
└── translations/                  # Localization files (en, sl)
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
- `'AB'`, `'AC'`, `'BC'` - Two-phase limits (for 2-phase loads)
- `'ABC'` - Three-phase limit (total)

This properly enforces constraints for every load configuration:

- 1-phase load on phase A: Uses `constraints['A']`
- 2-phase load on AB: Uses `min(constraints['A'], constraints['B'], constraints['AB'])`
- 3-phase load: Uses `min(constraints['A'], constraints['B'], constraints['C'], constraints['ABC'])`

**Why**: Physical reality - inverters and breakers have limits for EACH phase combination, not just individual phases.

### Calculation Flow

The calculation engine follows a 5-step process (see `target_calculator.py`):

```text
0. Refresh SiteContext (done externally in HA integration)
   → Subtract load draws from consumption (feedback loop correction)
   ↓
1. Calculate absolute site limits (per-phase physical constraints)
   → _calculate_site_limit()
     ├─ _calculate_grid_limit()      (grid capacity based on breaker rating)
     └─ _calculate_inverter_limit()  (export with our loads off + battery headroom)
   ↓
2. Calculate solar surplus power (includes battery charge/discharge)
   → _calculate_solar_surplus()
   ↓
3. Calculate excess available power
   → _calculate_excess_available()
   ↓
4. Compute per-load ceilings based on each load's operating mode
   → _source_limit() per load (mode-aware, uses solar/excess pools)
   ↓
5. Distribute power among loads (sorted by mode urgency + priority)
   → _distribute_power()
   ↓
6. Enforce circuit group limits (post-distribution capping)
   → _enforce_circuit_groups()
```

### Data Models

**PhaseValues** (`calculations/models.py`) - Per-phase values (a, b, c) with `.total` property.

**PhaseConstraints** (`calculations/models.py`) - Per-phase + combination power constraints (A, B, C, AB, AC, BC, ABC). Methods: `from_per_phase()`, `from_pool()`, `get_available(mask)`, `deduct()`, `normalize()`, arithmetic operators.

**SiteContext** (`calculations/models.py`) - Represents the entire electrical site:

- Electrical: voltage, num_phases, main_breaker_rating
- Per-phase: consumption (PhaseValues), export_current (PhaseValues), grid_current (PhaseValues)
- Solar: solar_production_total (derived via `engine/fleet.py` `solar_total()`, or from dedicated entity), solar_is_derived, household_consumption_total
- Derived: total_export_current, total_export_power (computed properties)
- Battery: battery_soc, battery_soc_min, battery_soc_target, battery_max_charge/discharge_power
- Inverter: inverter_max_power, inverter_max_power_per_phase, inverter_supports_asymmetric, wiring_topology, inverter_output_per_phase
- Charging: distribution_mode, loads[], circuit_groups[]

**LoadContext** (`calculations/models.py`) - Represents a single managed load (EVSE or smart plug):

- Config: load_id, min_current, max_current, phases, priority, device_type, operating_mode
- Status: connector_status (Available, Charging, etc.)
- Phase tracking: active_phases_mask ("A", "B", "C", "AB", "BC", "AC", "ABC")
- Current: l1_current, l2_current, l3_current (actual OCPP draw)
- Calculated: target_current (output of calculation)

**CircuitGroup** (`calculations/models.py`) - Shared breaker limit for co-located loads:

- Config: group_id, name, current_limit (per-phase A), member_ids[]
- Enforced post-distribution: member allocations per phase capped to current_limit

### HA Integration Layer

The `calculations/` directory is pure Python and can be imported/tested independently. The HA integration layer:

1. **engine/hub_calculation.py** (with `readers.py`, `load_builders.py`, `hub_result.py`): Reads HA entity states, builds SiteContext/LoadContext, calls calculation engine. Key patterns:
   - `_UNAVAILABLE` sentinel: returned by `readers.py:_read_entity()` when a configured sensor is unavailable/unknown
   - `_smooth()` + `_stale_guard()` (`readers.py`): EMA smoothing with `_UNAVAILABLE` holdover (holds last value instead of decaying to 0), NaN/Inf rejection
   - `_coerce()` (`readers.py`): converts `_UNAVAILABLE` back to safe defaults for non-smoothed values
   - Solar derivation: `engine/fleet.py` (`solar_total()` / `member_solar()`) - uses inverter output when available, falls back to grid export + battery
   - Off-grid: when no grid CTs are configured, phases with inverter output entities are zeroed (not None), making the site behave like a grid site with 0A grid current
2. **Hub coordinator (sensor.py) + entities/load.py + control/**: ONE `DataUpdateCoordinator` per hub entry (`hass.data[DOMAIN]["hub_coordinators"]`) runs the engine once per `site_update_frequency`, publishes the trimmed result via `entities/hub.py:publish_hub_data`, then awaits each registered load processor sequentially (`hass.data[DOMAIN]["load_processors"]`, entry_id order - strict serialization for OCPP), then each async cycle worker (`SITE_CYCLE_WORKERS`, e.g. the inverter charge-limit write - after publish, order-insignificant), and finally notifies the push-reader sensors (`site_cycle_listeners`, rebound every tick so hub-only reloads can't strand them). `LoadJugglerDeviceSensor.async_process(hub_data)` does the per-load work - smoothing (`control/smoothing.py`), grace/pause state machines, then dispatch: OCPP charging profiles via `control/ocpp.py`, plug/tank/station actuation via their `control/` modules. Per-load `update_frequency` gates command sends inside the processor. Hub sensors (`entities/hub.py`) are pure readers of hub_data: Site Available Power, Hub Status, per-metric data sensors. Load sensors: allocated current, available current, charging status.
3. **Platform files** (button.py, number.py, select.py, etc.): Thin wiring that exposes the `entities/` classes and controls to the HA UI

### Asymmetric vs Symmetric Inverters

**Symmetric Inverter** (`inverter_supports_asymmetric=False`):

- Solar/battery power is fixed per-phase
- Each phase operates independently
- 3-phase loads limited by minimum available phase

**Asymmetric Inverter** (`inverter_supports_asymmetric=True`):

- Solar/battery power can be distributed across any phase
- Inverter can balance load dynamically
- Total power pool available (not per-phase limited, respecting inverter limits)

**Important**: Regardless of inverter type, loads are physically connected to specific phases and can only draw from those phases. The inverter asymmetric capability affects power SUPPLY flexibility, not load DRAW flexibility.

### Phase-Specific Allocation

When loads have explicit phase assignments (e.g., `l1_phase: "B"`):

- All distribution uses PhaseConstraints - per-phase limits are enforced automatically
- Each phase is allocated independently via `_distribute_power()`
- 3-phase loads limited by minimum available phase

## Operating & Distribution Modes

Per-load operating modes (set independently per load): **Standard** (EVSE: max speed from all sources), **Continuous** (Plug: always on), **Solar Priority** (solar-first with min rate fallback), **Solar Only** (pure solar only), **Excess** (threshold-based export charging). Mode urgency: Standard/Continuous > Solar Priority > Solar Only > Excess. See [CHARGE_MODES_GUIDE.md](CHARGE_MODES_GUIDE.md) for full details.

Four distribution modes for multi-load setups: **Shared** (equal split), **Priority** (higher priority first), **Optimized** (sequential with leftover sharing), **Strict** (sequential, no sharing). See [DISTRIBUTION_MODES_GUIDE.md](DISTRIBUTION_MODES_GUIDE.md) for full details.

## Development

### Guidelines

1. **Understand the Flow**: Always trace through the 5-step calculation process
2. **Pure Python**: `calculations/` directory has no HA dependencies for testability
3. **Data Models**: Use SiteContext and LoadContext - don't pass raw values
4. **Logging**: Use `_LOGGER.debug()` extensively for troubleshooting
5. **Test First**: Run relevant tests before and after changes
6. **Helper Functions**: Prefer helper functions over inline logic for maintainability
7. **General naming**: When something concerns more than one device type, name it by the general concept - `load`, never `charger`, for the generic managed-device idea. The generic rename is done: `charger` now survives ONLY where the thing can only ever be an EVSE (OCPP discovery and profiles, `CONF_CHARGER_ID`/`CONF_CHARGER_L1..L3_PHASE`, the `charger_*` config-flow steps, `validate_charger_settings`, compliance, the `*_ocpp_evse` service). If a plug, tank or power station can flow through it, it is a `load`. Two deliberate exceptions, both documented in place: the published `charger_priority` entity attribute (public API since 2.0.5) and the EVSE phase-mapping notification text.

### Adding New Features

1. **Operating Mode**: Add ceiling logic in `_source_limit()` in `target_calculator.py`
2. **Distribution Mode**: Add to `target_calculator.py` as `_distribute_<mode>()`
3. **Test Scenarios**: Create YAML scenarios in `dev/tests/scenarios/`
4. **Documentation**: Update CHARGE_MODES_GUIDE.md, README.md

### Common Pitfalls

1. **Asymmetric vs Symmetric confusion**: Remember inverter capability affects SUPPLY, not load DRAW
2. **Per-phase vs total power**: Track carefully whether working with per-phase (A) or total (A*3)
3. **Battery priority**: Battery charges BEFORE EVs when SOC < target (Standard mode being the exception)
4. **Minimum current**: Loads need >= min_current or get 0 (can't run below minimum)
5. **Phase assignment defaults**: Don't default to "A" - only set when explicitly specified
6. **Legacy code**: legacy compatibility should be removed as users are expected to reconfigure the integration
7. **Grid CT consumption includes load draws**: Grid current sensors measure TOTAL site import, which includes managed-load power. `engine/hub_calculation.py` (`_apply_feedback_loop()`) subtracts each load's l1/l2/l3_current from `site.consumption` before calling the engine (step 0). Without this, the engine double-counts load power as both "consumption" and "load demand", leading to under-allocation or false pauses. Hub sensor display values intentionally show the raw (unadjusted) grid readings.

## Testing and Debugging

**Test procedure**: Do not combine multiple shell commands to one line. Always run one test at a time.

Every test - unit, HA integration and each YAML scenario - runs under plain pytest from the
project root (`pip install -r requirements_dev.txt`; `pytest.ini` puts the root on the path):

```bash
# Everything (what CI runs)
pytest dev/tests/ -v

# Only the YAML scenarios; only the human-verified ones; only the unverified ones
pytest dev/tests/test_scenarios.py
pytest dev/tests/ -m verified
pytest dev/tests/test_scenarios.py -m "not verified"

# One scenario (or any test) by name
pytest dev/tests/ -k "scenario-name"

# ... with its cycle-by-cycle trace printed live, plus the engine's debug log
pytest dev/tests/ -k "scenario-name" -s --log-cli-level=DEBUG
```

### Calculation Scenario Tests

YAML-driven tests that validate the calculation engine directly: `dev/tests/test_scenarios.py` runs
every scenario in `dev/tests/scenarios/` through the 30-cycle simulation in `dev/tests/run_tests.py`,
one test per scenario, named after it. A failing scenario's assertion message lists its validation
lines, and its captured output is the cycle-by-cycle trace.

**IMPORTANT**: When creating new or modifying existing test scenarios, always set `human_verified: false`. Only the developer marks scenarios as verified after manual review.

Scenario YAML format:

```yaml
scenarios:
  - name: "test-name"
    description: "What this tests"
    human_verified: false
    site:
      voltage: 230
    loads:
      - entity_id: "load_1"
        min_current: 6
        max_current: 16
        phases: 3
        priority: 1
        l1_phase: "A"
        operating_mode: "Solar Only"
    expected:
      load_1:
        allocated: 10.0
```

Scenario files in `dev/tests/scenarios/` (organized by site type × charging mode):

```text
1ph/            - Single-phase, no battery (test_solar, test_eco, test_standard, test_excess)
1ph_battery/    - Single-phase with battery (test_solar, test_eco, test_standard, test_excess)
3ph/            - Three-phase, no battery (test_solar, test_eco, test_standard, test_excess)
3ph_battery/    - Three-phase with battery (test_solar, test_eco, test_standard, test_excess)
features/       - Cross-cutting tests (test_available, test_plugs, test_phase_mapping, test_circuit_groups)
```

### HA Integration Tests

Integration tests use `pytest-homeassistant-custom-component` and are collected by the same plain
pytest run as everything else. The one local way to run it is the uv venv in `dev/tests/README.md`
(Python 3.14, what CI runs); there is no Docker test image.

**Integration test files:**

- `test_init.py` - Setup, teardown, migration (v1->v2, v2.0->v2.1)
- `test_config_flow.py` - Config flow step navigation and validation
- `test_config_flow_e2e.py` - Full hub/load creation flows, options flow, discovery, entry migration
- `test_ocpp_discovery.py` - The OCPP charger scan against a mocked device+entity registry, the OCPP
  device pickers on both edit paths (create wizard and options charger page), and the runtime
  connector-status resolution
- `test_sensor_update.py` - Sensor initialization, update cycle, OCPP calls, charge pause, profile formats

### Debugging

1. **Enable verbose logging** in HA: `custom_components.dynamic_ocpp_evse: debug`
2. **Run specific test**: `pytest dev/tests/ -k "test-name"`
3. **Debug a single scenario**: `pytest dev/tests/ -k "scenario-name" -s --log-cli-level=DEBUG` (cycle trace + engine debug log)
4. **Check calculation steps**: Each step logs its output (site_limit, solar_available, target_power, etc.)
5. **Per-phase values**: Log phase_a/b/c_export, consumption, available

## Useful Resources

- Charging Modes Guide: `CHARGE_MODES_GUIDE.md`
- Distribution Modes Guide: `DISTRIBUTION_MODES_GUIDE.md`
- Release notes: `RELEASE_NOTES.md`
- YAML Test Scenarios: `dev/tests/scenarios/*.yaml`
- OCPP 1.6J Specification: <https://www.openchargealliance.org/>
- Home Assistant Developer Docs: <https://developers.home-assistant.io/>
