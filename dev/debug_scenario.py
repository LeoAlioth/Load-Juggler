#!/usr/bin/env python3
"""Debug runner for a single test scenario by name."""

import sys
import argparse
import logging
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run a single debug scenario by name (uses dev/tests scenarios)."
    )
    parser.add_argument(
        "scenario",
        help="Scenario name to run (e.g., 3ph-1c-solar-prio-with-bat-normal)",
    )
    parser.add_argument(
        "--scenarios-dir",
        default=str(Path("dev/tests/scenarios")),
        help="Directory containing scenario YAML files (default: dev/tests/scenarios)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable verbose debug logging",
    )

    args = parser.parse_args()

    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=log_level, format="%(levelname)s: %(message)s")

    repo_root = Path(__file__).parents[1]
    sys.path.insert(0, str(repo_root))

    from dev.tests.run_tests import run_single_scenario

    scenarios_dir = Path(args.scenarios_dir)
    search_paths = list(sorted(scenarios_dir.glob("*.yaml"))) + list(
        sorted(scenarios_dir.glob("*.yml"))
    )

    if not search_paths:
        logging.error("No scenario files found in %s", scenarios_dir)
        return 1

    for scenario_file in search_paths:
        if run_single_scenario(args.scenario, yaml_file=str(scenario_file)):
            return 0

    logging.error("Scenario '%s' not found in %s", args.scenario, scenarios_dir)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())