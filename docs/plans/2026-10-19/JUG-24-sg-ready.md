# JUG-24 SG Ready device type

Agreed with the owner on 2026-10-19. The design below is settled; the implementation details that are
not in it are the implementer's to propose (see "Open implementation points").

## What SG Ready is

A label for heat pumps (BWP, Germany). The device has four operating states, selected by two
potential-free contacts (a Shelly with two relays does it). The pump's own controller decides what each
state does.

| State | Relay 1 | Relay 2 | Meaning |
|---|---|---|---|
| 1 Block | closed | open | utility lockout, the pump is off |
| 2 Normal | open | open | regular, energy-efficient operation |
| 3 Recommended on | open | closed | run harder (higher setpoints); a recommendation, the draw is the pump's choice |
| 4 Force on | closed | closed | run-up command, as hard as it safely can; model dependent |

Relays (0,0) = Normal, so a Shelly that loses power or loses Home Assistant falls back to Normal.
This is the safe state; unload, restart and "relay entity unavailable" must also leave Normal.

## State rule (no user modes)

The device has no operating mode of its own. The state follows the site, first match wins:

| Order | Condition | State |
|---|---|---|
| 1 | overload: the same shortage that pauses an EVSE outside the Solar / Excess modes, and it reaches this device in the priority order | Block |
| 2 | actual excess (the Excess trigger of the site) | Force on |
| 3 | an early-start verdict says the unit should already run (the forecast "Vnaprejšnja poraba viška", see `forecast_excess.py`, `engine/hub_calculation._apply_forecast_excess`) and that hub setting is on | Recommended on, until real excess arrives, then Force on |
| 4 | battery above its target SOC (sites with a battery) | Recommended on |
| 5 | otherwise: battery below target, and most of the time on battery-free sites | Normal |

- Battery-free sites: row 4 never applies, so they go Normal -> Force on excess, with nothing between.
- Row 3 follows the existing hub switches, no per-device toggle. The PV-clipping early start
  (`forecast_clip_early_enabled`) is an open question for the owner (below).
- The state sensor shows the reason too ("Recommended (predicted excess)", "Recommended (battery above
  target)"), so the two Recommended causes can be told apart.

## Timing limits (the signal sender enforces them: the standard puts them on us)

Settings per device, all with defaults:

| Setting | Default | Meaning |
|---|---|---|
| minimum time per state | 10 min | a state stays at least this long and can only be re-entered this long after it ended; the standard's 10-minute rule (the sources disagree on whether it covers every state, so it applies to all four) |
| maximum block time | 120 min | one Block lasts at most this long |
| maximum blocks per day | 3 | counted over a rolling 24 hours, not the calendar day |

- Block is a last resort: it is reached only when the overload shortage actually gets to this device in the
  priority order. Lower-priority loads (the EVSE) are cut first, as today. The device must not use up an
  allowance for a shortage something else can absorb.
- **Release rule** (avoid wasting the 3-per-day allowance): leave Block only when the grid headroom covers
  the device's Normal-state draw plus a margin. The draw is its last measured Normal-state power from the
  optional power sensor, or the nominal power when there is none. This stops the usual re-block, where the
  pump comes back and its own draw pushes the house over the limit again.
- **Allowance used up** (3 blocks in the rolling 24 h, or a block at its maximum time): go to Normal, never
  Block, and raise a repair or log note so the owner can see that the overload went unanswered. The other
  loads and the site's own limits then have to handle it.
- Later, optional (not part of this build): hold a Block when the Load Insights house forecast shows a peak
  within the next two hours.

## Configuration (one device entry, "SG Ready heat pump")

- Two switch entities (relay 1, relay 2) - required.
- Nominal power in W (the extra draw it is assumed to add above Normal in states 3/4), the phases it is on,
  priority - the same shape as a plug.
- Optional power sensor (W or kW). Optional-entity pattern, as for `tank_power_entity_id` and
  `inverter_max_power_entity_id`: read each cycle, held while unavailable, the fixed value until first read.
  The Normal-state draw is just part of the household; only the extra draw above Normal is counted as a
  managed load.
- The three timing settings above.
- Optional Load Insights inputs, like the other loads: "Prostor za porabo danes" (room) and "Moč sprejema"
  (rate) for the early-start group; reuse the existing keys (`early_room_entity_id`, `early_rate_entity_id`).
- The per-load Dynamic Control switch, if the other loads have one: off leaves the relays at Normal.

## Entities

- A state sensor (Block / Normal / Recommended / Force) with the reason and the block counters as
  attributes (blocks in the last 24 h, minutes left in the current state's minimum time).
- No select or number for the user to pick a mode.

## Where it goes in the code (starting points, verify)

- Pure state machine: a new `calculations/sg_ready.py` (no HA imports; memory is a small dataclass like the
  pacing / outage-window memories), fed by what the engine already knows: the device's allocation
  outcome, the site shortage, the Excess latch, the early-start verdict, battery SOC vs target, headroom.
- Engine: a new load type in `engine/load_builders.py` (see `_build_plug_load` and the hot-water tank for
  the optional power sensor and the fixed-power fallback), a hook in `engine/hub_calculation.py` where the
  per-load results are final.
- Actuation: a new `control/sg_ready.py` (see `control/plug.py`, `control/hot_water_tank.py`): switch the
  two relays, never both changed in one burst if the second call can fail (end at a valid combination),
  and verify against the relay states.
- Config flow: new steps in `config_flow/` (schemas, flow, options, pages), the stored keys in `const/`,
  a migration step in `__init__.py` + `MINOR_VERSION` bump in `config_flow/flow.py` only if stored keys
  change for existing entries (a new device type probably needs none; check), with tests in
  `dev/tests/test_config_flow_e2e.py`.
- Translations: `strings.json`, `translations/en.json`, `translations/sl.json`. The owner reviews every
  Slovenian string; reuse the terms already in `sl.json` and these decided ones: "bojler" (the water tank),
  "Moč sprejema", "Prostor za porabo danes", "Vnaprejšnja poraba viška", "Prednost sončne energije",
  "Najkrajši čas izklopa", "Navaden PV razsmernik" / "Hibridni PV razsmernik". Do not invent a new term for
  something that has one.
- Docs: `CHARGE_MODES_GUIDE.md`, README, `RELEASE_NOTES.md` (user-facing, plain language, under the open
  version), the architecture tree in `AGENTS.md`, remove JUG-24 from `dev/TODO.md` when done
  (leave a one-line note for the optional forecast hold).

## Tests

- Pure tests for the state machine: every row of the table, the 10-minute rule, the Block limits, the
  rolling 24 h, the release rule, battery-free path, allowance used up, relay failures, restart -> Normal.
- A YAML scenario or two in `dev/tests/scenarios/features/` (`human_verified: false`).
- Config-flow e2e for the new device.
- The owner approves running the tests for this task: one command per call, from the repo root, with the
  project venv (`dev/tests/README.md`).

## Open implementation points (propose, then proceed unless blocked)

1. How "overload reaches this device" and "actual excess" map onto the existing allocation (priority,
   pools, `_source_limit`), without special cases per site type (AGENTS.md "generality over special cases").
2. Whether the device sits inside circuit groups and the household figure (it must not double-count its
   own Normal draw: that is household).
3. The headroom margin for the release rule (a sensible default, a setting only if needed).
4. Question for the owner, not for you to decide: should the heat pump also follow the PV-clipping early
   start (`forecast_clip_early_enabled`) and go to Recommended before a forecast clip? Build so that adding
   it is one condition, default it to following the forecast-excess early start only, and report it.

## Rules for the work

- Commit locally on `dev` as checkpoints. Never push (a push to `dev` deploys to both live sites).
- No new branches, worktrees or detached checkouts.
- Mark new scenarios `human_verified: false`. Slovenian strings are the owner's to review: list every new one
  in your report.
- Do not touch Load Insights; do not deploy anywhere.

## Implementer's note (2026-10-19): mapping and decisions

1. **Allocation mapping.** The device is ONE `LoadContext` in the existing allocation: a binary EXCESS load (`device_type sg_ready`, always "Charging") whose rating is the nominal extra power. It therefore joins the early-start group (`early_room_kwh`, `early_rate_w`), the Excess pool, the phase-is-buying guard and circuit groups with no new branch in the calculator.
   - Force = the grant (`available_current > 0` after everything) AND the site's Excess latch (or a full battery, which the Excess behaviour treats the same way). A grant resting on the forecast verdict alone is not actual excess.
   - Recommended / early start = `hub_runtime["_early_planned_on"]` (forecast-excess source, needs the hub option and this load in the group).
   - Recommended / SOC and Normal = SOC against the latched target; never true without a battery.
   - Block = `target_calculator.grid_headroom_w` (signed, unclamped `_calculate_grid_limit`: breaker less buffer, and max import power) on the device's phases, read from `site.consumption` - the household with every managed load off, so the other loads are cut first by construction. It reaches several pumps lowest priority first (`overloaded_devices`). No grid limit (off-grid) = never overloaded.
2. **Circuit groups and household.** The device may be a member of a circuit group like any load; the group limit then counts its extra draw only. Its Normal draw stays in the household (never a managed draw), so nothing is counted twice. Measured extra draw = power sensor less the last Normal-state reading (held while unavailable); without a sensor, the nominal power while the state is Recommended or Force.
3. **Release margin.** The hub's `excess_trigger_margin` (500 W default) on top of the Normal draw - the integration's existing lead below a limit; no new constant.
4. **Open point 4 (owner's question).** Following the PV-clipping early start is the single flag `sg_ready.FOLLOW_CLIP_EARLY` (default False): the hook reads `hub_runtime["_early_clip_on"]` beside `["_early_planned_on"]`.

Other choices: no stored key changes for existing entries, so no migration and no `MINOR_VERSION` bump. There is no `strings.json` in this repo; the strings live in `translations/en.json` and `sl.json`. Dynamic Control off drops the relays to Normal once, then hands off; a manual override (the relays operated outside Load Juggler) is left alone. The block history is in memory only, so a restart starts in Normal with no history.

### Overload timing (owner feedback, 2026-10-19)

Two causes, two timings. The shortage is split by `grid_headrooms_w` into the breaker headroom and the max-import-power headroom.
- **Breaker.** A Slovenian meter or breaker tolerates more than 110 % of its rating for only about 90 s, so this path is prompt: a 10 s noise filter (`BREAKER_HOLD_S`) and nothing more. The input is already smoothed by the engine's EMA (`EMA_TAU_S` 5.6 s, about 13 s to 90 % of a step), so the total is about 13 s + 10 s + one cycle, a quarter of 90 s. The threshold is the breaker rating less the power buffer, which is the limit the allocation already works to (more conservative than 110 %). It also does NOT wait for the minimum time of the state it interrupts (the Block then lasts its minimum). This is a deliberate exception to the 10-minute rule, for safety.
- **Max import power** (the 15-minute-block limit). Block only after 2 continuous minutes over it (`IMPORT_HOLD_S`), the timer resets when it drops under, and the state's minimum time still applies.
- The release margin is `excess_trigger_margin` (point 3 above).
