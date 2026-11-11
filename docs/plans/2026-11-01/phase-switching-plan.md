# Phase switching - implementation plan

**Goal:** a three-leg OCPP charger that can switch drops to one phase instead of pausing, and a Variable Load
writes its phase advice to an entity. **Spec:** `docs/plans/2026-11-01/phase-switching.md`.
**Execution:** native, in the owner's session (2026-11-01: "write up a spec/implementation plan and get to work").

## Global constraints

- `calculations/` stays HA-free. One algorithm for every phase count (AGENTS.md "Generality").
- No new stored key needs a migration: the new options read with defaults through `get_entry_value`.
- Slovenian strings reuse `sl.json` terms ("faza", "fazni").
- `human_verified: false` on any new scenario. Release notes under 2.1.7.

## Review focus

1. A charger whose capability read fails (offline at start-up) - never switched, retried, never crashes the cycle.
2. Option turned off while on one phase - the next profile carries `numberPhases` 3.
3. Car unplugged while on one phase - back to three, the next session starts on three.
4. 1 -> 3 with the permit sized for one leg - the pipeline restarts from the minimum, never 16 A on three legs.
5. A W-unit charger - the profile's watts use the phase count it was told.

## Task 1: the rule and the trial (pure)

- Create `calculations/phase_switch.py`: `ONE, THREE`; `decide(on, since, now, *, connected, held, permit,
  min_current, single_permit, potential, margin, interval_s) -> (phases, since, reason)`;
  `single_phase_permit(site, load_id) -> float`; constants in `const/evse.py`: `PHASE_SWITCH_INTERVAL_S = 600`,
  `PHASE_SWITCH_UP_MARGIN = 1.0`, `PHASE_SWITCH_SETTLE_S = 120`.
- `LoadContext.phase_switchable: bool = False`.
- Tests `dev/tests/test_phase_switch.py`: each branch of `decide`; Andrej's numbers through `single_phase_permit`;
  `calculate_all_load_targets` run twice gives the same permits.

## Task 2: engine, builder, publishing

- Runtime keys in `const/evse.py`: `EVSE_RT_PHASES`, `EVSE_RT_PHASES_SINCE`, `EVSE_RT_PHASES_REASON`,
  `EVSE_RT_PHASE_SUPPORTED`, `EVSE_RT_COMMANDED_PHASES`; `RT_PHASE_SWITCH_AT` in `const/common.py`;
  option `CONF_PHASE_SWITCHING` (default True).
- `_build_evse_load`: `phase_switchable` = three legs, option on, supported; on one phase: `phases=1`,
  `potential_phases_mask` = the three legs.
- `hub_calculation._apply_phase_switching(hass, site, now)` after the calculation; re-runs it on a change.
- `hub_result`: `load_phase_switch` per load; `entities/load_sensors.py` phase-mask attributes.
- Tests: the engine step on a built site (one phase on a squeeze, back after the interval, held keeps, Available
  resets).

## Task 3: capability, command, settling (HA)

- `control/ocpp.py`: `read_charger_setting(hass, devid, key)` (moved from `__init__.py`), `probe_phase_switching`
  (cached, retried every 10 min), `numberPhases` in the period, W encoding by the told phases, records
  `EVSE_RT_COMMANDED_PHASES` and `RT_PHASE_SWITCH_AT` on a change.
- `entities/load.py`: a pending phase change skips the command slot and restarts the pipeline from the minimum
  (shared with the Variable Load's legs-grew reset); probe call.
- `engine/readout_watch.track_commands(..., phases=None)`; `_watch_readout` passes the told phases.
- `control/compliance.py`: no judging inside `PHASE_SWITCH_SETTLE_S`. `control/__init__.manual_change`: same.
- Config: "Phase switching" checkbox on the charger page (schemas, groups, strings en/sl).
- Tests in `test_sensor_update.py` style: profile carries `numberPhases`; change goes out at once; compliance quiet.

## Task 4: Variable Load

- `CONF_VL_PHASE_ENTITY_ID`; `control/variable_load.py` writes 1 / 3 (`phase_value(state, advice)` pure helper in
  `calculations/variable_load.py`), sets `RT_PHASE_SWITCH_AT`; the advice reads `max(limit, offered)`.
- Config field, strings en/sl. Tests in `test_variable_load.py` / `test_variable_load_ha.py`.

## Task 5: docs and release

- `RELEASE_NOTES.md` 2.1.7, `CHARGE_MODES_GUIDE.md` section, `docs/live-sites.md` third-site note,
  `AGENTS.md` code-structure lines, `dev/TODO.md`.
- Full suite on this Mac, commit; push to `dev` and the third-site install only on the owner's word.
