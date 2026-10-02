"""Check the simulator's physics on the running rig.

The grid CTs are computed by a stack of template sensors (config/packages), and
a wrong sign or a missed phase would look like an engine bug rather than a rig
bug - the kind of confusion that costs an afternoon. This sets the simulator's
knobs through the REST API, waits on the rig's own 5-second sample clock, and
reads the template sensors back. Home Assistant renders them, so what is checked
is its real template engine AND its dependency tracking: a template that is
arithmetically right but never re-renders reads stale here, and fails.

    python3 dev/ha-test/check_physics.py

Needs the rig up and its token (see rig.py); takes about two minutes.
Load Juggler's dynamic control is switched off on the three loads for the run,
so the engine does not move the knobs under test, and every knob touched is put
back afterwards.
"""
import os, subprocess, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rig import BASE, TOKEN, _curl, get, num, select, toggle

# The scenario: 1 kW house on A, 6 kW solar on ABC, a 2 kW plug on A. Every
# knob the run touches is in here, so this is also what gets restored.
SITE = {
    "input_number.sim_household_a": 1000,
    "input_number.sim_household_b": 0,
    "input_number.sim_household_c": 0,
    "input_number.sim_solar": 6000,
    "input_number.sim_battery_power": 0,
    "input_number.sim_voltage": 230,
    "input_number.sim_export_limit": 11000,
    "input_boolean.sim_curtail": "on",
    "input_select.sim_inverter_phases": "ABC",
    "input_boolean.sim_plug_relay": "on",
    "input_number.sim_plug_power": 2000,
    "input_select.sim_plug_phase": "A",
    "input_boolean.sim_tank_element": "off",
    "input_number.sim_tank_element_power": 2000,
    "input_select.sim_tank_phase": "B",
    "input_number.sim_station_battery": 50,
    "input_number.sim_station_charge_speed_raw": 0,
    "input_select.sim_station_phase": "C",
    "input_number.sim_station_ramp": 100,   # settles in one sample; the ramp test sets 50
    "input_boolean.sim_lag": "on",
}
ENGINE = ("switch.smart_load_dynamic_control",
          "switch.power_station_dynamic_control",
          "switch.hot_water_tank_dynamic_control")


def put(knobs):
    for e, v in knobs.items():
        kind = e.split(".")[0]
        if kind == "input_number":
            num(e, v)
        elif kind == "input_select":
            select(e, v)
        else:
            toggle(e, v == "on")


def g(e):
    return float(get(e))


# HA's clock minus ours. Docker Desktop's VM clock can drift from the Mac's
# (after a sleep, notably), and the samples fire on HA's.
OFFSET = 0.0


def tick(n=1):
    """Sleep through n samples (the packages' `time_pattern: /5`) and wake 2 s
    after the last: the sample is committed and the state templates downstream
    have followed, and the next one is 3 s away - room for a step that must
    land BETWEEN two samples."""
    time.sleep(5 * n - (time.time() + OFFSET) % 5 + 2)


fails = []


def check(label, got, want, tol=0.02):
    ok = abs(got - want) <= tol
    print(f"  {'ok ' if ok else 'FAIL'} {label}: {got} (want {want:g})")
    if not ok:
        fails.append(label)


def run():
    put(SITE)
    tick(3)
    print("--- scenario: 1 kW house on A, 6 kW solar on ABC, 2 kW plug on A ---")
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

    # A second load on the same phase must add.
    print("\n--- tank switched on, on phase A too ---")
    put({"input_boolean.sim_tank_element": "on", "input_select.sim_tank_phase": "A"})
    tick(3)
    check("site load A (plug + tank)", g("sensor.sim_site_load_a"), 4000.0, 0.1)
    # 1000 + 4000 - 2000 = 3000 W -> 13.04 A import on A
    check("grid A", g("sensor.sim_grid_current_a"), 3000 / 230)
    # 6000 - 1000 - 4000 = 1000 W exported
    check("net power", g("sensor.sim_grid_net_power"), -1000.0, 5)

    # Deleting a device package must not break the site's physics: the
    # aggregate names each contribution, and `float(0)` turns a missing entity
    # into a 0 rather than an error. Removing the tank's state from the state
    # machine is what deleting its package does, without the restart.
    print("\n--- a deleted device package contributes 0 ---")
    subprocess.run(["curl", "-s", "-X", "DELETE", "-H", f"Authorization: Bearer {TOKEN}",
                    f"{BASE}/states/sensor.sim_managed_tank_a"], capture_output=True)
    time.sleep(1)
    check("aggregate drops the missing source", g("sensor.sim_site_load_a"), 2000.0, 0.1)

    # A step change must reach the engine LATE. If it arrives the same sample,
    # the rig cannot reproduce an over-commitment: the engine would always see
    # the effect of its own last grant before deciding the next one.
    print("\n--- one sample of CT delay ---")
    put({"input_boolean.sim_tank_element": "off"})   # also writes the tank's sensor back
    tick(3)
    before = g("sensor.sim_grid_current_a")
    put({"input_boolean.sim_tank_element": "on"})    # the tank's 2 kW, on A
    time.sleep(0.5)
    check("truth moved at once", g("sensor.sim_grid_instant_a"), before + 2000 / 230)
    check("meter has not moved yet", g("sensor.sim_grid_current_a"), before)
    tick()
    check("still one sample behind", g("sensor.sim_grid_current_a"), before)
    check("and the lag is visible", g("sensor.sim_ct_lag_error"), 2000.0, 5)
    tick()
    check("caught up on the next sample", g("sensor.sim_grid_current_a"), before + 2000 / 230)
    check("lag back to zero", g("sensor.sim_ct_lag_error"), 0.0, 5)

    # Two stages, different physics: the converter ramps (`draw`), and the
    # meter on it lags (`ac_input`). Which of the two samples HA runs first at
    # a given tick is its own business, so the meter trails the draw by one
    # sample or two - never zero.
    print("\n--- the station's converter ramps rather than steps ---")
    put({"input_number.sim_station_ramp": 50,
         "input_number.sim_station_charge_speed_raw": 1000})
    time.sleep(0.5)
    check("register target", g("sensor.sim_station_input_target"), 1000.0, 0.1)
    draw, meter = [], []
    for _ in range(8):
        tick()
        draw.append(g("sensor.sim_station_draw"))
        meter.append(g("sensor.sim_station_ac_input"))
    print(f"  draw:  {draw}\n  meter: {meter}")
    # Half the remaining gap each sample, then snapped once inside 50 W.
    check("first sample is halfway", draw[0], 500.0, 1)
    check("second sample is three quarters", draw[1], 750.0, 1)
    check("arrives at the target", draw[-1], 1000.0, 0.1)
    check("meter has not moved after the first sample", meter[0], 0.0, 0.1)
    trails = all(m in [0.0, *draw[:k]] for k, m in enumerate(meter))
    print(f"  {'ok ' if trails else 'FAIL'} meter reads an earlier draw every sample")
    if not trails:
        fails.append("meter trails the draw")
    check("meter arrives too", meter[-1], 1000.0, 0.1)
    monotonic = all(b >= a for a, b in zip(draw, draw[1:]))
    print(f"  {'ok ' if monotonic else 'FAIL'} ramp is monotonic")
    if not monotonic:
        fails.append("ramp monotonic")

    print("\n--- ramp rate 100: a converter, not a car ---")
    put({"input_number.sim_station_ramp": 100,
         "input_number.sim_station_charge_speed_raw": 1500})
    tick()
    check("there in one sample", g("sensor.sim_station_draw"), 1500.0, 0.1)

    # The engine derives phases from len(connected_to_phase) and spreads the
    # draw the same way, so a mask the rig splits evenly is exactly what it
    # expects. Pinned because an exact-match phase test would silently report
    # ZERO draw for "ABC" - the load would vanish from the physics while still
    # being granted power.
    print("\n--- a three-phase station splits its draw ---")
    put({"input_select.sim_station_phase": "ABC",
         "input_number.sim_station_charge_speed_raw": 3000})
    tick()
    check("draws its register", g("sensor.sim_station_draw"), 3000.0, 0.1)
    for letter in "abc":
        check(f"one third on {letter.upper()}",
              g(f"sensor.sim_managed_station_{letter}"), 1000.0, 0.1)
    put({"input_select.sim_station_phase": "C",
         "input_number.sim_station_charge_speed_raw": 1000})
    tick()
    check("single phase puts it all on C", g("sensor.sim_managed_station_c"), 1000.0, 0.1)
    check("and nothing on A", g("sensor.sim_managed_station_a"), 0.0, 0.1)

    # Ramp back at 50 so the station's jump is the lag switch's doing.
    print("\n--- lag off: measurements are instant again ---")
    put({"input_boolean.sim_lag": "off", "input_boolean.sim_tank_element": "off",
         "input_number.sim_station_ramp": 50,
         "input_number.sim_station_charge_speed_raw": 300})
    tick()
    check("meter equals the truth", g("sensor.sim_grid_current_a"), g("sensor.sim_grid_instant_a"))
    check("station jumps to its register", g("sensor.sim_station_draw"), 300.0, 0.1)
    tick()
    check("and its meter with it", g("sensor.sim_station_ac_input"), 300.0, 0.1)


def main():
    global OFFSET
    if get("input_boolean.sim_lag") is None:
        raise SystemExit(f"The rig does not answer at {BASE} - is it up?")
    t0 = time.time()
    OFFSET = _curl("/template", {"template": "{{ as_timestamp(now()) }}"}) - (t0 + time.time()) / 2
    saved = {e: get(e) for e in (*SITE, *ENGINE)}
    put({e: "off" for e in ENGINE})
    try:
        run()
    finally:
        put({e: v for e, v in saved.items() if v is not None})
    print(f"\n{'FAILED: ' + ', '.join(fails) if fails else 'ALL CHECKS PASSED'}")
    return 1 if fails else 0


sys.exit(main())
