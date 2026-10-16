# LJ-3: Verify four shipped Load Juggler fixes on the real sites

Repo: Load Juggler `~/Projects/Dynamic_OCPP_EVSE`, `dev/TODO.md`, the four entries marked "verify".
The owner approved the checks on 2026-10-12. This is a personal project, so don't log it to work-context.

The check reads history only. It changes nothing in HA. The only edit afterwards is `dev/TODO.md`: commit
it locally on dev and don't push (dev auto-deploys).

Script: `~/Projects/plans/2026-10-12/lj-3-verify.py`. It uses only the stdlib and has had a
syntax check only; nobody has run it yet.

```
python3 ~/Projects/plans/2026-10-12/lj-3-verify.py          # all four
python3 ~/Projects/plans/2026-10-12/lj-3-verify.py 1 4      # some
END=2026-10-14T06:00:00Z python3 .../lj-3-verify.py 2                  # fixed end (default: now)
```

What the script does:
- **Reads:** `/api/history/period/<start>?end_time=<end>&filter_entity_id=...` for every item.
  - Entities whose setpoint or count is an attribute are fetched with attributes
    (`significant_changes_only=0`).
  - Everything else is fetched with `minimal_response&no_attributes`.
  - Item 1's meters (every few seconds) are fetched only for the windows when both cars were plugged in.
  - Item 4 also does one `GET /api/error_log` on Kozolec.
- **Tokens:** read from `Load_Insights/data/<host>` inside the script and never printed.
- **Prints:** one `VERDICT n:` line per item: PASS / FAIL / REVIEW / NOT EXERCISED / N/A, with the cases
  behind it.

## Install times (site NOTES.md, local CEST = UTC+2)

NOTES times are local. The Pond automation's last trigger, 09:45 UTC, matches the 12:00-local install of
the restart feature.

| Item | Build that shipped it | Site | Installed | Window start (UTC) |
|---|---|---|---|---|
| 1 slow meter | c32bbc7 (assumed over, two-cycle pause) + 2139595 (Optimized table / 1 A margin) | the third site | 2026-10-04 ~20:30 / ~21:00 | `2026-10-04T19:00:00Z` |
| 2 leaving boost | ed155b7 | Home | 2026-10-04 ~18:36 | `2026-10-04T16:36:00Z` |
| 2 leaving boost | ed155b7 (in the c32bbc7 build) | the third site | 2026-10-04 ~20:30 | `2026-10-04T18:30:00Z` |
| 3 saturation latch | 2.1.3 | none (see item 3) | - | - |
| 4 no-draw restart | fed9c6f (2.1.6-dev.20261005.0936) | Kozolec | 2026-10-05 ~12:00 | `2026-10-05T10:00:00Z` |

Later builds inside these windows change what the history should show:
- **The third site:** 2d20d71 (climb, 10-07 ~20:00), 2ec16da (reset wait, 10-08 ~21:20), d124219 (the pause's
  0 A is sent once; 1905 build, 10-09), 2.1.6-dev.20261012.0946 and 37fd304 (10-12).
- **Tanks:** b2bc123 (10-09: a denied tank whose device says it heats at its floor target goes to its
  lowest), 2ecf2cd (10-10: boost on Excess only), 37fd304 (10-12: the boost rides the Solar/Excess grace
  period).

## Did the situation happen? (cheap probe, 2026-10-12 ~14:00 UTC)

1. **Both of the third site's cars plugged in: once.**
   - The window ran 2026-10-11 20:06Z -> 2026-10-12 03:07Z (7 h). Both connectors were `Charging` for
     about 68 min, from 23:36Z.
   - `select.site_load_management_distribution_mode` was **Sequential - Optimized for the whole window**,
     so the Priority checks have not been exercised. The Optimized checks have, once.
   - The status text is live: `sensor.charger_charging_status` read *Charging (waiting for a new
     reading - assumed current)* at the probe, with `readout_assumed_current: [14.9, 14.9, 14.9]`.
2. **Leaving boost:**
   - **Home:** yes, many times (10-05, 10-06, 10-07, 10-11, 10-12). Every probe sample shows 80 -> 10 in
     `heat`, never `off`.
   - **The third site:** the boost ended about 9 times (45 -> ...), but the tank always read **below 42**
     (39-41.5 °C) at that moment. There, 42 -> 40 (lowest) is the intended behaviour, so the fixed case
     (tank at or above its floor) has probably **not happened yet**. The script judges this.
3. **Saturation latch:** no site has the topology (details under item 3).
4. **Pond EVSE:** the feature has been on since 10-05 (see item 4). Since then the status has read
   *Restarted (no draw 10 min)* 14 times and *No draw (gave up after 3 restarts)* 7 times.
   `restart_count` is 2 at the probe.

---

## Item 1: Slow meter, the assumed draw on the third site's two go-eChargers (site `third`)

**Entities** (from states and the entity registry):
- **Stara, priority 1, "first":**
  - `sensor.charger_status_connector` (OCPP connector)
  - `sensor.charger_allocated_current` (LJ command, A)
  - `sensor.charger_current_import` (reading, A)
  - `sensor.charger_power_active_import` (kW)
  - `sensor.charger_charging_status` (LJ status text)
  - `sensor.charger_effective_priority`
- **Nova, priority 2, "second":** the same names with `charger_nova_`.
- **Site:**
  - `select.site_load_management_distribution_mode` (options: Shared / Priority / Sequential - Optimized /
    Sequential - Strict)
  - `sensor.site_load_management_available_current_a|b|c`: LJ's room for its loads, own draw handed back
- **Meters:**
  - `sensor.solaredge_i2_m1_ac_current_a|b|c` (A)
  - `sensor.solaredge_i2_m1_ac_power_a|b|c` and `sensor.solaredge_i2_m1_ac_power` (W, export-positive,
    so negative means import)
  - `sensor.glajena_povprecna_moc_do_konca_cb` (block allowance, W; LJ's Max Import Power)
- **Main breaker:** 20 A.

**Windows:** "both plugged" means both connectors are not in Available / Unavailable / unknown / Faulted /
Finishing.

**Pass/fail** (from the TODO text). Each check is evaluated in every both-plugged window:
- **(a) The first charger's command settles.** The bug was 7/15/8/14 A every 30 s. While the first charger
  is `Charging`, a "reversal" is a step of 3 A or more followed by an opposite step of 3 A or more.
  - FAIL if any 5 minutes hold 4 or more reversals. The bug pattern gives about 10.
- **(b) The second charger is not paused while there is room.** Paused means connected with allocated
  under 1 A.
  - **Room needed:** 12 A per phase (both 6 A minimums) in Priority/Shared, 17 A in Optimized (16 A for
    the first charger plus the 1 A start margin).
  - **Two views of the room:**
    - LJ's view is `min(available_current_a..c)`. It is reported only, because the bug made LJ's own view
      wrong.
    - The meters' estimate is `min over phases (20 - grid import A + both chargers' A)`, together with
      `(allowance W + grid W + chargers W) / 690`.
  - FAIL if the second charger is paused for more than 3 minutes while the meters show room of the
    threshold + 1 A or more.
  - FAIL in Optimized also if the second charger's allocation flips 0 <-> on 4 or more times in 10 minutes
    (the 6/0 flicker).
- **(c) The breaker stays at 20 A once settled.** From 5 minutes after both cars start charging, FAIL on
  any span longer than 60 s with a phase importing more than 20.5 A.
- **(d) The status text at session start.** At every connector change into `Charging` (either charger,
  whole window), the status should read *Charging (waiting for a new reading - assumed current)* within
  120 s.
  - Misses are listed as REVIEW, not FAIL: if a meter value arrives in the same 5 s cycle, the text may
    never show.

**Verdict:**
- NOT EXERCISED if the cars were never both plugged in.
- The **Priority** half of the TODO stays unexercised while the mode is Optimized. The script says so on
  the PASS line.
- To exercise it, the owner could switch the third site's distribution mode to Priority for one two-car evening. That is
  his call and the third site, so the plan doesn't do it.

## Item 2: Leaving boost (the third site `water_heater.tc`, Home `climate.workshop_boiler`)

**The third site entities:**
- `water_heater.tc` (MELCloud Ecodan; setpoint in attribute `temperature`, tank in `current_temperature`;
  `min_temp` 40 = its lowest; operation_list auto / force_hot_water, no off mode)
- LJ `sensor.hot_water_tank_tank_status` (attributes `setpoint_source`, `heating_permitted`)
- `select.hot_water_tank_operating_mode` (Normal throughout)
- `number.hot_water_tank_{away,normal,boost}_temperature` = 40 / 42 / **45**. The boost was 44 until
  10-06 19:47, then 46 -> 45. It is no longer 46 as the TODO says.

**Home entities:**
- `climate.workshop_boiler` (generic_thermostat on `switch.workshop_bolier`; `min_temp` 7, `max_temp` 80)
- The LJ tank is the entry "Hot Water Tank", with entity prefix **`server_room_hot_water_tank`**:
  - `sensor.server_room_hot_water_tank_tank_status`
  - `select.server_room_hot_water_tank_operating_mode`: **Freeze Protection** throughout
  - `number.server_room_hot_water_tank_{away,normal,boost}_temperature` = **10** / 42 / 80

**What the code does now** (`control/hot_water_tank.py:281-319`):
- The floor is away in Freeze Protection (and below the battery's minimum SOC); otherwise it is normal.
- A denied tank whose `current_temperature` is at or above the floor is set to the floor, in heat.
- A tank below its floor is sent off, or its lowest target. That is by design: such a tank would heat.

**Correction to the TODO:**
- Home's boiler is in Freeze Protection, so the right answer is **80 -> 10 in heat**, not 80 -> 42.
- 80 -> 42 would be right only in Normal mode.

**Pass/fail:**
- A "boost exit" is a setpoint drop from above normal + 0.5.
- If the tank was at or above its floor at that moment, the **first** new setpoint must equal the floor and
  the state must not be `off`. The third site: 45 -> 42, never 40 or off. Home: 80 -> 10 heat, never off.
- Exits with the tank below its floor are printed as "by design, not judged".
- A later floor -> lowest while the device says it heats is b2bc123, not this bug. The original bug went
  straight 46 -> 40, so it shows as the first new setpoint being wrong.
- Verdicts:
  - PASS if there is at least one judged exit and all judged exits are OK.
  - NOT EXERCISED if there are none (likely for the third site, see the probe).
  - FAIL otherwise.

## Item 3: Meter-only hybrid at its rating (saturation latch)

The latch runs only on a site that has all of these (`engine/hub_calculation._apply_saturation_latch`):
- grid-tied
- `solar_is_metered`, meaning no solar sensor and no output sensors
- an Inverter Max Power
- `battery_power` read
- a discharge rating above 0

**No site has that** (checked 2026-10-12 from the 10-12 diagnostics snapshots and live states):
- **Kozolec:** off-grid (Victron MultiPlus-II, battery sensor `sensor.gx_device_dc_battery_power_inverted`).
- **Home:** grid-tied SolarEdge SE17K with **no battery**. The inverter's `battery_power` is None, and LJ
  reads a solar sensor (`_ema_inputs.solar_<inv>`) and per-phase output sensors (`inv_<inv>_0..2`).
- **The third site:** grid-tied SolarEdge with **no battery** (no battery entity on the site).

Verdict: N/A. The script prints this; it needs no history.

## Item 4: Restart if it stops drawing, Kozolec's Pond EVSE

**Already enabled. No steps need the owner's OK.**
- Since 2026-10-05 ~12:00 local (LJ 2.1.6-dev.20261005.0936), entry `01KVZQN00Y326HES6214DGSRCK`
  (Pond EVSE Smart Load) has `plug_restart_on_no_draw = True` and `plug_restart_after = 10` min on
  `switch.pond_evse`, with monitor `sensor.pond_evse_power` (W).
- `automation.reset_pond_evse` is **off**. It last triggered 2026-10-05 09:45 UTC (kozolec/NOTES.md line
  338; confirmed live at the probe).
- The TODO's "Turn it on ..., disable the owner's 15-min automation" is done. Only the check remains.

**Entities:**
- `sensor.pond_evse_status` (LJ; attribute `restart_count`, fetched with attributes because consecutive
  restarts don't change the text)
- `switch.pond_evse`
- `sensor.pond_evse_power`
- `sensor.pond_evse_allocated_current` (permit; above 0 means permitted)
- `select.pond_evse_operating_mode` (Solar Only)

Log: `GET /api/error_log` on Kozolec, counting lines with "still draws nothing after" for Pond. This only
covers the run since the last HA restart, and HA restarted several times on 10-12.

**A restart** is `restart_count` stepping up. Each one must have all of these, or it is BAD:
- power under 10 W for the 10 min before it
- permitted at that moment
- the switch off for 20-90 s (30 s expected)
- at least 30 min after the previous restart of the same run
- a count of 3 or less

**Kinds of restart:**
- **Cut-out:** it drew 1 kW or more in the 30 min before the quiet 10 min. This is the over-temperature
  case.
- **Resumed:** it drew 100 W or more within 15 min of the restart.
- **Recovery:** after a resume with 5 min of draw, `restart_count` must be 0. Otherwise BAD.

**Car full:** each run should end at *No draw (gave up after 3 restarts)*, with no 4th restart. Each
give-up is listed. Note that in Solar Only the permit drops at dusk or under clouds, which resets the count
by design.

**Verdicts:**
- FAIL on any BAD restart, or if no cut-out restart resumed.
- REVIEW if every restart is correct but none followed a cut-out (the over-temperature case not seen).
- PASS if at least one cut-out resumed and the count reset.

## Afterwards: dev/TODO.md (commit locally on dev, don't push)

**Per verdict:**
- **PASS:** delete the entry. If a known ceiling in its text is still open, keep that ceiling as a short
  line in the release notes or guide where it already lives; don't keep a TODO for it.
- **NOT EXERCISED:** keep the entry and add `*(checked YYYY-MM-DD: <situation> not seen since <install>;
  re-run plans/2026-10-12/lj-3-verify.py N)*`.
- **REVIEW:** look at the listed cases. Then treat it as PASS or FAIL.
- **FAIL:** keep the entry, rewrite it as a [BUG] with the evidence (times, values), and drop "verify".

**Per item:**
- **Item 1:**
  - If only Optimized was exercised and it passed, remove the Optimized sentence and keep the entry for
    Priority, annotated.
  - The **Known ceiling** (the ocpp integration writes the measurands before the connector status) is not
    something a history check can verify. Keep it either way.
- **Item 2:**
  - Fix the text either way: Home's boiler is in Freeze Protection, so the expectation is 80 -> 10
    (away) in heat. The third site's boost is 45 now.
  - If Home passes and the third site is not exercised, the owner decides whether one site is enough. The fix is the
    same code path for climate and water_heater except `_command_water_heater`. Suggestion: keep the entry
    for the third site only.
- **Item 3:** keep it, annotated *(no site has this topology - Home and the third site have no battery, Kozolec
  is off-grid; checked 2026-10-12)*, until a site with a meter-only hybrid exists.
- **Item 4:** drop the "Turn it on ... disable the automation" sentence (done 10-05). Then decide by the
  verdict.

## Seen in passing (not these TODOs; for the owner, nothing changed)

- **The third site, `water_heater.tc`:** it flips 42 <-> 40 every ~5 min for hours when the tank is under 42
  (e.g. 10-05 17:20-19:42Z, 10-07 13:15-16:17Z, 10-11 20:07-23:33Z). That is the permit coming and going
  for a tank below its floor. Several of these spans overlap charging sessions.
- **Home, `climate.workshop_boiler`:** 80 <-> 10 every ~5 min on 10-06, 10-07 and 10-11 (Excess flapping).
  The 37fd304 grace period (installed 10-12) should damp it. 10-12 already shows longer holds (12:05-12:29,
  13:41-13:48Z).
