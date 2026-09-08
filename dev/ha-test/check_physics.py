"""Check the simulator's derived physics without booting a config flow.

The grid CTs here are computed by a stack of Jinja templates, and a wrong sign
or a missed phase would look like an engine bug rather than a rig bug — the
kind of confusion that costs an afternoon. This renders those templates against
stubbed states and checks the arithmetic against hand-computed answers.

WHAT IT CANNOT CHECK, and this bit matters: it renders the templates itself,
against its own fixed-point loop. Home Assistant's dependency TRACKING is not
exercised at all — so a template that is arithmetically perfect but never
re-renders passes here and is broken in the instance. That is exactly what
happened: the per-phase aggregates matched their sources with
``states.sensor | selectattr('object_id', ...)``, which gives Home Assistant no
trackable entity, so it rendered them once at startup and never again. This
script was green throughout. Only the running instance showed it (the aggregate
sat 34 minutes stale while its source had moved 3 minutes earlier).

The aggregates now name their sources with ``states('...')``, which is tracked.
The test below pins the property that made the tidy version tempting — a
deleted device package still contributes 0 rather than breaking the sum.

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
    """Pull every template entity out of the packages.

    Returns (state-based, trigger-based): the first settle to a fixed point on
    every change, the second only advance when their trigger fires, and they
    can read their own previous state — which is what makes a delay line
    possible at all, and what has to be simulated tick by tick.
    """
    plain, triggered = [], []
    for path in sorted(glob.glob(f"{CFG}/packages/*.yaml")):
        doc = yaml.safe_load(open(path))
        for block in doc.get("template") or []:
            bucket = triggered if "trigger" in block else plain
            for kind in ("sensor", "switch", "number"):
                for ent in block.get(kind) or []:
                    obj = ent["name"].lower().replace(" ", "_")
                    bucket.append(
                        (f"{kind}.{obj}", ent.get("state"), ent.get("attributes") or {})
                    )
        for domain in ("input_number", "input_boolean", "input_select"):
            for obj in (doc.get(domain) or {}):
                plain.append((f"{domain}.{obj}", None, {}))
    return plain, triggered


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


def render(expr, env, this=None):
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
        this=this,
    ).strip()


ATTRS = {}   # entity_id -> {attribute: value}, as the delay lines keep them


def settle(exprs, env, rounds=8):
    """Drive the state-based templates to a fixed point, as HA's own
    change-propagation does."""
    for _ in range(rounds):
        for eid, expr in exprs.items():
            STATE[eid] = render(expr, env)


def tick(triggered, env):
    """One firing of the 5-second time pattern.

    `state` and `attributes` are both rendered against the entity's state
    BEFORE the run and committed together — which is precisely what makes
    `this.attributes.pending` the value from one tick ago rather than this
    one's. Rendering them in sequence, committing as you go, would collapse
    the delay to nothing and the rig would look instant again.
    """
    pending = {}
    for eid, expr, attr_exprs in triggered:
        this = SimpleNamespace(
            entity_id=eid,
            state=STATE.get(eid, "unknown"),
            attributes=ATTRS.get(eid, {}),
        ) if eid in STATE else None
        pending[eid] = (
            render(expr, env, this=this),
            {k: render(v, env, this=this) for k, v in attr_exprs.items()},
        )
    for eid, (state, attrs) in pending.items():
        STATE[eid] = state
        ATTRS[eid] = attrs


def main():
    env = make_env()
    plain, triggered = collect()
    exprs = {e: s for e, s, _ in plain if s}

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
        "input_number.sim_station_ramp": "50",
        "input_boolean.sim_lag": "on",
    })

    try:
        settle(exprs, env)
        # Enough ticks for the delay lines to reach steady state, so the checks
        # below are about the physics rather than about start-up transients.
        for _ in range(8):
            tick(triggered, env)
            settle(exprs, env)
    except Exception as exc:
        print(f"RENDER FAIL: {type(exc).__name__}: {exc}")
        import traceback; traceback.print_exc()
        return 1

    def g(e):
        return float(STATE[e])

    print("--- scenario: 1 kW house on A, 6 kW solar on ABC, 2 kW plug on A ---")
    for e in sorted(STATE):
        if e.startswith(("sensor.sim_grid", "sensor.sim_site_load",
                         "sensor.sim_inverter", "sensor.sim_ct")):
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

    # Deleting a device package must not break the site's physics: the
    # aggregate names each contribution explicitly, and `float(0)` turns a
    # missing entity into a 0 rather than an error or an "unknown".
    print("\n--- a deleted device package contributes 0 ---")
    del STATE["sensor.sim_managed_tank_a"]
    del STATE["sensor.sim_managed_station_a"]
    again = float(render(exprs["sensor.sim_site_load_a"], env))
    ok = abs(again - 2000.0) < 0.1      # the plug's 2 kW, and nothing else
    print(f"  {'ok ' if ok else 'FAIL'} aggregate survives missing sources: {again}")
    if not ok:
        fails.append("missing-source")
    settle(exprs, env)

    # A second load on the same phase must add.
    print("\n--- tank switched on, on phase A too ---")
    STATE["input_boolean.sim_tank_element"] = "on"
    STATE["input_select.sim_tank_phase"] = "A"
    settle(exprs, env)
    for _ in range(8):
        tick(triggered, env)
        settle(exprs, env)
    check("site load A (plug + tank)", g("sensor.sim_site_load_a"), 4000.0, 0.1)
    # 1000 + 4000 - 2000 = 3000 W -> 13.04 A import on A
    check("grid A", g("sensor.sim_grid_current_a"), 3000 / 230)
    # 6000 - 1000 - 4000 = 1000 W exported
    check("net power", g("sensor.sim_grid_net_power"), -1000.0, 5)

    # --- the transient, which is the whole reason the lag exists ----------
    # A step change must reach the engine LATE. If it arrives the same tick,
    # the rig cannot reproduce an over-commitment: the engine would always see
    # the effect of its own last grant before deciding the next one.
    print("\n--- one tick of CT delay ---")
    STATE["input_boolean.sim_tank_element"] = "off"
    settle(exprs, env)
    for _ in range(6):
        tick(triggered, env)
        settle(exprs, env)
    before = g("sensor.sim_grid_current_a")

    # The step: the tank's 2 kW element closes on phase A.
    STATE["input_boolean.sim_tank_element"] = "on"
    STATE["input_select.sim_tank_phase"] = "A"
    settle(exprs, env)
    check("truth moved at once", g("sensor.sim_grid_instant_a"), before + 2000 / 230)
    check("meter has not moved yet", g("sensor.sim_grid_current_a"), before)

    tick(triggered, env); settle(exprs, env)
    check("still one tick behind", g("sensor.sim_grid_current_a"), before)
    check("and the lag is visible", g("sensor.sim_ct_lag_error"), 2000.0, 5)

    tick(triggered, env); settle(exprs, env)
    check("caught up on the next tick",
          g("sensor.sim_grid_current_a"), before + 2000 / 230)
    check("lag back to zero", g("sensor.sim_ct_lag_error"), 0.0, 5)

    print("\n--- the station's converter ramps rather than steps ---")
    STATE["input_number.sim_station_charge_speed_raw"] = "1000"
    settle(exprs, env)
    check("register target", g("sensor.sim_station_input_target"), 1000.0, 0.1)
    seen = []
    for _ in range(6):
        tick(triggered, env)
        settle(exprs, env)
        seen.append(g("sensor.sim_station_ac_input"))
    print(f"  ramp: {seen}")
    # Half the remaining gap each tick, then snapped once inside 10 W.
    check("first tick is halfway", seen[0], 500.0, 1)
    check("second tick is three quarters", seen[1], 750.0, 1)
    # The last 50 W is snapped, so it arrives instead of creeping forever.
    check("arrives at the target", seen[-1], 1000.0, 0.1)

    print("\n--- ramp rate 100: a converter, not a car ---")
    STATE["input_number.sim_station_ramp"] = "100"
    STATE["input_number.sim_station_charge_speed_raw"] = "1500"
    settle(exprs, env)
    tick(triggered, env); settle(exprs, env)
    check("there in one tick", g("sensor.sim_station_ac_input"), 1500.0, 0.1)
    if not all(b >= a for a, b in zip(seen, seen[1:])):
        print("  FAIL ramp is not monotonic")
        fails.append("ramp monotonic")
    else:
        print("  ok  ramp is monotonic")

    print("\n--- a three-phase station splits its draw ---")
    # The engine derives phases from len(connected_to_phase) and spreads the
    # draw the same way, so a mask the rig splits evenly is exactly what it
    # expects. Pinned here because an exact-match phase test would silently
    # report ZERO draw for "ABC" — the load would vanish from the physics
    # while still being granted power.
    STATE["input_select.sim_station_phase"] = "ABC"
    STATE["input_number.sim_station_charge_speed_raw"] = "3000"
    STATE["input_number.sim_station_ramp"] = "100"
    settle(exprs, env)
    tick(triggered, env); settle(exprs, env)
    check("draws its register", g("sensor.sim_station_ac_input"), 3000.0, 0.1)
    for letter in "abc":
        check(f"one third on {letter.upper()}",
              g(f"sensor.sim_managed_station_{letter}"), 1000.0, 0.1)
    STATE["input_select.sim_station_phase"] = "C"
    STATE["input_number.sim_station_charge_speed_raw"] = "1000"
    settle(exprs, env)
    tick(triggered, env); settle(exprs, env)
    check("single phase puts it all on C",
          g("sensor.sim_managed_station_c"), 1000.0, 0.1)
    check("and nothing on A", g("sensor.sim_managed_station_a"), 0.0, 0.1)

    print("\n--- lag off: measurements are instant again ---")
    STATE["input_boolean.sim_lag"] = "off"
    STATE["input_number.sim_station_charge_speed_raw"] = "300"
    STATE["input_boolean.sim_tank_element"] = "off"
    settle(exprs, env)
    tick(triggered, env); settle(exprs, env)
    check("meter equals the truth",
          g("sensor.sim_grid_current_a"), g("sensor.sim_grid_instant_a"))
    check("station jumps to its register", g("sensor.sim_station_ac_input"), 300.0, 0.1)

    print(f"\n{'FAILED: ' + ', '.join(fails) if fails else 'ALL CHECKS PASSED'}")
    return 1 if fails else 0


sys.exit(main())
