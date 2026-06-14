# Load Juggler — Open Issues

1. **Icon not shown in HA/HACS** — HA does not load `icon.png` from the custom component directory. The icon must be submitted as a PR to the [Home Assistant brands repo](https://github.com/home-assistant/brands). The `icon.png` file exists at `custom_components/dynamic_ocpp_evse/icon.png` and is ready to submit.

## Candidate bugs from code review (2026-06-11)

Found during the off-grid inverter-limit fix session. Ordered by confidence; spot-checked against the source. Resolved items have been removed — see `git log` and `RELEASE_NOTES.md` for what was fixed; the rest need a failing scenario before fixing.

### Medium confidence

7. **Shared distribution starves cross-phase chargers** — `target_calculator.py` `_distribute_per_phase_shared` (~line 927-952): pass 2 takes `min_available` across ALL wanting chargers' masks and `break`s at ≤ 0, so one charger on an exhausted phase pins every other charger at min even with headroom free on their phases. Same loop scales every charger's increment by solar scarcity, including Standard/full-power chargers that are grid-backed.

8. **Hub calc runs once per device entry per cycle — cycle-counted state advances N× too fast** — `sensor.py` creates one coordinator per charger entry, each calling `run_hub_calculation()`. With N managed devices the settle counter (`SETTLE_DRAW_CYCLES`) and the input EMAs advance N× per real interval: `draw_settled` trips while a car is still ramping (footprint shrinks prematurely, gap over-granted), and input smoothing is effectively disabled on multi-device sites. **Needs its own session** (architectural; Docker pytest suite).

9. ~~**Watts-profile compliance check uses the wrong phase count**~~ **FIXED 2026-06-14** — `control/compliance.py` now decodes the offered power with the same factor the command encodes with (`_car_active_phases or _phases or 1`), so a 1-phase car at 16 A on a 3-phase EVSE reads back 3 680 W → 16 A (was 5.3 A) and no longer trips the auto-reset loop. Regression tests: `test_compliance_watts_decode_uses_car_active_phases` (+ positive control `…_detects_real_mismatch`) in `test_sensor_update.py`; both verified to fail with the fix reverted. **Still wants validation against a live Watts-reporting charger** to confirm `power_offered` echoes the commanded total power as assumed.

12. **Series household clamps to 0 under sensor lag** — `calculations/utils.py` `compute_household_per_phase` (~line 56-58): `max(0, inverter_output − charger_draws)` reads household 0 when the OCPP draw rises faster than the inverter-output sensor updates, transiently overstating per-phase inverter headroom by the full household. No hold-last guard, unlike grid CT staleness.

17. **Battery-backed binary plugs silently top up from the grid** *(found 2026-06-11 while writing regression tests)* — an above-min/above-target binary plug is SOC-gated by design (battery = stored-solar buffer) and is not bounded by the solar pool, so when the inverter is saturated the plug stays on and the difference is imported from the grid (observed: Solar Only plug on, inverter at its cap, grid importing 3.2 A). Contradicts the "none of them use the grid" intent in `test_battery_plug.yaml`. Needs a design decision: shed the plug when the inverter can't cover it, or accept grid top-up as the documented behavior.

13. **Hub sensor inverter headroom ignores wiring topology** — `hub_calculation.py` `_build_hub_result` (~line 1035-1043): `current_inverter_output = max(0, solar + battery_power)` is the series model; for parallel (AC-coupled) sites with the battery charging, output is understated by the charge power and `total_site_available` is inflated. (Display-layer sensors only; the engine pools have their own path.)

### Low confidence / design questions

14. **Smoothing snaps to target when restarting from 0** — `control/smoothing.py` (~line 37-41): `_rate_limited_current == 0` resets all smoothing state to `raw_allocated`, so 0 → 32 A in one cycle, bypassing `RAMP_UP_RATE`. May be intended fast-start; contradicts the ramp design and leans on the compliance "ramping" skip.

15. **`_read_inverter_output` takes `abs()`** — `hub_calculation.py` (~line 126): a hybrid inverter importing AC to charge its battery (negative reading) is counted as positive output; series derived solar then fabricates production (|import| + |charge|). Only matters if users feed signed sensors.

All other issues have been moved to detailed TODOs in `TODO.md`.
