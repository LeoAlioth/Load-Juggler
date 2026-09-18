"""Drive the Docker rig through its REST API and report what settles.

Companion to scenarios.py. Needs a long-lived access token - see TOKEN
below. Everything here talks to localhost only.
"""
import json, os, subprocess, sys, time

_TOKEN_FILE = os.path.join(os.path.dirname(__file__), "ha_token")
TOKEN = (os.environ.get("HA_TOKEN")
         or (open(_TOKEN_FILE).read().strip() if os.path.exists(_TOKEN_FILE) else ""))
if not TOKEN:
    raise SystemExit(
        "No token. Create a long-lived access token in Home Assistant "
        "(profile -> Security -> Create token) and either put it in "
        f"{_TOKEN_FILE} (gitignored) or export HA_TOKEN.")
BASE = "http://localhost:8124/api"

def _curl(path, payload=None):
    cmd = ["curl", "-s", "-H", f"Authorization: Bearer {TOKEN}",
           "-H", "Content-Type: application/json", BASE + path]
    if payload is not None:
        cmd += ["-X", "POST", "-d", json.dumps(payload)]
    out = subprocess.run(cmd, capture_output=True, text=True).stdout
    return json.loads(out) if out.strip() else None

def get(entity):
    s = _curl(f"/states/{entity}")
    return s["state"] if s else None

def num(entity, value):
    _curl("/services/input_number/set_value",
          {"entity_id": entity, "value": value})

def select(entity, option):
    _curl("/services/input_select/select_option",
          {"entity_id": entity, "option": option})

def toggle(entity, on):
    domain = entity.split(".")[0]
    _curl(f"/services/{domain}/turn_{'on' if on else 'off'}", {"entity_id": entity})

WATCH = {
    "grid_W":    "sensor.site_load_management_current_grid_power",
    "managed_W": "sensor.site_load_management_current_managed_power",
    "tank":      "sensor.hot_water_tank_tank_status",
    "tank_A":    "sensor.hot_water_tank_available_current",
    "stn":       "sensor.power_station_status",
    "stn_A":     "sensor.power_station_available_current",
    "plug":      "sensor.smart_load_status",
    "plug_A":    "sensor.smart_load_available_current",
}

# Stability is judged on the DECISIONS, not the power readings: the engine
# smooths its grid figures, so grid_W and managed_W drift by a few watts for
# ~40 s after any change and would flag every scenario as unstable.
DECISIONS = ("plug", "plug_A", "tank", "tank_A", "stn", "stn_A")

def settle(warmup=160, window=25, samples=5):
    """Let the engine converge, THEN sample, and report what held steady.

    160 s of warmup. A binary load settles in well under a minute, but a
    MODULATING one rings and damps: measured from a cold start, the station's
    permit spread decayed 552 -> 414 -> 345 -> 207 -> 0 W over 150 s. Sampling
    at 95 s catches that ring and reports it as sustained hunting, which is
    exactly the mistake made on 2026-09-08.

    A tank start is also a chain: the engine decides, the
    control layer writes a setpoint on the device's own 5 s cycle, the
    thermostat reacts, the element closes, the power monitor lags 5-10 s and
    the grid CT another 5-10 s, and the engine's EMA then takes ~40 s to work
    the step through. A shorter warmup samples a transient and reports it as
    the settled answer.
    """
    time.sleep(warmup)
    seen = {k: [] for k in WATCH}
    for _ in range(samples):
        for k, e in WATCH.items():
            seen[k].append(get(e))
        time.sleep(window / samples)
    out = {}
    for k, v in seen.items():
        spread = None
        try:
            nums = [float(x) for x in v]
            spread = max(nums) - min(nums)
        except (TypeError, ValueError):
            pass
        out[k] = (v[-1], len(set(v)) == 1, spread)
    return out

def show(name, expectation, result):
    # Statuses must be exactly constant. Permits are judged with a tolerance of
    # one register step (100 W = 0.43 A): that is the smallest change the
    # device can be told about, so sub-step wobble is invisible to it and
    # calling it instability is a measurement artefact, not a finding.
    stable = True
    for k in DECISIONS:
        val, unchanged, spread = result[k]
        if unchanged:
            continue
        if k.endswith("_A") and spread is not None and spread <= 0.5:
            continue
        stable = False
    print(f"\n{'='*70}\n{name}\n  expect: {expectation}")
    print(f"  {'STABLE' if stable else 'UNSTABLE - values moved during the window'}")
    for k in ("grid_W", "managed_W", "plug", "plug_A",
              "tank", "tank_A", "stn", "stn_A"):
        val, ok, spread = result[k]
        note = "" if ok else (f"  <-- moved {spread:.2f}" if spread is not None
                              else "  <-- changed")
        print(f"    {k:10s} {str(val):>12s}{note}")
