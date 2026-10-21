"""Replay bench: Kozolec's two early-start tanks on RECORDED days.

Machine-authored bench - not yet human-reviewed. Not collected by the normal
run (no test_ prefix); run it on purpose, one day per test:

    BENCH_DATA=<dir of YYYY-MM-DD.json> BENCH_OUT=<dir> BENCH_LABEL=new \\
        .venv/bin/python -m pytest dev/tests/bench_tank_days.py -q -s
    .venv/bin/python dev/tests/bench_tank_days.py <BENCH_OUT> old new   # the table

Each day file is two Home Assistant history responses (``sensors``: minimal,
``climates``: with attributes) for one local day, 22:00Z to 22:00Z, of the
entities in ENT below (fetched read-only from the site; not kept in the repo).

The REAL hub cycle (``sensor.async_run_hub_cycle``) runs every CYCLE_S over the
day against a world built from the recording, as test_tank_forecast_cycle.py
does on a synthetic morning:

- solar: recorded PV power; house: recorded battery (+ = discharge) + PV - the
  two tanks' recorded power sensors (every other load stays in the house);
- the tanks are simulated: an element draws while its setpoint is above its
  water, and the water follows the RECORDED temperature plus the difference
  between simulated and recorded element energy over the thermal mass (so the
  recorded draw-offs and losses are kept, the recorded heating is replaced);
- battery = house + simulated tanks - PV, SOC integrated from the recorded
  start over the 62.05 kWh pack, capped at 100 %;
- the planning sensors (planned kWh, production end, next full charge), the
  tanks' Room to absorb / Absorption rate and the hub's SOC min/target are
  replayed as recorded (they reflect the old behaviour, not recomputed).
"""

import bisect
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ENT = {
    "pv": "sensor.kozolec_total_pv_power",
    "batt": "sensor.gx_device_dc_battery_power_inverted",
    "soc": "sensor.kozolec_battery_soc_corrected",
    "planned": "sensor.kozolec_pv_kozolec_insights_daily_excess",
    "horizon": "sensor.kozolec_pv_kozolec_insights_production_ends",
    "full": "sensor.kozolec_pv_kozolec_insights_next_full_charge",
    "soc_min": "number.kozolec_load_management_home_battery_soc_min",
    "soc_target": "number.kozolec_load_management_home_battery_soc_target",
}
PACK_KWH = 62.05
CYCLE_S = float(os.environ.get("BENCH_CYCLE_S", "5"))   # Kozolec's site_update_frequency
DATA = os.environ.get("BENCH_DATA")
OUT = os.environ.get("BENCH_OUT")
LABEL = os.environ.get("BENCH_LABEL", "run")

# name, climate, power sensor, room, rate, element W (real), kWh/K, away/normal/boost,
# priority, device type, load update frequency - the site's config and numbers
TANKS = [
    dict(name="boiler", climate="climate.kozolec_boiler", power="sensor.boiler_power",
         room="sensor.kotlovnica_boiler_room_to_absorb_today",
         rate="sensor.kotlovnica_boiler_absorption_rate",
         element_w=1886.0, kwh_per_k=0.322, temps=(7.0, 42.0, 75.0), priority=4,
         device="hot_water_tank", update=5.0),
    dict(name="heater", climate="climate.bee_hive_room_bee_room_heater_bee_room_thermostat",
         power="sensor.bee_room_heater_power",
         room="sensor.bee_hive_room_bee_room_heater_room_to_absorb_today",
         rate="sensor.bee_hive_room_bee_room_heater_absorption_rate",
         element_w=1026.0, kwh_per_k=0.656, temps=(7.0, 18.0, 24.0), priority=1,
         device="room_thermostat", update=15.0),
]


def _ts(s):
    return datetime.fromisoformat(s).timestamp()


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


class Series:
    """A recorded state history, held step-wise."""

    def __init__(self, rows):
        rows = sorted(rows)
        self.t = [r[0] for r in rows]
        self.v = [r[1] for r in rows]

    def at(self, t, default=None):
        i = bisect.bisect_right(self.t, t) - 1
        return self.v[i] if i >= 0 else (self.v[0] if self.v else default)

    def last_num(self, t):
        """The latest numeric value at or before ``t`` (the first one if none)."""
        i = bisect.bisect_right(self.t, t) - 1
        for j in range(i, -1, -1):
            if _num(self.v[j]) is not None:
                return _num(self.v[j])
        return next((_num(x) for x in self.v if _num(x) is not None), None)

    def mean(self, a, b):
        """Time-weighted mean of the numeric values over [a, b), holding the last
        number across unavailable spells."""
        total, t, cur = 0.0, a, self.last_num(a) or 0.0
        i = bisect.bisect_right(self.t, a)
        while i < len(self.t) and self.t[i] < b:
            total += cur * (self.t[i] - t)
            t = self.t[i]
            cur = _num(self.v[i]) if _num(self.v[i]) is not None else cur
            i += 1
        return (total + cur * (b - t)) / (b - a)


def load_day(path):
    raw = json.loads(Path(path).read_text())
    s = {}
    for hist in raw["sensors"]:
        if hist:
            s[hist[0]["entity_id"]] = Series(
                [(_ts(r["last_changed"]), r["state"]) for r in hist])
    temp, setp = {}, {}
    for hist in raw["climates"]:
        if hist:
            e = hist[0]["entity_id"]
            temp[e] = Series([(_ts(r["last_updated"]), r["attributes"].get("current_temperature"))
                              for r in hist])
            setp[e] = Series([(_ts(r["last_updated"]), r["attributes"].get("temperature"))
                              for r in hist])
    return s, temp, setp


def day_bounds(day):
    start = datetime.fromisoformat(day).replace(tzinfo=timezone.utc) - timedelta(hours=2)
    return start.timestamp(), (start + timedelta(days=1)).timestamp()


def start_soc(s, t0, t1):
    """The recorded SOC at the day's start (0 % readings are glitches); with no SOC
    sensor that day, integrated back from the next day's first reading."""
    soc = s.get(ENT["soc"])
    if soc:
        vals = [_num(v) for v in soc.v if _num(v)]
        first = soc.last_num(t0)
        return first if first else vals[0]
    nxt = Path(DATA) / (datetime.fromtimestamp(t1, timezone.utc) + timedelta(hours=2)).date().isoformat()
    ns, _, _ = load_day(f"{nxt}.json")
    end = ns[ENT["soc"]].last_num(t1)
    drained_kwh = s[ENT["batt"]].mean(t0, t1) * (t1 - t0) / 3.6e6
    return min(100.0, end + drained_kwh / PACK_KWH * 100)


def recorded_counts(setp, t0, t1):
    """Boost starts and stops of each tank in the recording (setpoint raised to
    its boost temperature or above, and dropped back below it)."""
    out = {}
    for tk in TANKS:
        ser = setp.get(tk["climate"])
        ups = downs = 0
        prev = ser.at(t0) if ser else None
        for t, v in (zip(ser.t, ser.v) if ser else ()):
            if t0 <= t < t1 and v is not None and prev is not None and v != prev:
                boost = tk["temps"][2]
                ups += v >= boost > prev
                downs += prev >= boost > v
            if v is not None:
                prev = v
        out[tk["name"]] = (ups, downs)
    return out


# ---------------------------------------------------------------- the bench --

import pytest  # noqa: E402

DAYS = sorted(p.stem for p in Path(DATA).glob("2026-*.json")) if DATA else []


@pytest.mark.skipif(not DATA, reason="BENCH_DATA not set")
@pytest.mark.parametrize("day", DAYS)
async def test_replay_day(hass, day):
    from homeassistant.util import dt as dt_util
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.dynamic_ocpp_evse import sensor as sensor_platform
    from custom_components.dynamic_ocpp_evse.const import (
        DOMAIN, ENTRY_TYPE, ENTRY_TYPE_HUB, ENTRY_TYPE_LOAD, TANK_MODE_NORMAL,
    )

    from .closed_loop import clocked
    from .conftest import INPUT_GRID, input_options, with_inverter

    s, rec_temp, rec_setp = load_day(Path(DATA) / f"{day}.json")
    t0, t1 = day_bounds(day)
    # Kozolec's hub, inverter and two tanks as its diagnostics show them (2026-10-21);
    # the next-full-charge entity only where the day recorded one (from 18 Oct).
    hub_opts = {
        "phase_voltage": 230.0, "main_breaker_rating": 40.0,
        "site_update_frequency": CYCLE_S, "base_consumption": 250.0,
        "excess_trigger_margin": 250.0, "excess_hysteresis": 250.0,
        "battery_soc_hysteresis": 2.0, "battery_soc_freeze_floor": 5.0,
        "forecast_excess_enabled": True, "forecast_excess_soc_source": "minimum",
        "forecast_excess_soc": 80.0, "forecast_soc_floor": 30.0,
        "grid_export_limit": 0.0,
        "pacing_energy_entity_id": "sensor.planned",
        "pacing_horizon_entity_id": "sensor.horizon",
        # inverter
        **input_options(INPUT_GRID),
        "solar_production_entity_id": "sensor.pv",
        "battery_soc_entity_id": "sensor.soc",
        "battery_power_entity_id": "sensor.battery",
        "inverter_output_phase_a_entity_id": "sensor.consumption_l1",
        "inverter_max_power": 9000.0, "inverter_max_power_per_phase": 9000.0,
        "battery_max_charge_power": 9000.0, "battery_max_discharge_power": 9000.0,
        "battery_soc_full": 97.0,
    }
    if ENT["full"] in s:
        hub_opts["forecast_excess_full_entity_id"] = "sensor.full"
    hub = with_inverter(hass, MockConfigEntry(
        domain=DOMAIN, version=2, minor_version=2, title="Hub",
        data={"name": "Hub", "entity_id": "hub", ENTRY_TYPE: ENTRY_TYPE_HUB},
        options=hub_opts,
    ))
    entries = []
    for tk in TANKS:
        away, normal, boost = tk["temps"]
        entries.append(MockConfigEntry(
            domain=DOMAIN, version=2, minor_version=2, title=tk["name"],
            data={"entity_id": tk["name"], "name": tk["name"], ENTRY_TYPE: ENTRY_TYPE_LOAD,
                  "device_type": tk["device"], "climate_entity_id": tk["climate"],
                  "hub_entry_id": hub.entry_id},
            options={
                "connected_to_phase": "A", "load_priority": tk["priority"],
                "heating_element_power": 2000.0, "binary_min_off_time": 5.0,
                "update_frequency": tk["update"], "solar_grace_period": 5.0,
                "tank_away_temperature": away, "tank_normal_temperature": normal,
                "tank_boost_temperature": boost, "tank_prioritize_below_normal": True,
                "tank_power_entity_id": f"sensor.{tk['name']}_power",
                **({"early_room_entity_id": f"sensor.{tk['name']}_room"} if tk["room"] in s else {}),
                **({"early_rate_entity_id": f"sensor.{tk['name']}_rate"} if tk["rate"] in s else {}),
            },
        ))
    hass.data[DOMAIN] = {
        "hubs": {hub.entry_id: {
            "entry": hub, "loads": [e.entry_id for e in entries],
            "distribution_mode": "Priority", "allow_grid_charging": True,
            "power_buffer": 0, "max_import_power": 0,
            "battery_soc_min": 10, "battery_soc_target": 55,
        }},
        "loads": {e.entry_id: {
            "entry": e, "hub_entry_id": hub.entry_id, "dynamic_control": True,
            "operating_mode": TANK_MODE_NORMAL.key,
        } for e in entries},
    }
    hub_rt = hass.data[DOMAIN]["hubs"][hub.entry_id]
    procs = hass.data[DOMAIN].setdefault("load_processors", {}).setdefault(hub.entry_id, {})
    for tk, e in zip(TANKS, entries):
        procs[e.entry_id] = sensor_platform.LoadJugglerDeviceSensor(hass, e, hub, tk["name"], tk["name"])

    setpoint = {tk["climate"]: tk["temps"][1] for tk in TANKS}

    async def accept(domain, service, data=None, *args, **kwargs):
        if domain == "climate" and service == "set_temperature":
            ent = data.get("entity_id")
            ent = ent[0] if isinstance(ent, list) else ent
            if ent in setpoint:
                setpoint[ent] = float(data["temperature"])

    soc = soc0 = start_soc(s, t0, t1)
    W = {"device_class": "power", "unit_of_measurement": "W"}
    st = {tk["name"]: dict(ups=0, downs=0, on_s=0.0, kwh=0.0, batt_kwh=0.0, sun_kwh=0.0,
                           boost_s=0.0, rec_kwh=0.0, end_c=None) for tk in TANKS}
    sim_e = {tk["name"]: 0.0 for tk in TANKS}   # kWh since t0, simulated
    rec_e = {tk["name"]: 0.0 for tk in TANKS}   # kWh since t0, recorded
    min_soc, group_s, any_boost_s = soc, 0.0, 0.0
    events = []
    put = hass.states.async_set

    def publish_raw(entity, key, t, unit_attrs, ts=False):
        raw = s[ENT[key]].at(t) if ENT[key] in s else None
        if key == "full" and raw is not None and t < s[ENT[key]].t[0]:
            # before the sensor existed the hub had none: the pacing horizon stood in
            raw = s[ENT["horizon"]].at(t)
        if raw is None:
            return
        if raw in ("unavailable", "unknown"):
            put(entity, raw)
        elif ts:
            put(entity, (dt_util.utcnow() + timedelta(seconds=_ts(raw) - t)).isoformat(),
                {"device_class": "timestamp"})
        else:
            put(entity, raw, unit_attrs)

    with clocked(accept) as clock:
        t = t0
        while t < t1:
            b = t + CYCLE_S
            pv = s[ENT["pv"]].mean(t, b)
            rec_tanks = {tk["name"]: s[tk["power"]].mean(t, b) if tk["power"] in s else 0.0
                         for tk in TANKS}
            house = max(0.0, s[ENT["batt"]].mean(t, b) + pv - sum(rec_tanks.values()))
            # the water as recorded, its recorded heating swapped for the simulated
            water = {}
            for tk in TANKS:
                rt = rec_temp[tk["climate"]].last_num(t) if tk["climate"] in rec_temp else None
                water[tk["name"]] = (rt if rt is not None else tk["temps"][1]) + (
                    sim_e[tk["name"]] - rec_e[tk["name"]]) / tk["kwh_per_k"]
            draw = {tk["name"]: tk["element_w"] if setpoint[tk["climate"]] > water[tk["name"]] else 0.0
                    for tk in TANKS}
            tanks_w = sum(draw.values())
            if soc >= 100.0 and pv > house + tanks_w:
                pv = house + tanks_w          # full pack: the MPPT holds the sun back
            batt = house + tanks_w - pv

            put("sensor.pv", str(round(pv, 1)), W)
            put("sensor.battery", str(round(batt, 1)), W)
            put("sensor.soc", str(round(soc, 2)), {"device_class": "battery", "unit_of_measurement": "%"})
            put("sensor.consumption_l1", str(round(house + tanks_w, 1)), W)
            publish_raw("sensor.planned", "planned", t, {"device_class": "energy", "unit_of_measurement": "kWh"})
            publish_raw("sensor.horizon", "horizon", t, None, ts=True)
            publish_raw("sensor.full", "full", t, None, ts=True)
            for key in ("soc_min", "soc_target"):
                v = s[ENT[key]].last_num(t) if ENT[key] in s else None
                if v is not None:
                    hub_rt["battery_" + key] = v
            for tk in TANKS:
                n = tk["name"]
                for k, unit in (("room", "kWh"), ("rate", "kW")):
                    if tk[k] in s:
                        raw = s[tk[k]].at(t)
                        put(f"sensor.{n}_{k}", raw, {} if raw in ("unavailable", "unknown") else {
                            "device_class": "energy" if k == "room" else "power",
                            "unit_of_measurement": unit})
                put(f"sensor.{n}_power", str(draw[n]), W)
                put(tk["climate"], "heat", {
                    "temperature": setpoint[tk["climate"]],
                    "current_temperature": round(water[n], 2),
                    "hvac_action": "heating" if draw[n] else "idle",
                    "min_temp": 5, "max_temp": 80,
                })

            before = dict(setpoint)
            await sensor_platform.async_run_hub_cycle(hass, hub)

            # the cycle's energy at the draw in force over it
            sun_for_tanks = max(0.0, min(tanks_w, pv - house))
            for tk in TANKS:
                n, w = tk["name"], draw[tk["name"]]
                kwh = w * CYCLE_S / 3.6e6
                share = w / tanks_w if tanks_w else 0.0
                st[n]["kwh"] += kwh
                st[n]["sun_kwh"] += sun_for_tanks * share * CYCLE_S / 3.6e6
                st[n]["batt_kwh"] += kwh - sun_for_tanks * share * CYCLE_S / 3.6e6
                st[n]["on_s"] += CYCLE_S if w else 0.0
                st[n]["boost_s"] += CYCLE_S if before[tk["climate"]] >= tk["temps"][2] else 0.0
                sim_e[n] += kwh
                rec_e[n] += rec_tanks[n] * CYCLE_S / 3.6e6
                new = setpoint[tk["climate"]]
                boost = tk["temps"][2]
                st[n]["ups"] += new >= boost > before[tk["climate"]]
                st[n]["downs"] += before[tk["climate"]] >= boost > new
                if new != before[tk["climate"]]:
                    events.append(f"{datetime.fromtimestamp(t, timezone.utc):%H:%M}Z {n} "
                                  f"{before[tk['climate']]:g}->{new:g} water {water[n]:.1f}"
                                  f" soc {soc:.1f} pv {pv:.0f} house {house:.0f}"
                                  f" planned {s[ENT['planned']].at(t) if ENT['planned'] in s else '-'}"
                                  f" group {hub_rt.get('_early_group_on')}")
            if any(before[tk["climate"]] >= tk["temps"][2] for tk in TANKS):
                any_boost_s += CYCLE_S
            group_s += CYCLE_S if hub_rt.get("_early_group_on") else 0.0
            soc = min(100.0, soc - batt * CYCLE_S / 3.6e6 / PACK_KWH * 100)
            min_soc = min(min_soc, soc)
            clock.now += CYCLE_S
            t = b

    rec = recorded_counts(rec_setp, t0, t1)
    for tk in TANKS:
        n = tk["name"]
        st[n]["end_c"] = round(water[n], 1)
        st[n]["rec_kwh"] = rec_e[n]
        st[n]["rec_ups"], st[n]["rec_downs"] = rec[n]
    result = dict(day=day, label=LABEL, code=sensor_platform.__file__, cycle_s=CYCLE_S, start_soc=soc0,
                  min_soc=min_soc, end_soc=soc, group_h=group_s / 3600, boost_h=any_boost_s / 3600,
                  has_planned=ENT["planned"] in s, has_full=ENT["full"] in s,
                  has_room=TANKS[0]["room"] in s, tanks=st, events=events)
    print(json.dumps(result))
    if OUT:
        Path(OUT, f"{LABEL}_{day}.json").write_text(json.dumps(result, indent=1))


# ------------------------------------------------------------------ table --

def table(out_dir, labels):
    days = sorted({p.stem.split("_", 1)[1] for p in Path(out_dir).glob("*_2026-*.json")})
    rows = []
    for day in days:
        runs = {lb: json.loads(Path(out_dir, f"{lb}_{day}.json").read_text())
                for lb in labels if Path(out_dir, f"{lb}_{day}.json").exists()}
        if not runs:
            continue
        any_run = next(iter(runs.values()))
        print(f"\n{day}  planned:{'y' if any_run['has_planned'] else 'n'} full:{'y' if any_run['has_full'] else 'n'}"
              f" room/rate:{'y' if any_run['has_room'] else 'n'}  start SOC {any_run['start_soc']:.1f}")
        for tk in TANKS:
            n = tk["name"]
            r0 = any_run["tanks"][n]
            print(f"  {n:6} recorded: starts {r0['rec_ups']:3} stops {r0['rec_downs']:3}  kWh {r0['rec_kwh']:5.2f}")
            for lb, r in runs.items():
                t = r["tanks"][n]
                print(f"  {n:6} {lb:8}: starts {t['ups']:3} stops {t['downs']:3}  on {t['on_s']/60:6.0f} min"
                      f"  boost {t['boost_s']/60:5.0f} min  kWh {t['kwh']:5.2f} (batt {t['batt_kwh']:5.2f}"
                      f" sun {t['sun_kwh']:5.2f})  end {t['end_c']:5.1f} C")
        for lb, r in runs.items():
            print(f"  site   {lb:8}: min SOC {r['min_soc']:5.1f}  end SOC {r['end_soc']:5.1f}"
                  f"  group due {r['group_h']:4.1f} h  any boost {r['boost_h']:4.1f} h")
        rows.append((day, runs))
    return rows


if __name__ == "__main__":
    table(sys.argv[1], sys.argv[2:] or ["old", "new"])
