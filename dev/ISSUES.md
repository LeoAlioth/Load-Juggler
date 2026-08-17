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

## Bugs from code review (2026-08-17)

Full-codebase review (four parallel reviewers: engine/calculations, config flow/setup, entities/control, architecture/tests). Verified against source by the reviewers. Structural refactorings arising from this review live in `TODO.md`.

**Status (2026-08-17, same session): #18–30, #35, #36, #39, #42 FIXED** by delegated fix agents, plus the `hub_calculation.py` items of #43 (unused `voltage` param, dead `raw_solar`, `_read_grid_phases` return signature) — see the working-tree diff. All 161 calc scenarios pass on the combined tree; the Docker pytest tier was NOT run (Docker daemon unavailable this session) and should be run before release, particularly `test_config_flow*.py`, `test_init.py`, `test_sensor_update.py`. **Still open: #31–34, #37, #38, #40, #41, the remaining parts of #43 (`calculations/context.py`, `_create_entry_and_seed_options`), #44, #45.**

### High confidence

18. **Device sensor is double-driven: platform polling on top of its coordinator** — `LoadJugglerDeviceSensor` (`sensor.py` ~187-247) is not a `CoordinatorEntity` and doesn't set `_attr_should_poll = False`, so HA polls `async_update()` every `SCAN_INTERVAL` (10 s) in addition to the private coordinator (default 2 s). The engine + EMA/ramp pipeline runs more often than configured, and the two schedulers can interleave around the command-interval gate (gate is checked before `_last_command_time` is written) → duplicate OCPP profiles. Aggravates #8.

19. **Power stations falsely paused by the EVSE minimum-current default** — `entities/load.py` ~453-457 unconditionally re-reads `CONF_EVSE_MINIMUM_CHARGE_CURRENT` after the command-interval gate, clobbering the station-specific floor (`min_power/(V×phases)` ≈ 0.9 A) computed at ~320-342. Station entries don't carry the EVSE key → fallback 6 A. Any permit between the real floor and 6 A hits the pause branch → `limit = 0`; resume-from-zero restarts at ~0.9 A and is immediately re-paused, so a station can never ramp up from a pause unless the permit jumps past 6 A in one cycle. The re-read is redundant even for EVSEs (variable still in scope).

20. **Editing the OCPP Device ID via reconfigure silently does nothing** — the reconfigure/options flows write `CONF_OCPP_DEVICE_ID` to `entry.options` (`config_flow.py` ~3778, ~4299), but every runtime consumer reads `entry.data` directly (`control/ocpp.py` ~107/179, `__init__.py` ~194, unit auto-detect in the flow itself). The edit field exists specifically to fix a wrong devid and has no effect.

21. **Auto-discovered chargers are never controlled** — `_discover_and_notify_chargers` (`__init__.py` ~678-687) stores `device.id` (the HA device-registry UUID, or `None`) as `CONF_OCPP_DEVICE_ID`, while the manual flow scanner deliberately uses the entity base name ("not the internal HA UUID", `config_flow.py` ~3030). Profiles from discovery-added chargers target a nonexistent devid. Same charger added manually works.

22. **`_get_hub_phase_count` collapses to 1 phase post-import on off-grid sites** — `config_flow.py` ~1128-1159 infers phase count only from the hub entry's own grid-CT/inverter keys, but the legacy auto-import blanks those and moves inverter output entities onto inverter child entries. On an off-grid 3-phase site the charger flows then hide the L2/L3 mapping fields and force-write `CONF_CHARGER_L2/L3_PHASE = L1` (~3287, ~3824, ~4342), corrupting the phase mask. Must consult inverter child entries like the engine does.

23. **Plug power-learning "N stable readings" guard is dead code** — `engine/hub_calculation.py` ~690-703: `candidate = charger_rt.get("power_candidate", power_draw)` defaults to the current reading and the stable branch never persists `power_candidate`, so the comparison is reading-vs-itself → always stable. Any 3 readings > 10 W commit to `device_power`, including compressor-start transients (with #8/#18, "3 cycles" can be one scan interval).

24. **Tank power learning not gated on heating** — `engine/hub_calculation.py` ~907-913 contradicts its own comment ("while the element is heating"): any live reading > 10 W (standby electronics, circulation pump) overwrites `device_power`, making `equivalent_current` ≈ 0.1 A and the 2 kW tank a near-free load until the next real heat call.

### Medium confidence

25. **`current_import_total` fallback lacks the total-vs-per-phase clamp** — `engine/hub_calculation.py` ~502-508 copies a possibly phase-summed current onto all three phases (24 A total booked as 72 A); the feedback loop then fabricates export → phantom surplus → over-allocation. The attribute-based path directly above (~478-499) has exactly this clamp; the total path and per-phase-entities path (~444-447) don't.

26. **Per-phase available-current sensors misattribute on non-prefix phase sets** — `engine/hub_calculation.py` ~1668-1688 gates on `i >= num_phases` (a count) instead of `phase_cons[i] is None`; on a B+C-only site, phase C is forced to 0 and nonexistent phase A gets the inverter share. Diagnostics only, but actively wrong.

27. **Plug/tank/station reconfigure merges the whole `entry.data` snapshot into options** — `config_flow.py` ~3454 + ~3892-3966: static keys (`entry_type`, `hub_entry_id`, `name`, migration flags) get copied into `entry.options`, where `get_entry_value`'s options-first precedence permanently shadows future data-side changes/migrations. Hub/EVSE reconfigure steps correctly write only `user_input`.

28. **`CONF_PHASES` is read but never written by any flow** — reset service (`__init__.py` ~225) computes `min_current × voltage × 3` for a 1-phase charger; engine read at `hub_calculation.py` ~375 likewise always yields the default 3.

29. **Two divergent OCPP discovery scanners** — the `__init__.py` scanner passes only 5 keys (per-phase `current_import_l1/l2/l3`, `power_offered`, `power_import` stored as `None` for discovery-added chargers even when the entities exist) and hard-requires `current_offered` (watts-only chargers never discovered), while the flow scanner accepts a `power_offered` fallback. Should be one scanner.

30. **Phase voltage read from `entry.data`, bypassing reconfigure** — `control/ocpp.py` ~125 and `control/compliance.py` ~92 use `entry.data.get(CONF_PHASE_VOLTAGE)` while the hub reconfigure writes options and everything else uses `get_entry_value`. After a 230→240 V change, W-unit profiles and compliance decode diverge from the engine.

31. **Unavailability/staleness detection re-implemented ~5× with drifting predicates** — `_read_entity` (includes `None`/`""`), grid-stale block (~1888, adds non-finite), `_check_entity_availability` (~201/224, omits `None`), station status (~809, omits `""`), `forecast_reader.py` ~138. `_read_grid_phases` (~308) coerces `_UNAVAILABLE` to 0 A and relies on the downstream stale block to repair it — an unrepaired 0 A grid reading grants full breaker headroom. One `is_unavailable(state)` helper should own the predicate.

32. **Entity layer: deprecated sensor API, no `available` handling, `None` → `0.0`** — sensors override `state`/`unit_of_measurement`/`device_class` instead of `native_value`/`_attr_native_*`, smuggle `state_class` through `extra_state_attributes` (inconsistently — some W/A sensors get long-term statistics, some don't), no hass.data-fed sensor implements `available` (values freeze silently when the producer stops), and `LoadJugglerHubSensor.state` maps `None` → `0.0` (Site Remaining Power reads 0 W on failure instead of unknown). Mechanical but wide fix → tracked as a TODO refactor.

33. **Hub-sensor staleness constant races the configurable cadence** — `entities/hub.py` ~15/59-106: fixed `_HUB_DATA_STALE_SECONDS = 30` vs user-configurable `site_update_frequency`; at ≥ 30 s the hub sensor runs a second, parallel engine calc via the production `MockSensor` shim, and the two writers alternate different `hub_data` dict shapes. Threshold should derive from the configured frequency (shim removal is part of the coordinator refactor in TODO).

34. **Migration-flag consume triggers an entry reload from inside entity setup** — `select.py` ~115-123 calls `async_update_entry` in `async_added_to_hass` to clear `MIGRATE_PLUG_SOLAR_ONLY_FLAG`, firing the update listener → reload potentially while the entry is still `SETUP_IN_PROGRESS` (one-time `OperationNotAllowed` race after migration). Consume in `async_migrate_entry` instead.

35. **Translation drift** — `en.json` missing runtime error keys (`config/options.error.invalid_unit`, options `invalid_current`, `min_exceeds_max`, `battery_required_no_cts`, `no_members_selected`; `options.step.group` absent from `strings.json`); `strings.json` a release behind `en.json` (missing group steps, `set_operating_mode` service, `operating_mode` select; carries dead `config.step.device_type` and nonexistent `set_charging_mode`); `services.yaml` `set_operating_mode` dropdown omits tank modes `Freeze Protection`/`Normal` that the schema accepts; abort reason `already_configured` untranslated.

36. **Declared HA minimum is wrong** — `hacs.json` says `"homeassistant": "2023.8.0"`, but the options flow relies on auto-provided `self.config_entry` (≥ 2024.11) and migration uses `async_update_entry(..., minor_version=...)` (≥ 2024.1). Installing on the declared minimum crashes. (`"country": "US"` also filters HACS availability — probably unintended.)

### Low confidence / minor

37. **Pause/grace threshold ignores the runtime Min Current slider** — `entities/load.py` ~344-348 reads only the static config value while the engine floors permits with `charger_rt["min_current"]` (`hub_calculation.py` ~372); raising the slider changes allocation but not the pause decision.

38. **Number restore doesn't clamp to current bounds; min/max sliders don't cross-validate** — `entities/mixins.py` ~98-107, `number.py` ~168-223: a restored value can exceed bounds after a reconfigure lowers the max, and Min Current can be set above Max Current.

39. **Missing OCPP device id spams errors every cycle** — `control/ocpp.py` ~179-184 returns without setting `_last_command_time`, so the error path re-runs every site cycle (2 s) instead of every command interval.

40. **Effective-priority attributes wrong for multi-hub installs** — `entities/load_sensors.py` ~107-110: `total_devices` counts `charger_ranks` across all hubs, and removed chargers are never pruned.

41. **Binary Excess loads larger than the hysteresis band oscillate** *(design question)* — `target_calculator.py` ~778-785: a binary Excess load engages when the pool is merely > 0; a 3 kW plug on a +200 W margin overshoots `EXCESS_EXPORT_HYSTERESIS` (500 W) → ~10-20 s relay cycling grid-tied. Partly intended as the off-grid "probe".

42. **Inversion auto-detect has no minimum `|delta_grid|`** — `engine/auto_detect.py` ~67-71: only `delta_draw` has a 1 A floor; noise-level grid deltas decide sample signs. Phase-mapping has `_PM_MIN_DELTA_A` for both sides; inversion doesn't. Small chance of a false one-shot "CTs inverted" notification.

43. **Dead code** — `calculations/context.py` (140 lines, zero callers, reads live HA state inside the "pure" package, defaults phase mask to "A" against pitfall #5 — delete); `_read_grid_phases` consumption/export returns discarded by the only caller (`hub_calculation.py` ~313/1857); `_apply_feedback_loop`'s `voltage` param unused (~1081); `raw_solar = None` never reassigned (~1952, debug always prints "n/a"); `_create_entry_and_seed_options` trivial pass-through (`config_flow.py` ~1857); dead `config.step.device_type` in `strings.json`; `run_tests.py` default `yaml_file` points at a nonexistent file.

44. **Repo hygiene** — zero-byte `mypy.py` and `requirements.txt` at root (CI installs the empty one); 1.45 MB `icon.png` tracked twice, the in-component copy ships in every HACS zip but HA never loads it (see issue #1); tracked `dev/tests/debug.log` not covered by `.gitignore`; `manifest.json` docs/issues URLs still point at the old GitHub repo; `test_validation.py`/`test_config_flow_validation.py` are near-identical and test a local copy of `validate_charger_settings` instead of the real one (tautological — delete one, import the real function in the survivor).

45. **`dev/tests/test_auto_detect.py` cannot run under the documented invocation** *(found 2026-08-17 by the engine fix agent; pre-existing)* — (a) its bootstrap importlib loader omits `const/power_station.py`, so importing `const/modes.py` raises `ModuleNotFoundError`; (b) `engine/auto_detect.py` uses PEP 604 unions (`dict | None`) in runtime signatures, which need Python ≥ 3.10, while the system `python3` is 3.9.6. All 26 tests verified passing via a scratch harness on Python 3.12. Fix the loader (or dedupe it with `run_tests.py`'s — see the test-hygiene TODO) and document the required Python version.
