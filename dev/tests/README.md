# Dynamic OCPP EVSE Scenario Tests

This folder contains the scenario-based test runner and YAML scenarios used to validate the Dynamic OCPP EVSE calculation engine.

## Test Runner

The main runner is `dev/tests/run_tests.py`, which executes scenarios defined in `dev/tests/scenarios/`.

### Run all scenarios

```bash
python dev/tests/run_tests.py dev/tests/scenarios
```

### Run only verified or unverified scenarios

```bash
python dev/tests/run_tests.py --verified dev/tests/scenarios
python dev/tests/run_tests.py --unverified dev/tests/scenarios
```

### Run a single scenario by name

```bash
python dev/tests/run_tests.py "scenario-name"
```

**Test results** are written to `dev/tests/test_results.log`.

## Debug Runner

Use `dev/debug_scenario.py` when you want to debug a **single scenario** interactively with logging enabled.

```bash
python dev/debug_scenario.py "scenario-name"
```

### Verbose logging

```bash
python dev/debug_scenario.py "scenario-name" --verbose
```

By default, the debug runner searches `dev/tests/scenarios/` for scenario YAML files. You can override this with `--scenarios-dir`.

## Scenarios

Scenario files live in:

```
dev/tests/scenarios/
```

Each YAML file contains a `scenarios:` list with inputs and expected targets for chargers.

## Notes

- The scenario runner uses **real production code** from `custom_components/dynamic_ocpp_evse/calculations`.
- Use scenario tests for calculation logic changes, and update expected values when behavior changes intentionally.