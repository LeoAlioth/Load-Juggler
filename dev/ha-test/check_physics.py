"""Check the simulator's derived physics without booting a config flow.

The grid CTs here are computed by a stack of Jinja templates, and a wrong sign
or a missed phase would look like an engine bug rather than a rig bug — the
kind of confusion that costs an afternoon. This renders those templates against
stubbed states and checks the arithmetic against hand-computed answers.

It also pins the naming rule the site package depends on: the per-phase
aggregate sums every ``sensor.sim_managed_<device>_<phase>``, so an aggregate
named that way would sum itself.

Run it inside the container, which already has jinja2 and PyYAML:

    docker exec load-juggler-test python3 /config/../dev/ha-test/check_physics.py

or, since the config dir is what it reads:

    docker cp dev/ha-test/check_physics.py load-juggler-test:/tmp/ && \
      docker exec load-juggler-test python3 /tmp/check_physics.py
"""
import sys, glob, os
import yaml
from jinja2 import Environment
from types import SimpleNamespace

# Inside the container the packages live at /config/packages; override with
# SIM_CONFIG to run this against a checkout instead.
CFG = os.environ.get("SIM_CONFIG", "/config")

STATE = {}          # entity_id -> string state


def collect():
    """Pull every template sensor/switch/number out of the packages."""
    items = []
    for path in sorted(glob.glob(f"{CFG}/packages/*.yaml")):
        doc = yaml.safe_load(open(path))
        for block in doc.get("template") or []:
            for kind in ("sensor", "switch", "number"):
                for ent in block.get(kind) or []:
                    obj = ent["name"].lower().replace(" ", "_")
                    items.append((f"{kind}.{obj}", ent.get("state"), path))
        for domain in ("input_number", "input_boolean", "input_select"):
            for obj in (doc.get(domain) or {}):
                items.append((f"{domain}.{obj}", None, path))
    return items


def make_env():
    env = Environment()
    env.filters["float"] = lambda v, d=0.0: (
        float(v) if str(v).replace("-", "").replace(".", "").isdigit() else d
    )
    env.filters["round"] = lambda v, n=0: round(float(v), n)
    # Home Assistant registers `match` and `search` as both filters and tests;
    # bare Jinja has neither, so the stub must supply them.
    import re as _re
    env.tests["match"] = lambda v, p: _re.match(p, str(v)) is not None
    env.tests["search"] = lambda v, p: _re.search(p, str(v)) is not None
    env.filters["match"] = env.tests["match"]
    env.filters["search"] = env.tests["search"]
    return env


def render(expr, env):
    def states(eid=None):
        if eid is None:
            return None
        return STATE.get(eid, "unknown")

    class Domain(list):
        pass

    sensors = Domain(
        SimpleNamespace(entity_id=e, object_id=e.split(".", 1)[1], state=v)
        for e, v in sorted(STATE.items())
        if e.startswith("sensor.")
    )
    states_obj = SimpleNamespace(sensor=sensors)
    # `states` must be callable AND attribute-accessible, like HA's.
    class States:
        def __call__(self, eid=None):
            return states(eid)
        sensor = sensors
    return env.from_string(expr).render(
        states=States(),
        is_state=lambda e, v: STATE.get(e) == v,
    ).strip()


def main():
    env = make_env()
    items = collect()
    exprs = {e: s for e, s, _ in items if s}

    # --- the scenario -------------------------------------------------
    STATE.update({
        "input_number.sim_household_a": "1000",
        "input_number.sim_household_b": "0",
        "input_number.sim_household_c": "0",
        "input_number.sim_solar": "6000",
        "input_number.sim_battery_power": "0",
        "input_number.sim_battery_soc": "55",
        "input_number.sim_voltage": "230",
        "input_select.sim_inverter_phases": "ABC",
        "input_boolean.sim_plug_relay": "on",
        "input_number.sim_plug_power": "2000",
        "input_select.sim_plug_phase": "A",
        "input_boolean.sim_tank_element": "off",
        "input_number.sim_tank_element_power": "2000",
        "input_number.sim_tank_temperature": "40",
        "input_select.sim_tank_phase": "B",
        "input_number.sim_station_battery": "50",
        "input_number.sim_station_charge_speed_raw": "0",
        "input_number.sim_station_reserve_raw": "20",
        "input_select.sim_station_phase": "C",
    })

    # Resolve in dependency order by iterating to a fixed point.
    for _ in range(8):
        for eid, expr in exprs.items():
            try:
                STATE[eid] = render(expr, env)
            except Exception as exc:
                print(f"RENDER FAIL {eid}: {type(exc).__name__}: {exc}")
                return 1

    def g(e):
        return float(STATE[e])

    print("--- scenario: 1 kW house on A, 6 kW solar on ABC, 2 kW plug on A ---")
    for e in sorted(STATE):
        if e.startswith(("sensor.sim_grid", "sensor.sim_site_load",
                         "sensor.sim_inverter")):
            print(f"  {e:38s} {STATE[e]}")

    fails = []

    def check(label, got, want, tol=0.02):
        ok = abs(got - want) <= tol
        print(f"  {'ok ' if ok else 'FAIL'} {label}: {got} (want {want})")
        if not ok:
            fails.append(label)

    print("\n--- checks ---")
    check("inverter per phase", g("sensor.sim_inverter_output_a"), 2000.0, 0.1)
    check("site load A (plug only)", g("sensor.sim_site_load_a"), 2000.0, 0.1)
    check("site load B", g("sensor.sim_site_load_b"), 0.0, 0.1)
    # A: 1000 house + 2000 plug - 2000 inverter = 1000 W -> 4.35 A import
    check("grid A", g("sensor.sim_grid_current_a"), 1000 / 230)
    # B and C: 0 - 2000 = -2000 W -> -8.70 A export
    check("grid B", g("sensor.sim_grid_current_b"), -2000 / 230)
    check("grid C", g("sensor.sim_grid_current_c"), -2000 / 230)
    # 6000 solar - 1000 house - 2000 plug = 3000 W exported
    check("net power", g("sensor.sim_grid_net_power"), -3000.0, 5)
    check("plug power monitor", g("sensor.sim_plug_power"), 2000.0, 0.1)
    check("tank power (element off)", g("sensor.sim_tank_power"), 0.0, 0.1)

    # The self-reference guard: the aggregate must NOT include itself.
    print("\n--- the self-sum guard ---")
    STATE["sensor.sim_site_load_a"] = "999999"
    again = float(render(exprs["sensor.sim_site_load_a"], env))
    ok = abs(again - 2000.0) < 0.1
    print(f"  {'ok ' if ok else 'FAIL'} aggregate ignores its own state: {again}")
    if not ok:
        fails.append("self-sum")

    # A second load on the same phase must add.
    print("\n--- tank switched on, on phase A too ---")
    STATE["input_boolean.sim_tank_element"] = "on"
    STATE["input_select.sim_tank_phase"] = "A"
    for _ in range(8):
        for eid, expr in exprs.items():
            STATE[eid] = render(expr, env)
    check("site load A (plug + tank)", g("sensor.sim_site_load_a"), 4000.0, 0.1)
    # 1000 + 4000 - 2000 = 3000 W -> 13.04 A import on A
    check("grid A", g("sensor.sim_grid_current_a"), 3000 / 230)
    # 6000 - 1000 - 4000 = 1000 W exported
    check("net power", g("sensor.sim_grid_net_power"), -1000.0, 5)

    print(f"\n{'FAILED: ' + ', '.join(fails) if fails else 'ALL CHECKS PASSED'}")
    return 1 if fails else 0


sys.exit(main())
