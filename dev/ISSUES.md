# Load Juggler — Open Issues

1. **Icon not shown in HA/HACS** — HA does not load `icon.png` from the custom component directory. The icon must be submitted as a PR to the [Home Assistant brands repo](https://github.com/home-assistant/brands). The `icon.png` file exists at `custom_components/dynamic_ocpp_evse/icon.png` and is ready to submit.

## Candidate bugs from code review (2026-06-11)

Found during the off-grid inverter-limit fix session. Ordered by confidence; spot-checked against the source. Items marked **FIXED** were fixed the same day (see `git log`); the rest need a failing scenario before fixing.

### High confidence

2. ~~**Battery discharge headroom ignores discharge already in flight**~~ **FIXED 2026-06-11** — `_calculate_solar_surplus` now estimates the inverter's current output as solar + in-flight discharge (accurate branch) or `max(export-based estimate, actual discharge)` in derived/CT mode — in derived mode the export already contains the discharge, so it must not be subtracted twice. Regression scenario: `night-battery-household-inverter-headroom`.

3. ~~**Excess-export hysteresis evaluated on pre-feedback export**~~ **FIXED 2026-06-11** — the hysteresis block moved after `_apply_feedback_loop` and now evaluates `site.total_export_power` (post-feedback), the same figure the excess pool compares.

4. ~~**Inverter output entities ignored unless phase A is configured/readable**~~ **FIXED 2026-06-11** — each phase is read independently; configured-but-unreadable phases carry the `_UNAVAILABLE` sentinel which the EMA smoothing resolves to the last known value.

5. ~~**Grid-stale fallback force-permits binary loads ON**~~ **FIXED 2026-06-11** — only an EVSE already charging keeps a `min_current` permit; binary loads and idle EVSEs get 0.

### Medium confidence

6. ~~**Symmetric inverter per-phase cap ignores household**~~ **FIXED 2026-06-11** — the symmetric branch of `_build_inverter_constraints` (and the inline symmetric path in `_calculate_solar_surplus`) now caps per-phase at `max_per_phase − household`, same as asymmetric. Regression scenario: `symmetric-phase-household-caps-solar-only-evse`.

7. **Shared distribution starves cross-phase chargers** — `target_calculator.py` `_distribute_per_phase_shared` (~line 927-952): pass 2 takes `min_available` across ALL wanting chargers' masks and `break`s at ≤ 0, so one charger on an exhausted phase pins every other charger at min even with headroom free on their phases. Same loop scales every charger's increment by solar scarcity, including Standard/full-power chargers that are grid-backed.

8. **Hub calc runs once per device entry per cycle — cycle-counted state advances N× too fast** — `sensor.py` creates one coordinator per charger entry, each calling `run_hub_calculation()`. With N managed devices the settle counter (`SETTLE_DRAW_CYCLES`) and the input EMAs advance N× per real interval: `draw_settled` trips while a car is still ramping (footprint shrinks prematurely, gap over-granted), and input smoothing is effectively disabled on multi-device sites. **Needs its own session** (architectural; Docker pytest suite).

9. **Watts-profile compliance check uses the wrong phase count** — `control/ocpp.py` builds the W-based profile with `_car_active_phases` (e.g. 1-phase car → 16 A = 3 680 W) but `control/compliance.py` converts the offered power back with hardware `_phases` (3 680 W / 3 phases = 5.3 A ≠ 16 A commanded) → perpetual mismatch → auto-reset loop on 1-phase cars at 3-phase EVSEs. **Needs OCPP-side testing.**

10. ~~**Non-numeric grid CT state silently reads 0 A**~~ **FIXED 2026-06-11** — the stale detector now also flags states that fail to parse as a finite float, routing them through the same hold/breaker-fallback path as `unavailable`.

11. ~~**Solar/battery sensors held at last EMA forever**~~ **FIXED 2026-06-11** — `_stale_guard` bounds the EMA hold: after `INPUT_STALE_TIMEOUT` (60 s) a dead solar sensor falls back to 0 W, a dead battery power sensor to None (dropping battery-power-derived terms from the pools), and a dead inverter output phase to None.

12. **Series household clamps to 0 under sensor lag** — `calculations/utils.py` `compute_household_per_phase` (~line 56-58): `max(0, inverter_output − charger_draws)` reads household 0 when the OCPP draw rises faster than the inverter-output sensor updates, transiently overstating per-phase inverter headroom by the full household. No hold-last guard, unlike grid CT staleness.

17. **Battery-backed binary plugs silently top up from the grid** *(found 2026-06-11 while writing regression tests)* — an above-min/above-target binary plug is SOC-gated by design (battery = stored-solar buffer) and is not bounded by the solar pool, so when the inverter is saturated the plug stays on and the difference is imported from the grid (observed: Solar Only plug on, inverter at its cap, grid importing 3.2 A). Contradicts the "none of them use the grid" intent in `test_battery_plug.yaml`. Needs a design decision: shed the plug when the inverter can't cover it, or accept grid top-up as the documented behavior.

13. **Hub sensor inverter headroom ignores wiring topology** — `hub_calculation.py` `_build_hub_result` (~line 1035-1043): `current_inverter_output = max(0, solar + battery_power)` is the series model; for parallel (AC-coupled) sites with the battery charging, output is understated by the charge power and `total_site_available` is inflated. (Display-layer sensors only; the engine pools have their own path.)

### Low confidence / design questions

14. **Smoothing snaps to target when restarting from 0** — `control/smoothing.py` (~line 37-41): `_rate_limited_current == 0` resets all smoothing state to `raw_allocated`, so 0 → 32 A in one cycle, bypassing `RAMP_UP_RATE`. May be intended fast-start; contradicts the ramp design and leans on the compliance "ramping" skip.

15. **`_read_inverter_output` takes `abs()`** — `hub_calculation.py` (~line 126): a hybrid inverter importing AC to charge its battery (negative reading) is counted as positive output; series derived solar then fabricates production (|import| + |charge|). Only matters if users feed signed sensors.

16. **Unguarded `/ site.voltage`** — `target_calculator.py` divides by voltage in `_calculate_grid_limit` / `_calculate_inverter_limit` without the `voltage > 0` guard `_calculate_excess_available` has. ZeroDivisionError if a voltage entity ever reads 0.

All other issues have been moved to detailed TODOs in `TODO.md`.
