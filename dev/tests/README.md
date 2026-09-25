# Dynamic OCPP EVSE Tests

This folder contains two categories of tests:

## 1. Calculation Scenario Tests (Pure Python)

YAML-driven tests that validate the calculation engine directly. Run on any platform (no HA dependency).

### Run all scenarios

```bash
python3 dev/tests/run_tests.py dev/tests/scenarios
# equivalent - the scenarios directory is the default:
python3 dev/tests/run_tests.py
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
- `test_scenarios_1ph.yaml` - Single-phase scenarios
- `test_scenarios_1ph_battery.yaml` - Single-phase with battery
- `test_scenarios_3ph.yaml` - Three-phase scenarios
- `test_scenarios_3ph_battery.yaml` - Three-phase with battery

Each YAML file contains a `scenarios:` list with inputs and expected targets for loads.

## 2. HA Integration Tests

Pytest-based tests using `pytest-homeassistant-custom-component`. This is the
tier CI runs and gates a release on, so it is the one that matters before a
push.

### Set it up

Home Assistant needs a recent Python - newer than the system one on a Mac - so
let `uv` fetch a standalone build rather than hunting for an interpreter:

```bash
uv venv .venv --python 3.13 && uv pip install -r requirements_dev.txt
```

They run natively on macOS. The instructions here used to send you through WSL
to a Windows path that no longer exists, on the grounds that HA core needs
`fcntl` - it is in the macOS standard library too, and the whole tier runs in
about 25 seconds.

**Do not skip this tier because the venv is awkward.** On 2026-09-18 a one-line
change to `ocpp_discovery.py` shipped unverified for exactly that reason, and
it turned every charge point into a string: six tests red, every Gitea build
and the 2.1.2 release blocked, and nobody noticed for four days because the
only thing that would have caught it was this command.

### Run them

```bash
PYTHONPATH=$PWD .venv/bin/python -m pytest dev/tests/ --ignore=dev/tests/scenarios -q
```

### Run a single integration test file

```bash
PYTHONPATH=$PWD .venv/bin/python -m pytest dev/tests/test_sensor_update.py -v
```

### Integration test files

| File | What it tests |
|---|---|
| `test_init.py` | Hub/load setup, teardown, v1→v2 migration |
| `test_config_flow.py` | Config flow step navigation and input validation |
| `test_config_flow_e2e.py` | Full hub/load creation, discovery, options flow, entry migration |
| `test_sensor_update.py` | Sensor init, update cycle, OCPP calls, charge pause, profiles |
| `test_power_station_ha.py` | Power station builder (bounds, managed draw, status) and command module (what is written where) |
| `conftest.py` | Shared fixtures (`mock_hub_entry`, `mock_charger_entry`, `mock_setup`) |

### Pure-python unit test files

These live alongside the integration tests and are collected by the same pytest
run, but have no Home Assistant dependency of their own:

| File | What it tests |
|---|---|
| `test_hot_water_tank.py` | Tank setpoint resolution and urgency-tier promotion/demotion |
| `test_water_heater_tank.py` | A tank on a `water_heater` entity - heating read off its power sensor, gated by its target temperature alone |
| `test_tank_slider_range.py` | A tank's temperature sliders take the thermostat's own range, and follow it when it changes |
| `test_excess_margin.py` | The Excess trigger - `excess_margin()` across grid/battery/off-grid states |
| `test_power_station.py` | Power station charge-speed quantisation and reserve resolution |
| `test_auto_detect.py` | Grid CT inversion + phase-mapping auto-detection (26 tests) |

These files (and `run_tests.py`) share `dev/tests/standalone_loader.py`, which
stubs out the HA-importing package root and loads the pure modules directly, so
they also run standalone on **Python 3.9+** without Home Assistant:

```bash
python3 dev/tests/test_household_hold.py
python3 dev/tests/test_excess_stayon.py
python3 dev/tests/test_inverter_gate.py
python3 dev/tests/test_inverter_output.py
# needs pytest installed (its __main__ delegates to pytest with --noconftest):
python3 dev/tests/test_auto_detect.py
```

## Debug Runner

Use `dev/debug_scenario.py` to debug a **single calculation scenario** interactively with logging:

```bash
python3 dev/debug_scenario.py "scenario-name"
python3 dev/debug_scenario.py "scenario-name" --verbose
```

## Notes

- Calculation scenario tests use **real production code** from `custom_components/dynamic_ocpp_evse/calculations` - no mocks.
- Integration tests mock platform forwarding (`async_forward_entry_setups`) to isolate the component logic under test.
- OCPP service calls are mocked via `patch("homeassistant.core.ServiceRegistry.async_call", ...)` since no real OCPP integration is present in tests.