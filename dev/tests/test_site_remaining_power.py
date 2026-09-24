"""Site Remaining Power is the physical pool the loads were offered.

Machine-authored tests - not yet human-reviewed.

``engine/hub_result.py`` used to re-derive Site Remaining Power (and its
per-phase, grid and inverter breakdown) from the site's headroom terms - grid
breaker headroom + solar less the house + battery discharge not yet flowing,
capped at the inverter's rating less its current output - while the chargers
were sized from the ``PhaseConstraints`` the calculator built. Two derivations
of one quantity, and they disagreed: settled, 95 of 223 scenarios published a
Site Remaining Power more than 5 W from the pool. 8 over it, by up to
22 080 W, all with *Allow Grid Charging* off - the breaker published as
headroom the engine never grants; 87 under it, by up to 14 950 W - the
inverter's current output, our own loads' draw included, taken off its rating,
where the pool hands that draw back.

The published figures now come FROM those pools (``SiteContext.pool_snapshot``,
the same dict the Overview shows as the pool detail). Pinned here on every
settled cycle of every YAML scenario: each published figure equals the pool
it names, to the rounding the sensor publishes.

Solar Remaining Power / Current joined them later. It was re-derived as the
solar production less ``household_consumption_total`` - a total built only
beside a production sensor - and wherever that total was missing nothing was
taken off: 52 of 230 scenarios published a figure more than 2 W from the sun's
share of the solar pool their Solar loads were offered, on some cycle, from
+5 599 W (a series hybrid's output sensor reading the import passing through
it, all of it published as sun) to -4 000 W (a solar sensor reading 0 W at
night beside a battery whose power is not read, whose export the pool
offers). It now reads the pool's ``sun`` share
(``target_calculator._calculate_solar_surplus``).

Pure Python, no Home Assistant dependencies. Runnable two ways:
  python3 dev/tests/test_site_remaining_power.py   (standalone, no pytest)
  pytest dev/tests/test_site_remaining_power.py    (Docker / CI tier)
"""

import contextlib
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from standalone_loader import load_pure_modules

load_pure_modules(engine_modules=("fleet", "hub_calculation"))

import run_tests  # noqa: E402 - the scenario harness drives the sites
from custom_components.dynamic_ocpp_evse.engine.hub_result import (  # noqa: E402
    _build_hub_result,
)

SCENARIOS = Path(__file__).resolve().parent / "scenarios"
# The pool snapshot rounds to 0.01 A; at 230 V that is 1.15 W, and the
# published watts round to 1 W.
TOLERANCE_W = 2.0
TOLERANCE_A = 0.051


def _published(site):
    """The hub result for a site the calculator has just run on."""
    return _build_hub_result(
        site,
        raw_phases=(
            (site.grid_current.a, site.grid_current.b, site.grid_current.c)
            if site.grid_current is not None
            else (None, None, None)
        ),
        voltage=site.voltage,
        battery_soc=site.battery_soc,
        battery_soc_min=site.battery_soc_min,
        battery_max_discharge_power=site.battery_max_discharge_power,
        battery_power=site.battery_power,
        load_targets={},
        load_available={},
        load_names={},
    )


def _disagreements(site):
    """Every published figure that is not the pool it names."""
    result = _published(site)
    detail = site.pool_snapshot
    physical = detail["physical"]["start"]
    v = site.voltage
    out = []
    checks = [("total_site_available_power", physical["ABC"] * v, TOLERANCE_W)]
    for part in ("grid", "inverter", "sun"):
        if part not in detail:
            out.append(f"the pool snapshot has no {part} part")
    if not out:
        grid = detail["grid"]["start"]["ABC"]
        # Solar Remaining: the sun's share of the solar pool, nothing below 0.
        sun = max(0.0, detail["sun"]["start"]["ABC"])
        checks += [
            ("available_grid_power", grid * v, TOLERANCE_W),
            ("available_grid_current", grid, TOLERANCE_A),
            ("available_inverter_current", detail["inverter"]["start"]["ABC"], TOLERANCE_A),
            ("available_solar_power", sun * v, TOLERANCE_W),
            ("available_solar_current", sun, TOLERANCE_A),
        ]
    for key, phase in zip(
        ("available_current_a", "available_current_b", "available_current_c"),
        "ABC",
    ):
        # What a single-phase load on that phase could be offered.
        checks.append((key, min(physical[phase], physical["ABC"]), TOLERANCE_A))
    for key, pool, tolerance in checks:
        if abs(result[key] - pool) > tolerance:
            out.append(f"{key} {result[key]} where the pool is {pool:.2f}")
    return out


def _scenario_disagreements():
    """(scenario, cycle, [disagreements]) for every cycle of every scenario."""
    found = []
    real = run_tests.calculate_all_load_targets
    current = {}

    def calculate_and_compare(site):
        real(site)
        current["cycle"] = current.get("cycle", -1) + 1
        wrong = _disagreements(site)
        if wrong:
            found.append((current["name"], current["cycle"], wrong))

    run_tests.calculate_all_load_targets = calculate_and_compare
    try:
        for path in sorted(SCENARIOS.rglob("*.yaml")):
            for scenario in run_tests.load_scenarios(path):
                current.clear()
                current["name"] = scenario["name"]
                with contextlib.redirect_stdout(io.StringIO()):
                    run_tests.run_scenario_simulation(scenario)
    finally:
        run_tests.calculate_all_load_targets = real
    return found


def test_every_published_remaining_figure_is_the_pool_it_names():
    """On every cycle of every scenario. Before the fix, measured: 95 of 223
    scenarios settled on a Site Remaining Power more than 5 W from the pool
    their loads were offered, from +22 080 W to -14 950 W; and 52 of 230
    published a Solar Remaining Power more than 2 W from the solar pool's sun
    share, from +5 599 W to -4 000 W."""
    found = _scenario_disagreements()
    scenarios = sorted({name for name, *_ in found})
    # The headline figure on its own, in watts: the worst Site Remaining Power.
    worst = max(
        (
            (float(w.split()[1]) - float(w.split()[-1]) * 1.0, name, cycle)
            for name, cycle, wrong in found
            for w in wrong
            if w.startswith("total_site_available_power")
        ),
        key=lambda item: abs(item[0]),
        default=None,
    )
    assert not found, (
        f"{len(scenarios)} scenarios publish figures that are not their pools"
        + (
            f"; Site Remaining Power worst {worst[0]:+.0f} W off the pool "
            f"({worst[1]} cycle {worst[2]})"
            if worst
            else ""
        )
        + f"; e.g. {found[0][0]} cycle {found[0][1]}: {'; '.join(found[0][2])}"
    )


if __name__ == "__main__":
    # Deliberately pytest-free: the pure tier has to run on the developer's
    # machine, which has no pytest (dev/tests/conftest.py imports HA anyway).
    failed = []
    for _name, _fn in sorted(list(globals().items())):
        if not _name.startswith("test_") or not callable(_fn):
            continue
        try:
            _fn()
        except Exception as exc:  # noqa: BLE001 - report and continue
            failed.append((_name, exc))
            print(f"FAIL {_name}: {type(exc).__name__}: {exc}")
        else:
            print(f"PASS {_name}")
    print(f"\n{'FAILED' if failed else 'OK'} - {len(failed)} failure(s)")
    sys.exit(1 if failed else 0)
