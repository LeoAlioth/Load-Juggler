"""The rig's scenario set: six site states that each exercise one
behaviour, run end to end against the real integration.

    python3 dev/ha-test/scenarios.py            # all six
    CASES=2,3 python3 dev/ha-test/scenarios.py  # just those

Each case sets the simulated site, waits for it to settle, then samples
and reports whether the DECISIONS held steady. Statuses must be exactly
constant; permits are allowed one register step of wobble, because that
is the smallest change a device can be told about.
"""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rig import num, select, toggle, settle, show, get

def base(solar, hb=300, plug_relay=False, plug_managed=False,
         stn_managed=True, tank_managed=True):
    num("input_number.sim_household_a", 300)
    num("input_number.sim_household_b", hb)
    num("input_number.sim_household_c", 300)
    num("input_number.sim_solar", solar)
    num("input_number.sim_voltage", 230)
    num("input_number.sim_battery_power", 0)
    select("input_select.sim_inverter_phases", "ABC")
    select("input_select.sim_tank_phase", "B")
    select("input_select.sim_station_phase", "C")
    num("input_number.sim_tank_temperature", 45)
    num("input_number.sim_station_battery", 50)
    # Zero the station's charge register before each scenario. Left alone it
    # keeps whatever was last written to it and the simulated station goes on
    # drawing - which confounded S1 on the previous run, showing 300 W of
    # phantom load that pushed the site under its threshold.
    num("input_number.sim_station_charge_speed_raw", 0)
    num("input_number.sim_station_ramp", 100)
    toggle("input_boolean.sim_lag", True)
    toggle("input_boolean.sim_plug_relay", plug_relay)
    toggle("switch.smart_load_dynamic_control", plug_managed)
    toggle("switch.power_station_dynamic_control", stn_managed)
    toggle("switch.hot_water_tank_dynamic_control", tank_managed)
    time.sleep(6)

CASES = [
    ("S1  tank alone, 100 W over the threshold (solar 11 500)",
     "tank ON and steady; station waits behind it",
     lambda: base(11500, stn_managed=True)),
    ("S2  tank alone, below the threshold (solar 10 800)",
     "nothing runs; recon 9 900 W is under the 10 500 W threshold",
     lambda: base(10800)),
    ("S3  sequential start (solar 14 000)",
     "tank ON, station ALSO on and MODULATING - permit well under its 2 400 W",
     lambda: base(14000)),
    ("S4  phase B importing, site exporting (solar 20 000, house B 7 500)",
     "tank on B REFUSED (its phase buys); station on C runs",
     lambda: base(20000, hb=7500)),
    ("S5  unmanaged plug drawing 2 kW (solar 11 500)",
     "plug permit 0 and its draw is household, so managed_W excludes it; "
     "site export 8.6 kW is under threshold so the tank stays off",
     lambda: base(11500, plug_relay=True, plug_managed=False)),
    ("S6  release: drop from 14 000 to 10 000",
     "both loads let go (grace period is 0 here)",
     lambda: (base(14000), time.sleep(40), num("input_number.sim_solar", 10000))),
]

which = os.environ.get("CASES", "")
for i, (name, expect, setup) in enumerate(CASES):
    if which and str(i) not in which.split(","):
        continue
    setup()
    show(name, expect, settle())
