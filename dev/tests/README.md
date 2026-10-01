# Dynamic OCPP EVSE Tests

Everything in this folder - the YAML calculation scenarios, the unit tests and
the Home Assistant integration tests (`pytest-homeassistant-custom-component`) -
is collected by one plain pytest run. That run is what CI gates a release on,
so it is the one that matters before a push.

## Set it up

Home Assistant needs a recent Python - newer than the system one on a Mac - so
let `uv` fetch a standalone build rather than hunting for an interpreter:

```bash
uv venv .venv --python 3.13 && uv pip install -r requirements_dev.txt
```

They run natively on macOS. The instructions here used to send you through WSL
to a Windows path that no longer exists, on the grounds that HA core needs
`fcntl` - it is in the macOS standard library too, and the whole tier runs in
about 25 seconds.

**Do not skip this run because the venv is awkward.** On 2026-09-18 a one-line
change to `ocpp_discovery.py` shipped unverified for exactly that reason, and
it turned every charge point into a string: six tests red, every Gitea build
and the 2.1.2 release blocked, and nobody noticed for four days because the
only thing that would have caught it was this command.

## Run them

`pytest.ini` puts the project root on the import path, so run from the project
root with no `PYTHONPATH`:

```bash
.venv/bin/python -m pytest dev/tests/ -q
```

```bash
# One file
.venv/bin/python -m pytest dev/tests/test_sensor_update.py -v

# One test or scenario by name
.venv/bin/python -m pytest dev/tests/ -k "scenario-name"

# Only the YAML scenarios; only the human-verified ones; only the unverified ones
.venv/bin/python -m pytest dev/tests/test_scenarios.py
.venv/bin/python -m pytest dev/tests/ -m verified
.venv/bin/python -m pytest dev/tests/test_scenarios.py -m "not verified"

# A scenario's cycle-by-cycle trace live, plus the engine's debug log
.venv/bin/python -m pytest dev/tests/ -k "scenario-name" -s --log-cli-level=DEBUG
```

## Calculation scenarios

`test_scenarios.py` runs every scenario in `scenarios/**/*.yaml` through the
30-cycle simulation in `run_tests.py`, one test per scenario, named after it.
Scenarios with `human_verified: true` carry the `verified` marker. A failing
scenario's assertion message lists its validation lines; its captured output is
the cycle-by-cycle trace.

Scenario YAML files live in `scenarios/`, one folder per site shape
(`1ph/`, `1ph_battery/`, `3ph/`, `3ph_battery/`) plus `features/`. Each file
contains a `scenarios:` list with inputs and expected targets for loads.

## Integration test files

| File | What it tests |
|---|---|
| `test_init.py` | Hub/load setup, teardown, v1→v2 migration |
| `test_config_flow.py` | Config flow step navigation and input validation |
| `test_config_flow_e2e.py` | Full hub/load creation, discovery, options flow, entry migration |
| `test_sensor_update.py` | Sensor init, update cycle, OCPP calls, charge pause, profiles |
| `test_power_station_ha.py` | Power station builder (bounds, managed draw, status) and command module (what is written where) |
| `conftest.py` | Shared fixtures (`mock_hub_entry`, `mock_charger_entry`, `mock_setup`) |
| `closed_loop.py` | The closed-loop rigs the permit tests share: `close_loop` and `evse_entry` (site cycle + permit pipeline against a plant), `clocked` (the whole hub cycle on a fake monotonic clock) |

## Unit test files

These live alongside the integration tests and are collected by the same run,
but use no Home Assistant fixtures:

| File | What it tests |
|---|---|
| `test_hot_water_tank.py` | Tank setpoint resolution and urgency-tier promotion/demotion |
| `test_water_heater_tank.py` | A tank on a `water_heater` entity - heating read off its power sensor, gated by its target temperature alone |
| `test_tank_slider_range.py` | A tank's temperature sliders take the thermostat's own range, and follow it when it changes |
| `test_excess_margin.py` | The Excess trigger - `excess_margin()` across grid/battery/off-grid states |
| `test_power_station.py` | Power station charge-speed quantisation and reserve resolution |
| `test_auto_detect.py` | Grid CT inversion + phase-mapping auto-detection (26 tests) |

## Notes

- Calculation scenario tests use **real production code** from `custom_components/dynamic_ocpp_evse/calculations` - no mocks.
- Integration tests mock platform forwarding (`async_forward_entry_setups`) to isolate the component logic under test.
- OCPP service calls are mocked via `patch("homeassistant.core.ServiceRegistry.async_call", ...)` since no real OCPP integration is present in tests.
