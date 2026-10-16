# lj-7 - Tank target flipping (Andrej 42 <-> 40, Home 80 <-> 10)

Repo `/Users/anzek/Projects/Dynamic_OCPP_EVSE`, branch `dev`. Personal project - never log to work-context.
Planned 2026-10-12 from read-only history/diagnostics probes; nothing was changed. Runs after lj-1 in the
batch (lj-1 touches the command gate in `entities/load.py`; re-check the line numbers below after it).

## What the history shows

**Andrej, `water_heater.tc`** (MELCloud Ecodan; LJ "Hot Water Tank", Normal, away/normal/boost sliders
40/42/45, element slider 900 W on ABC = 1.304 A/phase, min off 5 min, grace 5 min, update 15 s, priority 3;
hub Sequential - Optimized, 20 A main breaker; the heater's own `min_temp` 40 - a capability attribute,
so not in recorder history - read live):

- Target changes: 10-10 38, 10-11 72, 10-12 28. Nearly all are 42 <-> 40.
- `setpoint_source` stays `normal` the whole time. The mode decision does not flip. `heating_permitted`
  flips: the status reads *Waiting for Power* / *Idle* in step with every 40 / 42.
- **40 is the heater's `min_temp`**, not away. That is what `_command_water_heater` writes for a denied
  tank that is not "at its floor" (`control/hot_water_tank.py:547`). The tank is at 38.5-41.5 C, so it is
  under the 42 floor and the floor rule (`hot_water_tank.py:299-319`, `current_temperature >= floor`) never
  holds it at 42.
- The ~5 min cadence comes from `binary_min_off_time` = 5 min (`entities/load.py:65-89`, applied at
  `:649-680`). After each shed the tank is held off for 5 min (40). The first positive cycle after that
  grants the permit (42), and the next flicker sheds it again. Grace is not involved: a `normal` label does not
  ride it (`rides_grace`, `load.py:133-147`). `judge_kept_target` is not involved either: it is reset
  whenever the tank is denied (`hot_water_tank.py:385-386`), and no permit lasted 300 s with a lower
  readback.

Two different causes take the permit away:

1. **Idle tank, real contention** (10-10 10:50-12:56 with Nova charging, 10-11 20:07-00:11 with both
   cars, 10-12 14:00-14:33 with Stara). In Optimized the cars take phase A up to the 20 A breaker. The idle
   tank's could-start permit (the inactive branch, `calculations/target_calculator.py:337-354`) flickers
   0 / 1.3 A every 2-30 s with the car readings. Example, 10-11 20:05-20:35 UTC: 65 permit edges, and the
   heater went 40 @20:07:35, 42 @20:12:39, 40 @20:13:59, 42 @20:19:03, 40 @20:20:39, 42 @20:30:47, 40
   @20:32:07. The Ecodan sat `status: idle` the whole time. It restarts hot water only ~4.5 K under its
   target (seen at 37.5-38.5 C under 42), so at 39-41 C it drew nothing at either target. Every flip was a
   MELCloud cloud write for nothing.
2. **Heating tank refused by rounding - a real bug.** Each time the Ecodan starts (`status: heat_water`,
   10-10 09:36, 10-11 04:26, 10-12 06:06 and 10:35), the permit drops to 0 within 2 s while the site has
   ~19 A/phase free. The tank stays *Waiting for Power* for the whole run, `allocated_current` stays 0 and
   the target is set to 40. So it heats to 40-40.5 C instead of 42. Cause: `_distribute_per_phase_optimized`
   rounds first and compares after (`target_calculator.py:2571-2572`):
   `wanted = round(1.304, 1) = 1.3 < load.min_current 1.304` -> allocated 0. Strict does the same
   (`:2480-2482`). The rounding came in c32bbc7 (10-04) for the two-charger flicker. An idle tank escapes
   this because the inactive branch compares before it rounds (`:351-352`). Confirmed on the real calculator
   (scratch probe): 900 W / 3 ph, Charging -> Shared 1.3, Priority 1.3, **Optimized 0, Strict 0**; Available
   -> 1.3 in all four. Any binary load whose rating rounds down to a tenth is hit (1500 W/1 ph = 6.52 A,
   900 W/3 ph...) in the two Sequential modes.
3. Smaller: **after every HA restart** the first cycle's permit is 0, so min-off holds the tank at 40 for
   5 min (10-12 10:20:33-10:25:36, 14:38:42-14:43:46). Cause 1's fix covers this: the device is idle at 42.

**Home, `climate.workshop_boiler`** (generic_thermostat, Freeze Protection, away 10 / boost 80, 2.1 kW on
B, Priority mode, export limit 13.5 kW, trigger margin 500 W): the 10-11 80 <-> 10 flips are boosts on an
Excess verdict at its edge. Export sat around 13 kW, so each boost lasted ~1 min and then waited 10-20 min
at away. Since 37fd304 (installed 10-12 ~10:30): 80 @10:55, 12:05, 12:34, 13:15, 13:41, each held 5-23 min
(`grace_active` true while permit 0), ~30 min apart, plus one restart drop at 13:18. **Damped - no change
in this plan**, only watched (below). Neither of Andrej's causes applies: Priority mode, and the tank is
far above its 10 C floor.

## Root cause (one line each)

- Andrej's flip: a denied tank under its floor temperature is set to its lowest target even when its
  device is idle **at the floor target** (`control/hot_water_tank.py:299-319`). With `min_off_hold`
  (5 min) and a permit that flickers under charger contention, that is one cloud write every ~5 min.
- Andrej's heat runs cut short: Optimized/Strict judge a rounded allocation against an unrounded minimum
  (`calculations/target_calculator.py:2480-2482, 2571-2572`), so an active 900 W/3 ph tank gets 0.

## Fix (two rules, both in shared code, no tank special case)

### A. Judge the allocation on the grid it is given (`target_calculator.py`)

In both Sequential modes, compare against the minimum rounded to the same 0.1 A:

```python
# Strict, :2482
if allocation < round(load.min_current, 1):
# Optimized, :2572
if wanted < round(load.min_current, 1):
```

Add one sentence to the Optimized comment above it: the minimum is rounded too, or a binary load whose
rating rounds down (900 W / 3 ph = 1.304 -> 1.3 A) is never allocated while it runs. Nothing downstream
compares `allocated_current` with `min_current` for binary loads (`_set_available_current_for_loads`
checks `> 0`; the tank/plug commands check `> 0`). An EVSE is unchanged (6 A minimums are on the grid).

### B. A denied tank idle at its floor target stays there (`control/hot_water_tank.py:299-319`)

The temperature test stands in for "won't heat at the floor target". The device's own word says it
directly. Add one alternative to `at_floor`: **the device reads idle (`tank_hvac_action == "idle"`) and is
already set to the floor target (within 0.05)**. Keep the existing temperature path and its "heating at
the floor target" exception unchanged. The new branch only stops a lowering - it never raises a target -
so a denied tank lowered to its lowest after a heating run stays there until power returns (no
raise/heat/lower loop at the command interval on a heater without hysteresis).

Sketch (keep the existing try/except semantics: no temperature / no target -> not at floor):

```python
hvac = load_rt.get("tank_hvac_action")
try:
    floor_t = settings[floor]
    device_t = climate_state.attributes.get("temperature")
    idle_there = hvac == "idle" and device_t is not None and abs(float(device_t) - floor_t) < 0.05
    at_floor = idle_there or (
        float(climate_state.attributes["current_temperature"]) >= floor_t
        and not (hvac == "heating" and device_t is not None and float(device_t) <= floor_t + 0.05)
    )
except (AttributeError, KeyError, TypeError, ValueError):
    pass
```

Extend the comment block above it with one or two sentences and the Andrej evidence (10-11, 72 writes a day,
Ecodan idle at 39-41 C under 42). Known ceiling, the same one the TODO already names for the at-floor case:
a thermostat with a tight tolerance (generic_thermostat 0.3 K) denied while idle at its floor can start one
heating burst before the next cycle reads `heating` and switches it off. Once off, it reads `off`, not idle,
so this happens once per denial at most.

Skipped: the status sensor still flickers *Waiting for Power* / *Idle* under contention. That is
cosmetic, and the permit is honest. Also skipped: `target_setpoint` shows 42 while `min_temp` was written. Add
either only if Anze asks.

## Tests (`dev/tests/test_hot_water_tank.py`, plus one in `test_distribution_table.py`)

1. **Replay Andrej 10-11 20:05-20:35 UTC** (new, `test_hot_water_tank.py` next to the leaving-boost
   replays). The existing `_replay` ticks 15 s with no permit trace, so add a sibling `_replay_permits`
   (or a `permits=` argument): 2 s site ticks running `min_off_hold` on every tick, as `load.py` does, and a
   command every 15 s. `water_heater.tc`, Normal, 40/42/45, `min_temp` 40, `status="idle"`, temp 40.0,
   device target 42, no power sensor. Permit trace = the recorded edges, (seconds after 20:05:00, A):

   ```
   (1,1.3),(147,0),(149,1.3),(175,0),(201,1.3),(213,0),(231,1.3),(237,0),(247,1.3),(273,0),(291,1.3),
   (305,0),(307,1.3),(333,0),(343,1.3),(365,0),(369,1.3),(393,0),(401,1.3),(523,0),(529,1.3),(551,0),
   (561,1.3),(579,0),(591,1.3),(609,0),(621,1.3),(933,0),(937,1.3),(963,0),(983,1.3),(987,0),(993,1.3),
   (995,0),(999,1.3),(1025,0),(1027,1.3),(1055,0),(1057,1.3),(1085,0),(1089,1.3),(1115,0),(1123,1.3),
   (1145,0),(1153,1.3),(1175,0),(1183,1.3),(1205,0),(1223,1.3),(1239,0),(1245,1.3),(1265,0),(1283,1.3),
   (1295,0),(1313,1.3),(1329,0),(1345,1.3),(1355,0),(1373,1.3),(1385,0),(1403,1.3),(1415,0),(1433,1.3),
   (1619,0),(1621,1.3)
   ```

   Assert `shown == [42.0]` and no `set_temperature` call. Check first, on the unfixed code, that the
   same replay gives `[42, 40, 42, 40, ...]` - on site: 40 @t155, 42 @t459, 40 @t539, 42 @t843 (the replay's
   exact seconds follow its 15 s command grid; assert the sequence, not the times) - so the test is known to
   catch it.
   Second leg in the same test: a heating start while denied (status `heat_water` at 37.5 C, permit 0)
   writes 40 once. Back to `idle` at 40.5 C with target 40 it is **not** raised while still denied
   (`shown == [42.0, 40.0]`), and the permit returning gives 42 (`[42.0, 40.0, 42.0]`).
2. **A running binary load is allocated in every mode** (`test_distribution_table.py`, or a small new
   test beside it): a tank LoadContext, `min_current = max_current = rated = 900/690`, 3 ph,
   `connector_status="Charging"`, drawing it, 20 A breaker, 0.6 A house. Parametrize over the four `MODES`
   and assert `allocated_current == 1.3` and `available_current == 1.3`. Today it fails for Optimized and
   Strict. Add a 1500 W / 1 ph plug row (6.52 A -> 6.5).
3. Existing tests that must stay green unchanged: the leaving-boost replays,
   `test_a_denied_tank_the_device_says_heats_is_not_held_at_its_floor`,
   `test_a_tank_denied_power_below_its_floor_is_switched_off` (no hvac_action there, so the temperature
   path still decides), `test_a_tank_denied_power_with_no_temperature_is_switched_off`,
   `test_water_heater_tank._command` (builder not run, so hvac is None), and `test_distribution_table`
   (EVSE minimums are on the grid).

Run: `.venv/bin/python -m pytest dev/tests -q -n 8` - expect 1671 + new, all pass.

## Docs

**`RELEASE_NOTES.md`**, `## 2.1.6` -> `### Bug Fixes`, near the other tank entries:

- **A tank or smart load whose rating is not a whole tenth of an amp is no longer refused power once it
  runs, in the Sequential modes**: Sequential - Optimized and Strict rounded a load's allocation to 0.1 A
  and then compared it with its unrounded minimum. A 900 W tank on three phases (1.304 A) came out at 1.3 A,
  under its own minimum, so it got nothing - and only while it was heating. Andrej's heat pump was denied
  power each time it started heating water and was set to its lowest target, so it heated to 40 C instead of
  42. The minimum is now rounded the same way.
- **A heat pump's tank denied power no longer flips between its normal and lowest temperature**: a tank
  denied power below its normal temperature was set to its lowest target even when the device said it was
  idle at the normal one. A heat pump idles a few degrees under its target, so it drew nothing there either
  way. With two cars charging on a 20 A supply, the tank's permit came and went every few seconds, and after
  each *Minimum off time* the target went 42 -> 40 -> 42 C - 72 cloud writes in a day (Andrej's MELCloud
  heat pump, 11 Oct). A denied tank whose device is idle at its floor target now stays there. One that
  starts heating is still set to its lowest target, and is not raised again until power returns.

**`dev/TODO.md`**: extend the existing **[BUG] Leaving boost: verify on Andrej's tank and Home's workshop
boiler** item (line 10). Rename it to "Tank target: verify on Andrej's tank and Home's workshop boiler"
and add: `water_heater.tc` holds 42 through charger contention (no 42 <-> 40 every 5 min); each
`heat_water` run shows *Heating* with `heating_permitted: true` and reaches ~42 C; and, until Andrej's
install, Home's boiler boosts hold >= 5 min (37fd304). Mention the generic_thermostat one-burst ceiling
next to the existing ceiling sentence. No new backlog item.

## Steps

1. `git status` clean on dev, pull. Check plan usage (pause at 90 %).
2. Write tests 1-2 first and see them fail on the current code (replay `[42, 40, 42, ...]`, Optimized/Strict 0).
3. Fix A (two comparisons + comment). Fix B (`at_floor` + comment).
4. Run the full suite (`.venv/bin/python -m pytest dev/tests -q -n 8`).
5. RELEASE_NOTES + TODO as above. `manifest.json` is already 2.1.6 - leave it.
6. Commit locally on dev: "Tank target flipping: ... N passed". **No push** until Anze says - a push
   auto-deploys to Home and Kozolec.

## Verify after install

Home and Kozolec get the push by default. Snapshot before and after, and add a NOTES.md line per site. Both run **Priority**,
so fix A is inert there; fix B is what could change behaviour.

- **Home, `climate.workshop_boiler`** (Freeze Protection, always far above its 10 C floor, so fix B rarely
  applies): count 80 <-> 10 changes per day (10-12 had 5 boosts in 3 h, each >= 5 min). Expect the same
  or fewer. Check there are no `hvac_action: heating` bursts under 30 s while the status reads *Waiting for Power*.
- **Kozolec**, three tank-type loads in Normal (`sensor.hot_water_tank_tank_status`,
  `bee_room_heater`, `bathroom_ir_panel`): over a day, check for the same thing - no heating bursts under
  30 s while denied, and no new setpoint churn. Off-grid denial below the minimum SOC is away/protective,
  so it should behave as before.
- **Andrej's site - only if Anze approves the build there** (HACS download + restart, snapshots,
  NOTES.md). Then watch `water_heater.tc` for 2-3 days:
  - target changes/day drop from 28-72 to the boost windows only (42 <-> 45). A 40 should appear only when
    the heat pump starts heating while both cars hold the supply.
  - each `heat_water` run (typically ~06:00-06:40 UTC at ~37.5 C): `sensor.hot_water_tank_tank_status`
    reads *Heating*, `heating_permitted: true`, and `sensor.hot_water_tank_allocated_current` reads 1.3,
    with the run ending at ~42 C rather than 40.5.
  - with both cars charging in the evening: the tank status may still flicker *Waiting for Power* (expected,
    cosmetic), but the target stays 42.
  - the two chargers should be unaffected (EVSE minimums are whole amps) - spot-check that Nova's
    allocation is not 0 more often than before.
