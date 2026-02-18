# Dynamic OCPP EVSE Tests

This folder contains two categories of tests:

## 1. Calculation Scenario Tests (Pure Python)

YAML-driven tests that validate the calculation engine directly. Run on any platform (no HA dependency).

### Run all scenarios

```bash
python3 dev/tests/run_tests.py dev/tests/scenarios
```

### Run only verified or unverified scenarios

```bash
python3 dev/tests/run_tests.py --verified dev/tests/scenarios
python3 dev/tests/run_tests.py --unverified dev/tests/scenarios
```

### Run a single scenario by name

```bash
python3 dev/tests/run_tests.py "scenario-name"
```

**Test results** are written to `dev/tests/test_results.log`.

### Scenario files

Scenario YAML files live in `dev/tests/scenarios/`:
- `test_scenarios_1ph.yaml` — Single-phase scenarios
- `test_scenarios_1ph_battery.yaml` — Single-phase with battery
- `test_scenarios_3ph.yaml` — Three-phase scenarios
- `test_scenarios_3ph_battery.yaml` — Three-phase with battery

Each YAML file contains a `scenarios:` list with inputs and expected targets for chargers.

## 2. HA Integration Tests (WSL/Linux)

Pytest-based tests using `pytest-homeassistant-custom-component`. These require a Linux environment (HA core depends on `fcntl`), so they run under WSL.

### Run all integration tests

```bash
wsl -- bash -c "source ~/ha-test-venv/bin/activate && cd /mnt/c/Users/anzek/Documents/Dynamic_OCPP_EVSE && python3 -m pytest dev/tests/test_init.py dev/tests/test_config_flow.py dev/tests/test_config_flow_e2e.py dev/tests/test_sensor_update.py -v"
```

### Run a single integration test file

```bash
wsl -- bash -c "source ~/ha-test-venv/bin/activate && cd /mnt/c/Users/anzek/Documents/Dynamic_OCPP_EVSE && python3 -m pytest dev/tests/test_sensor_update.py -v"
```

### Integration test files

| File | What it tests |
|---|---|
| `test_init.py` | Hub/charger setup, teardown, v1→v2 migration |
| `test_config_flow.py` | Config flow step navigation and input validation |
| `test_config_flow_e2e.py` | Full hub/charger creation, discovery, options flow |
| `test_sensor_update.py` | Sensor init, update cycle, OCPP calls, charge pause, profiles |
| `conftest.py` | Shared fixtures (`mock_hub_entry`, `mock_charger_entry`, `mock_setup`) |

## Debug Runner

Use `dev/debug_scenario.py` to debug a **single calculation scenario** interactively with logging:

```bash
python3 dev/debug_scenario.py "scenario-name"
python3 dev/debug_scenario.py "scenario-name" --verbose
```

## Notes

- Calculation scenario tests use **real production code** from `custom_components/dynamic_ocpp_evse/calculations` — no mocks.
- Integration tests mock platform forwarding (`async_forward_entry_setups`) to isolate the component logic under test.
- OCPP service calls are mocked via `patch("homeassistant.core.ServiceRegistry.async_call", ...)` since no real OCPP integration is present in tests.