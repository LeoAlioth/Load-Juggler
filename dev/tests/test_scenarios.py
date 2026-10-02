"""Every YAML scenario under scenarios/ through the 30-cycle simulation.

One test per scenario, named after it. The cycle-by-cycle trace is captured
and printed on failure; ``-s`` shows it live.
"""

from pathlib import Path

import pytest

from .run_tests import load_scenarios, run_scenario_simulation

SCENARIOS = Path(__file__).resolve().parent / "scenarios"


@pytest.mark.parametrize(
    "scenario",
    [
        pytest.param(
            scenario,
            id=scenario["name"],
            marks=[pytest.mark.verified] if scenario.get("human_verified") else [],
        )
        for path in sorted(SCENARIOS.rglob("*.yaml"))
        for scenario in load_scenarios(path)
    ],
)
def test_scenario(scenario):
    passed, errors, _ = run_scenario_simulation(scenario, verbose=True, trace=True)
    assert passed, "\n".join(errors)
