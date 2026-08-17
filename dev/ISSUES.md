# Load Juggler — Open Issues

1. **Icon not shown in HA/HACS** — HA does not load a custom component's own icon file. The icon must be submitted as a PR to the [Home Assistant brands repo](https://github.com/home-assistant/brands). Repo root now has brands-ready files: `icon.png` (256×256, 26 KB) and `icon@2x.png` (512×512, 81 KB) — resized 2026-08-17 from the original 1.45 MB 1024×1024 (the duplicate copy inside `custom_components/` that shipped in every HACS zip was removed the same day).

## Candidate bugs from code review (2026-06-11)

Found during the off-grid inverter-limit fix session. Ordered by confidence; spot-checked against the source. Resolved items have been removed — see `git log` and `RELEASE_NOTES.md` for what was fixed; the rest need a failing scenario before fixing.

### Medium confidence

7. **Shared distribution starves cross-phase chargers** — `target_calculator.py` `_distribute_per_phase_shared` (~line 927-952): pass 2 takes `min_available` across ALL wanting chargers' masks and `break`s at ≤ 0, so one charger on an exhausted phase pins every other charger at min even with headroom free on their phases. Same loop scales every charger's increment by solar scarcity, including Standard/full-power chargers that are grid-backed.

9. **Watts-profile compliance fix needs field validation** — the 2026-06-14 fix (decode with `_car_active_phases or _phases or 1`) still wants validation against a live Watts-reporting charger to confirm `power_offered` echoes the commanded total power as assumed.

12. **Series household clamps to 0 under sensor lag** — `calculations/utils.py` `compute_household_per_phase` (~line 56-58): `max(0, inverter_output − charger_draws)` reads household 0 when the OCPP draw rises faster than the inverter-output sensor updates, transiently overstating per-phase inverter headroom by the full household. No hold-last guard, unlike grid CT staleness.

17. **Battery-backed binary plugs silently top up from the grid** *(found 2026-06-11 while writing regression tests)* — an above-min/above-target binary plug is SOC-gated by design (battery = stored-solar buffer) and is not bounded by the solar pool, so when the inverter is saturated the plug stays on and the difference is imported from the grid (observed: Solar Only plug on, inverter at its cap, grid importing 3.2 A). Contradicts the "none of them use the grid" intent in `test_battery_plug.yaml`. Needs a design decision: shed the plug when the inverter can't cover it, or accept grid top-up as the documented behavior.

13. **Hub sensor inverter headroom ignores wiring topology** — `hub_calculation.py` `_build_hub_result`: `current_inverter_output = max(0, solar + battery_power)` is the series model; for parallel (AC-coupled) sites with the battery charging, output is understated by the charge power and `total_site_available` is inflated. (Display-layer sensors only; the engine pools have their own path.)

### Low confidence / design questions

14. **Smoothing snaps to target when restarting from 0** — `control/smoothing.py` (~line 37-41): `_rate_limited_current == 0` resets all smoothing state to `raw_allocated`, so 0 → 32 A in one cycle, bypassing `RAMP_UP_RATE`. May be intended fast-start; contradicts the ramp design and leans on the compliance "ramping" skip.

15. **`_read_inverter_output` takes `abs()`** — `hub_calculation.py` (~line 154): a hybrid inverter importing AC to charge its battery (negative reading) is counted as positive output; series derived solar then fabricates production (|import| + |charge|). Only matters if users feed signed sensors.

## Remaining from the code review (2026-08-17)

Full-codebase review (four parallel reviewers: engine/calculations, config flow/setup, entities/control, architecture/tests). 24 of the 29 findings (#18–30, #33–40, #42, #45, #46, most of #43/#44) were fixed the same day — see `RELEASE_NOTES.md` (2.0.6) for the user-facing ones and `git log` for the rest. Structural refactorings live in `TODO.md`. Still open:

31. **Unavailability/staleness detection re-implemented ~5× with drifting predicates** — `_read_entity` (includes `None`/`""`), the grid-stale block (adds non-finite), `_check_entity_availability` (omits `None`), station status (omits `""`), `forecast_reader.py`. `_read_grid_phases` coerces `_UNAVAILABLE` to 0 A and relies on the downstream stale block to repair it — an unrepaired 0 A grid reading grants full breaker headroom. One `is_unavailable(state)` helper should own the predicate (folds into the hub_calculation decomposition TODO).

32. **Entity layer: deprecated sensor API, no `available` handling, `None` → `0.0`** — sensors override `state`/`unit_of_measurement`/`device_class` instead of `native_value`/`_attr_native_*`, smuggle `state_class` through `extra_state_attributes` (inconsistently — some W/A sensors get long-term statistics, some don't), no hass.data-fed sensor implements `available` (values freeze silently when the producer stops), and `LoadJugglerHubSensor.state` maps `None` → `0.0` (Site Remaining Power reads 0 W on failure instead of unknown). Mechanical but wide fix → tracked as the entity-modernization TODO.

41. **Binary Excess loads larger than the hysteresis band oscillate** *(design question)* — `target_calculator.py` ~778-785: a binary Excess load engages when the pool is merely > 0; a 3 kW plug on a +200 W margin overshoots `EXCESS_EXPORT_HYSTERESIS` (500 W) → ~10-20 s relay cycling grid-tied. Partly intended as the off-grid "probe". Same family as #17 — decide together.

43. **Dead code, remainder** — `run_tests.py` default `yaml_file` points at a nonexistent file (harmless; the CLI always passes the scenarios dir).

(#44 closed 2026-08-17: the `manifest.json` GitHub URLs are **intentional** — the Gitea is private and the GitHub-synced mirror is the public face; the icon was resized for the brands submission, see issue #1.)
