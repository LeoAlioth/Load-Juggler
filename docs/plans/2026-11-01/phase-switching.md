# Phase switching (1 / 3 phases) - design

Agreed with the owner 2026-11-01. Supersedes the third site's "No phase switching (2026-10-08)" note in
`docs/live-sites.md`.

## Why

Andrej's second charger (go-eCharger V4, "Charger Nova") pauses whenever three phases cannot reach its 6 A
minimum: on 2026-11-01 the site's import cap ("Glajena povprecna moc do konca CB", ~6.9 kW at the time) less a
~3 kW house left ~3.9 kW, under the 4.1 kW three phases need at 6 A, and phase B's house load (9-20 A on a 20 A
breaker) did the same at its peaks. One phase at 6-16 A (1.4-3.7 kW) would have fitted every time. The charger
supports it: `ConnectorSwitch3to1PhaseSupported` = true, profiles in amps, and the go-e OCPP page lists
`numberPhases = 1` in SetChargingProfile for phase switching.

A Variable Load user (GitHub LeoAlioth/Load-Juggler issue 21, 2026-10-31) asks for the same through an entity:
write the phase advice to the charger's phase select (it takes 1 and 3), keep the advice from flipping to single
during Hold, and do not take the charger's 0 A dip during a switch for a manual change.

## The rule (owner's answers, 2026-11-01)

- **Who:** every OCPP charger with three legs that reports `ConnectorSwitch3to1PhaseSupported` = true, unless its
  new Configure option "Phase switching" (default on) is off.
- **When one phase:** only to avoid a pause. Three phases whenever three reach the minimum; one phase when three
  would not but one would. (Three phases at the minimum always deliver more than one at the maximum - 4.1 kW
  against 3.7 kW at 6/16 A - so "more power" and "avoid a pause" are the same rule.)
- **Back to three:** when the load's current on all three legs (its potential, `_set_potential_current`) reaches
  the minimum plus `PHASE_SWITCH_UP_MARGIN` (2 A) and stays there without a break for `PHASE_SWITCH_UP_HOLD_S`
  (5 min) - the owner, 2026-11-01, so a site sitting at the edge does not bounce.
- **Spacing:** a change back to three waits `PHASE_SWITCH_INTERVAL_S` (10 min) since the last change. A change to
  one phase that saves a pause goes at once - waiting would be the pause it exists to avoid. So at most one
  round trip per 10 minutes.
- **Mid-session:** allowed. The go-e interrupts charging for a few seconds by itself.
- **Held:** Dynamic Control off, or a connector status that is not drawing (`is_inactive_status`, which includes
  the SuspendedEV idle rewrite to Finishing): the phase count stays as it is. No car (Available): back to three,
  so a new session starts on three.

## Where it lives

1. **Decision, pure** - `calculations/phase_switch.py`: `decide(...)` (the rule above, no HA) and
   `single_phase_permit(site, load_id)` (the permit a load would get on its first line alone this cycle: the
   whole calculation re-run on a deep copy with that load's mask narrowed).
2. **Engine** - `engine/hub_calculation._apply_phase_switching`, right after `calculate_all_load_targets`: per
   switchable load, `decide`; on a change, narrow or widen the load on the real site and run the calculation
   again, so this cycle's permits, pools and circuit groups are all on the new phase count. State in the load's
   runtime bucket (`EVSE_RT_PHASES`, `EVSE_RT_PHASES_SINCE`, `EVSE_RT_PHASES_REASON`). Figures computed earlier in
   the cycle (Excess latch, forecast early start, catch-up) see the old count for that one cycle.
3. **Builder** - `_build_evse_load`: a switchable load on one phase is built with `phases=1` (mask = L1's site
   phase, draw spread over one leg) and `potential_phases_mask` = its three legs, so the existing potential
   figure says what three would get. `LoadContext.phase_switchable` marks it for the engine step.
4. **Capability** - read once per charger with `ocpp.get_configuration` (the read-back helper the refused-setting
   memory uses, moved to `control/ocpp.py` as `read_charger_setting`), retried every 10 min while the charger
   does not answer; cached in the runtime bucket (`EVSE_RT_PHASE_SUPPORTED`).
5. **Command** - `control/ocpp.py`: a charger that supports switching gets `numberPhases` (1 or 3) in its schedule
   period on every profile, so a charger left on one phase by a disabled option or a restart is put back. A W
   profile is encoded with that phase count. A change goes out at once (the command slot is skipped, as for a
   cut), and the permit pipeline restarts from the minimum - the Variable Load's "legs grew" reset, now for both.
6. **Settling** - after a phase change (`RT_PHASE_SWITCH_AT`): the readout watch counts the change as a command
   that moved the limit (`track_commands` keys on limit and phases), so the reading taken before it is replaced by
   the assumed draw on the new legs until a newer one arrives; compliance does not judge for
   `PHASE_SWITCH_SETTLE_S` (120 s); `manual_change` ignores the device for the same window (the Variable Load's
   setpoint dropping to 0 during the charger's switch).
7. **Published** - the phase-mask sensor's attributes: `phase_switching` (supported / not supported / off / not
   asked yet) for a three-leg charger, and while supported `phases_in_use` and `phase_switch_reason` (the reason
   names the minutes left before three phases may return, so no separate next-change time).

## Variable Load

- New optional field "Phase entity" (select, input_select, number or input_number) on its setpoint page. When
  set, the advice is written to it: `1` for single, `3` for multi (the select option that reads "1" / "3", else
  the one whose digits are exactly that). Written when the advice changes or the entity holds another value;
  unset, the advice stays output-only as now. The VL's own thresholds, hysteresis and minimum time stay.
- Hold and the other not-running states: the advice reads what the load would be offered
  (`hub_data["load_available"]`, which for an inactive load is the leftover offer) instead of the 0 it holds, so
  it does not drop to single and a load leaving Hold resumes on all legs when they have room.
- A phase write starts the same settling window, so the setpoint's 0 A dip is not a manual change.

## Not built, and why

- **The 9 A setpoint left on an unused socket** (the same comment): a value sitting on the device raises no
  state-change event, so it cannot start a manual override, and Load Juggler writes over it at its next command
  (an unplugged Variable Load is written its 0). A manual change made hours earlier was a real manual change,
  and the override timeout of 0 (2.1.7) already keeps the load under control. Nothing to fix; answered in the
  issue instead if the owner wants.
- **Two-phase multi**: the Alfen user runs two legs; "multi" means all configured legs, written as 3, which is
  what his charger takes.

## Tests

Pure: the rule (`decide`) case by case, `single_phase_permit` on Andrej's 2026-11-01 numbers (20 A breaker, a
6.9 kW import cap, a 3 kW house with phase B at 9 A: three phases pause, one phase gets 6+ A), and the
calculation's re-run being idempotent. HA: the profile carries `numberPhases`, a change bypasses the command slot,
compliance and the manual-change watch stay quiet in the settling window, the Variable Load writes its phase
entity and its advice holds through Hold.
