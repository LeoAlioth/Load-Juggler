"""A moving surplus: does a modulating load track it, or churn its register?

At a FIXED operating point the station's permit rings and then damps to a
steady value (measured 2026-09-08: spread 552 -> 0 W over 150 s). That makes
the fixed-point case a poor test of the control loop - the interesting question
is what happens when the surplus never stops moving, which is what a real site
does under passing cloud.

Drives the array on a slow sinusoid and reports how well the permit tracks the
surplus, and how much the charge register is made to move. Run it before and
after any change to the rate limiting; the numbers to compare are the mean
tracking error and the register writes per minute.

    python3 dev/ha-test/moving_surplus.py

Needs the same token as scenarios.py.
"""
import math
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rig import get, num, select, toggle

V = 230.0
HOUSEHOLD_PHASE_W = 300.0    # balanced, so the baseline adds no phase effect
HOUSEHOLD_W = HOUSEHOLD_PHASE_W * 3
EXPORT_LIMIT_W = 11000.0     # what the inverter itself will hold
THRESHOLD_W = 10500.0        # the export limit less the 500 W trigger margin
TANK_W = 2000.0
STATION_MIN_W, STATION_MAX_W = 200.0, 2400.0

# The swing is sized so the STATION ALONE can absorb all of it, which is what
# makes this a tracking test rather than a saturation one. Its usable band is
# 200-2 400 W, i.e. 2 200 W wide, so the largest fully absorbable swing is
# +/-1 100 W centred where the station sits at 1 300 W. The earlier +/-2 500 W
# was 5 kW peak to peak against 2.2 kW of range: the permit spent most of the
# cycle pinned at one end, and the number that came out measured the clipping.
#
# The centre follows from the rest of the site rather than being chosen: the
# station's ideal is `solar - household - tank - threshold`, so the solar mean
# has to sit 1 300 W above the point where that reaches zero.
#     13 400 = 900 household + 2 000 tank + 10 500 threshold
#     14 700 = 13 400 + 1 300  ->  13.6 to 15.8 kW
# Do not drop the centre without recomputing it. At a mean that lets the ideal
# fall under STATION_MIN_W the station is shed for tens of seconds per period,
# which outlasts the grace window and arms the 3-minute charge pause - the rest
# of the run then measures a stopped load.
MEAN_W, AMPLITUDE_W = 14700.0, 1100.0
PERIOD_S, STEP_S, PERIODS = 150, 5, 2


def ideal_station_w(solar_w, tank_running):
    """What the station should be permitted, from first principles.

    The surplus is the reconstruction less the threshold; the tank outranks the
    station and claims its whole element while it is boosting, so what is left
    is the station's. Clamped to what the device accepts.
    """
    surplus = (solar_w - HOUSEHOLD_W) - THRESHOLD_W
    left = surplus - (TANK_W if tank_running else 0.0)
    if left < STATION_MIN_W:
        return 0.0
    return min(left, STATION_MAX_W)


def setup():
    for phase in "abc":
        num(f"input_number.sim_household_{phase}", HOUSEHOLD_PHASE_W)
    num("input_number.sim_voltage", 230)
    num("input_number.sim_export_limit", EXPORT_LIMIT_W)
    toggle("input_boolean.sim_curtail", True)
    num("input_number.sim_battery_power", 0)
    num("input_number.sim_station_charge_speed_raw", 0)
    num("input_number.sim_station_ramp", 100)
    num("input_number.sim_tank_temperature", 45)
    select("input_select.sim_inverter_phases", "ABC")
    select("input_select.sim_tank_phase", "B")
    select("input_select.sim_station_phase", "C")
    toggle("input_boolean.sim_lag", True)
    toggle("input_boolean.sim_plug_relay", False)
    toggle("switch.smart_load_dynamic_control", False)
    toggle("switch.hot_water_tank_dynamic_control", True)
    toggle("switch.power_station_dynamic_control", True)
    num("input_number.sim_solar", MEAN_W)


def main():
    setup()
    print(f"settling 90 s at the mean ({MEAN_W:.0f} W)...")
    time.sleep(90)

    samples = []
    total = PERIOD_S * PERIODS
    print(f"driving solar {MEAN_W:.0f} +/- {AMPLITUDE_W:.0f} W over "
          f"{PERIODS} periods of {PERIOD_S} s\n")
    print(f"{'t':>4} {'solar':>7} {'ideal':>7} {'permit':>7} {'reg':>6} {'err':>6} "
          f"{'lost':>6}  tank")
    start = time.monotonic()
    while True:
        t = time.monotonic() - start
        if t > total:
            break
        solar = MEAN_W + AMPLITUDE_W * math.sin(2 * math.pi * t / PERIOD_S)
        num("input_number.sim_solar", round(solar))
        tank = get("sensor.hot_water_tank_tank_status")
        permit = float(get("sensor.power_station_available_current")) * V
        reg = float(get("number.sim_station_charge_speed"))
        lost = float(get("sensor.sim_curtailed_power") or 0)
        ideal = ideal_station_w(solar, tank == "Heating")
        samples.append((t, solar, ideal, permit, reg, tank, lost))
        print(f"{t:>4.0f} {solar:>7.0f} {ideal:>7.0f} {permit:>7.0f} {reg:>6.0f} "
              f"{permit - ideal:>+6.0f} {lost:>6.0f}  {tank}")
        time.sleep(max(0.0, STEP_S - (time.monotonic() - start - t)))

    errs = [abs(p - i) for _, _, i, p, _, _, _ in samples]
    regs = [s[4] for s in samples]
    losts = [s[6] for s in samples]
    writes = sum(1 for a, b in zip(regs, regs[1:]) if a != b)
    minutes = total / 60.0
    print()
    print(f"  samples                  : {len(samples)} over {total} s")
    print(f"  tracking error  mean/max : {sum(errs)/len(errs):.0f} / {max(errs):.0f} W")
    print(f"  register writes           : {writes}  ({writes/minutes:.1f} per minute)")
    print(f"  register range            : {min(regs):.0f}-{max(regs):.0f} W")
    print(f"  tank states seen          : {sorted({s[5] for s in samples})}")
    # The number that matters physically. With the inverter holding its own
    # export limit, unabsorbed surplus is not "extra export" - it is
    # production the array never made, and this is the meter for it.
    print(f"  curtailed  mean/max       : {sum(losts)/len(losts):.0f} / {max(losts):.0f} W")


if __name__ == "__main__":
    main()
