# TODO

## Completed

1. - [x] **PhaseValues → Optional[float]** — Fields `a`, `b`, `c` changed from `float = 0.0` to `float | None = None`. None = phase doesn't exist, 0.0 = exists with no load.
2. - [x] **PhaseValues helpers** — Added `active_count`, `active_mask` properties. Updated `__neg__`, `clamp_min`, `__repr__` to preserve None.
3. - [x] **Available current feature** — Added `available_current` field to `ChargerContext`, `_calculate_available_current()` in target_calculator, idle charger display in sensors.
4. - [x] **1-phase grid limit bug** — Fixed `_calculate_grid_limit()` zeroing non-existent phases instead of giving them full breaker rating.
5. - [x] **1-phase available current test scenarios** — Added 3 scenarios: `1ph-1c-idle-standard-no-solar`, `1ph-2c-one-active-one-idle-standard`, `1ph-2c-one-active-one-idle-tight`.
6. - [x] **SiteContext.num_phases → derived property** — Removed stored `num_phases: int = 3` field, replaced with `@property` returning `self.consumption.active_count`.
7. - [x] **target_calculator.py: replace num_phases checks** — Changed `site.num_phases >= 2` to `site.consumption.b is not None`, used `active_count` instead of `num_phases` in inverter/solar/excess/eco calculations.
8. - [x] **run_tests.py: infer phases from YAML** — Read `phase_X_consumption` as None when absent (not default 0). Updated `apply_charging_feedback` to use `site.num_phases` property.
9. - [x] **YAML scenarios: remove num_phases** — Removed `num_phases:` from all 5 scenario files. Removed `phase_b/c_consumption: 0.0` from 1-phase scenarios.
10. - [x] **dynamic_ocpp_evse.py: pass None for unconfigured phases** — When phase entity not configured, pass None instead of 0 for consumption/export values. Removed explicit `num_phases` calculation.
11. - [x] **Run all tests** — 61/61 passing after refactor (2026-02-14).
12. - [x] **Dedicated Solar Power Entity** — Added `CONF_SOLAR_PRODUCTION_ENTITY_ID` to const.py, config_flow.py (hub grid schema + auto-detection), dynamic_ocpp_evse.py (reads direct solar entity when configured). 3 test scenarios in `test_scenarios_solar_entity.yaml`. 64/64 passing.
13. - [x] **Smart Plug / Relay Support** — New `device_type` field (`"evse"` or `"plug"`) across the stack:
    - `const.py`: Added `CONF_DEVICE_TYPE`, `DEVICE_TYPE_EVSE/PLUG`, `CONF_PLUG_SWITCH_ENTITY_ID`, `CONF_PLUG_POWER_RATING`, `CONF_PLUG_POWER_MONITOR_ENTITY_ID`, `CONF_CONNECTED_TO_PHASE`.
    - `models.py`: Added `device_type: str = "evse"` to `ChargerContext`.
    - `config_flow.py`: Device type selection step, plug config/reconfigure/options steps with `_plug_schema()`.
    - `dynamic_ocpp_evse.py`: Plug charger context building (power_rating → equivalent current, switch state → connector_status).
    - `sensor.py`: Branched update on device_type — plugs use `switch.turn_on/off`, EVSEs use OCPP profiles.
    - `run_tests.py`: `device_type` + `power_rating` support in YAML scenarios.
    - 6 test scenarios in `test_scenarios_plugs.yaml`. 70/70 passing.

14. - [x] **Charge rate unit detection via OCPP** — Replaced unreliable sensor UoM detection with OCPP `GetConfiguration` query for `ChargingScheduleAllowedChargingRateUnit`. Config flow detects and pre-fills the dropdown; if detection fails the field is left empty for the user to choose. Detection also available in reconfigure/options flows. Sensor.py has cached OCPP fallback for legacy "auto" entries. (fixes ISSUES.md #2)
15. - [x] **Distribution mode string mismatch** — `target_calculator.py` matched `"optimized"` / `"strict"` exactly, but HA select entity stores `"Sequential - Optimized"` / `"Sequential - Strict"`. Changed to substring matching. (fixes ISSUES.md #3)
16. - [x] **Reset service hardcoded 3 phases** — `__init__.py` used `voltage * 3` for Watts conversion regardless of charger phases. Now reads `CONF_PHASES` from charger config entry. (fixes ISSUES.md #4)
17. - [x] **PhaseConstraints.copy() robustness** — Replaced manual field-by-field copy with `dataclasses.replace(self)`.
18. - [x] **Rename misleading variable/function names** — Fixed typos and clarified names across the codebase:
    - `CONF_CHARING_MODE` → `CONF_CHARGING_MODE` (typo)
    - `CONF_CHARGIN_MODE_ENTITY_ID` → `CONF_CHARGING_MODE_ENTITY_ID` (typo)
    - `CONF_AVAILABLE_CURRENT` → `CONF_TOTAL_ALLOCATED_CURRENT` (stores sum of allocations, not available)
    - `calculate_available_current_for_hub()` → `run_hub_calculation()` (does much more than calculate available current)
    - `_battery_power_entities()` → `_battery_and_power_entities()` (returns battery AND power entities)
    - `_current_power_entities()` → `_get_current_and_power_entities()` (returns current AND power sensors)
    - `_calculate_solar_available()` → `_calculate_solar_surplus()` (calculates surplus after consumption)
    - `_calculate_available_current()` → `_set_available_current_for_chargers()` (mutates charger objects)

19. - [x] **Smart Plug UX improvements** — Five changes to improve the plug user experience:
    - Device info model: Returns "Smart Plug" instead of "EV Charger" for plug devices across all entity files (`sensor.py`, `number.py`, `switch.py`, `button.py`).
    - Current rounding: All current (A) values (`_allocated_current`, `_state`) rounded to 1 decimal in `sensor.py`.
    - Device Power slider: New `PlugDevicePowerSlider` entity in `number.py` (W, 100-10000, step 100). Replaces min/max current sliders for plugs.
    - Engine reads slider: `dynamic_ocpp_evse.py` reads power from `number.{entity_id}_device_power` entity, falls back to `CONF_PLUG_POWER_RATING`.
    - Power monitor auto-adjust: When plug has power monitoring and is actively drawing, rolling average of last 5 readings updates both the engine calculation and the Device Power slider.

20. - [x] **Missing translations** — Added missing translation entries in `en.json` and `sl.json`:
    - `solar_production_entity_id` in `reconfigure_hub_grid` and `options.hub_grid` sections.
    - New `device_type`, `plug_config`, `reconfigure_plug` steps in `config.step`.
    - New `plug` step in `options.step`.

21. - [x] **Current rate limiting** — Re-added ramp rate limiting (lost in v2 refactor) in `sensor.py` EVSE branch:
    - Ramp up: max 0.1 A/s (1.5A per cycle at 15s). Ramp down: max 0.2 A/s (3A per cycle at 15s).
    - Only applies when both previous and current limits > 0 (pause→resume is instant).
    - Constants `RAMP_UP_RATE`, `RAMP_DOWN_RATE` in `const.py`.
    - 3 integration tests in `test_sensor_update.py`.

22. - [x] **Auto-reset for non-compliant chargers** — Detects when EVSE ignores charging profiles and automatically triggers reset:
    - `_check_profile_compliance()` in `sensor.py` compares `current_offered` entity against last commanded limit.
    - Tolerance = `RAMP_DOWN_RATE * update_frequency` (3A at 15s default) — dynamically adapts.
    - Triggers `reset_ocpp_evse` after 5 consecutive mismatched cycles. 120s cooldown after reset.
    - Guards: EVSE only, car plugged in, limit > 0, dynamic control on.
    - Constants `AUTO_RESET_MISMATCH_THRESHOLD`, `AUTO_RESET_COOLDOWN_SECONDS` in `const.py`.
    - 5 integration tests in `test_sensor_update.py`. 58/58 passing.

23. - [x] **Round current values in calculation engine** — Added `round(..., 1)` to all `allocated_current` and `available_current` assignments in `target_calculator.py` (priority, shared, strict, optimized distributions + `_set_available_current_for_chargers`). Values are now rounded to 1 decimal at the source, not just at display time in `sensor.py`. 70/70 + 26/26 tests passing.

24. - [x] **Total EVSE Power shows 0** — `dynamic_ocpp_evse.py` read per-phase currents from entity attributes (`l1_current`, `l2_current`, `l3_current`) that don't exist on most OCPP integrations. Fixed: try per-phase attributes first (multiple naming conventions via `_read_phase_attr` helper), fall back to entity state value distributed across configured phases.

25. - [x] **Inverter configuration in config flow** — Added `CONF_INVERTER_MAX_POWER`, `CONF_INVERTER_MAX_POWER_PER_PHASE`, `CONF_INVERTER_SUPPORTS_ASYMMETRIC` to `const.py`. New `hub_inverter` step in config_flow.py (initial setup, reconfigure, options flow). `dynamic_ocpp_evse.py` reads from config instead of hardcoded `False`. Translations added for en.json and sl.json. 70/70 + 50/50 tests passing.

26. - [x] **Grid consumption feedback loop** — Grid CTs measure total site current INCLUDING charger draws. The engine double-counted charger power as both "consumption" and "charger demand". Fixed: `dynamic_ocpp_evse.py` now subtracts each charger's l1/l2/l3_current from site.consumption before calling the calculation engine. Hub sensor display values still show raw grid readings. 2 integration tests added.

27. - [x] **Charge pause UX improvements** — Three changes:
    - `pause_remaining_seconds` attribute added to charger sensor `extra_state_attributes` (computed from pause start time and configured duration).
    - Mode change cancellation: pause is cancelled immediately when charging_mode or distribution_mode changes (tracks previous values via `_prev_charging_mode`/`_prev_distribution_mode`).
    - 3 integration tests in `test_sensor_update.py`. 70/70 + 55/55 tests passing.

28. - [x] **battery_soc_target None crash** — `TypeError: '<' not supported between 'float' and 'NoneType'` when `battery_soc_target` entity not configured. Added `site.battery_soc_target is not None` guard to all 4 comparison sites in `target_calculator.py`. (fixes ISSUES.md #5)

29. - [x] **Charge rate unit case sensitivity** — Chargers returning lowercase `"power"` instead of `"Power"` caused `Unrecognised ChargingScheduleAllowedChargingRateUnit` warning. Fixed `_detect_charge_rate_unit()` in `config_flow.py` to normalize to lowercase before matching. (fixes ISSUES.md #6)

30. - [x] **Eco mode fake solar surplus at night (partial fix)** — Feedback loop fix (item 26) created a mismatch: `solar_production_total` was derived from ORIGINAL consumption, but the engine used ADJUSTED consumption (after charger subtraction). This produced a fake solar surplus equal to the charger's own draw, inflating Eco mode targets (e.g. 11.2A instead of 6A at night). First fix (recalculating solar_production_total after feedback) was insufficient — see item 33 for the full fix.

31. - [x] **Dual-frequency update loop** — Coordinator now runs at `site_update_frequency` (hub-level, default 5s). Calculation + hub_data refresh happen every cycle. OCPP commands and plug switches are throttled to `update_frequency` (charger-level, default 15s) via `_last_command_time` monotonic clock. Eliminated temp sensor pattern — coordinator now uses the persistent sensor instance. New `CONF_SITE_UPDATE_FREQUENCY` constant, config flow field, translations (en + sl). 1 integration test. 70/70 + 57/57 tests passing.

32. - [x] **Derived solar production formula fundamentally broken** — The formula `solar_production_total = (consumption + export) * voltage` treats total grid import as solar production. At night with 36A grid import and 0A export, it computed "solar" = 8367W, creating fake surplus on phases with below-average consumption. Root cause: with only a grid CT, solar production CANNOT be determined — only the net export (surplus) is observable. Fix:
    - `models.py`: Added `solar_is_derived: bool` flag to `SiteContext`.
    - `dynamic_ocpp_evse.py`: When no dedicated solar entity, set `solar_production_total = total_export_power` and `solar_is_derived = True`.
    - `target_calculator.py`: New derived path in `_calculate_solar_surplus()` — uses per-phase export current directly as surplus (symmetric) or total export as pool (asymmetric). Skips battery adjustment since grid readings already reflect battery effects. Also skips battery in `_calculate_inverter_limit()` when derived.
    - Feedback loop: Now reconstructs raw grid current and re-splits into consumption/export after subtracting charger draws, correctly revealing hidden export.
    - Fixed `_last_command_time` init from `0` to `-inf` (first throttle check always passes regardless of process uptime).
    - 70/70 + 57/57 tests passing. (fixes ISSUES.md #7 and #8)

33. - [x] **Multi-cycle test simulation** — Revamped `run_tests.py` to run every scenario through a 30-cycle simulation (5 ramp-up + 20 warmup + 5 stability check) instead of single-shot calculation. Added ramp rate limiting simulation (1.5 A/cycle up, 3.0 A/cycle down, matching `sensor.py` constants). Cold start from zero with linear interpolation to target values. Stability check verifies convergence over last 5 cycles. Removed old `apply_charging_feedback`, `detect_oscillation`, `run_scenario_with_iterations` and the single-shot vs multi-iteration split. `iterations` YAML field now ignored. 70/70 + 57/57 tests passing.

34. - [x] **Test trace output** — Added `--trace` CLI flag to `run_tests.py`. When enabled, each cycle prints per-cycle state in the format: `grid=(A/B/C) inverter=(A/B/C solar=XW bat=XW) house=(A/B/C) ch_sum=(A/B/C) bat=(A/B/C soc=X%)`. Grid values are signed (+import/-export). Inverter shows per-phase current plus solar and battery power in watts. Also added `solar_is_derived` to the hub state debug log line in `dynamic_ocpp_evse.py` for future production debugging.

35. - [x] **Eco mode: inactive chargers inflating minimums** — `_determine_target_power()` in `target_calculator.py` summed `min_current * phases` for ALL chargers, including inactive ones (Available/Unknown/Unavailable). An inactive smart plug with ~15.6A equivalent current inflated `sum_minimums_per_phase` from 6A to 11.2A, causing the active EVSE to charge at 8.8A instead of 6A at night. Fixed: filter to active chargers only. (fixes ISSUES.md #9)

36. - [x] **Self-consumption battery model in test simulation** — Replaced the old `compute_battery_grid_effect()` (which always charged/discharged at max power based on SOC vs target) with a realistic self-consumption model in `simulate_grid_ct()`. Battery now dynamically responds to actual demand: deficit → discharge min(deficit, max_discharge) if SOC > min_soc; surplus → charge min(surplus, max_charge) if SOC < 97%. Grid CT shows near-zero for battery systems (matching real inverter behavior). 67/67 tests passing.

37. - [x] **Battery-aware derived solar mode** — Engine now uses `site.battery_power` in derived mode to recover surplus hidden by self-consumption. Two adjustments in `_calculate_solar_surplus()`: (1) battery charge-back: when battery is charging (battery_power < 0), adds absorbed solar power back to surplus; (2) discharge potential: when SOC > target, adds unused discharge capacity. Same principle in `_calculate_inverter_limit()` for Standard mode. Test sim sets `battery_power` from `simulate_grid_ct()`, all scenarios use `solar_is_derived=True`, removed step 5b consumption restoration hack. 3 asymmetric inverter scenarios updated for derived mode behavior (per-phase consumption not available in derived mode → slightly different allocation). 67/67 tests passing.

38. - [x] **Test trace scenario parameters** — Added `print_scenario_params()` to display scenario configuration (site, consumption, battery, inverter, chargers, expected values) at the start of each simulation in verbose/trace mode. Fixed Windows CP-1252 Unicode encoding in `TeeOutput`. Also added scenario name/description headers in batch run output.

39. - [x] **Inverter output cap for battery discharge** — Fixed inverter over-allocation in derived mode. Battery discharge goes through the inverter; when solar already maxes out inverter capacity, no room for additional discharge. Changes: (1) Engine: `_calculate_solar_surplus()` derived path now limits `discharge_potential` by `inverter_headroom = inverter_max - estimated_solar` and caps final pool at `inverter_max - household`. (2) Model: Added `household_consumption_total` to `SiteContext` — simulation sets true household, production code estimates from pre-feedback consumption. (3) Simulation: `simulate_grid_ct()` limits battery discharge by `max(0, inverter_max - solar_total)`. (4) Production: `dynamic_ocpp_evse.py` sets `household_consumption_total = max(0, consumption - charger_draws)` before feedback. Scenario `total-limit-hit` corrected from 16.7A to 15.7A; `per-phase-limit-hit` updated from 14.3A to 16.7A (correct pool allocation). 67/67 tests passing.

40. - [x] **Remove dedicated solar entity & simplify site sensors** — Removed the dual code path (derived vs dedicated solar entity) across the entire stack. All solar surplus is now derived from grid CT export current + battery power. Changes:
    - `const.py`: Removed `CONF_SOLAR_PRODUCTION_ENTITY_ID`.
    - `models.py`: Removed `solar_is_derived` flag from `SiteContext`.
    - `target_calculator.py`: Removed non-derived paths from `_calculate_inverter_limit()` and `_calculate_solar_surplus()`. Only the derived (grid CT-based) path remains. Asymmetric inverter logic preserved intact.
    - `dynamic_ocpp_evse.py`: Removed solar entity reading, always derives `solar_production_total` from export. Removed `solar_is_derived` guard in feedback loop. `net_site_consumption` now uses raw grid values (positive=import, negative=export). Removed `solar_surplus_power`/`solar_surplus_current` from result dict. Removed `household_consumption_total` assignment.
    - `config_flow.py`: Removed solar entity from grid schema, normalization, and auto-detection.
    - `sensor.py`: Removed Solar Surplus Power/Current sensor definitions. Updated Net Site Consumption icon to `mdi:home-lightning-bolt-outline`.
    - `translations`: Removed `solar_production_entity_id` from en.json and sl.json.
    - `run_tests.py`: Removed `solar_is_derived` from SiteContext construction, feedback adjustment, and trace output. Removed `household_consumption_total` assignment.
    - `test_sensor_update.py`: Removed solar surplus assertions.
    - Deleted `test_scenarios_solar_entity.yaml.disabled`.

41. - [x] **Update trace test output format** — Changed battery display condition from `bat_pp != 0` to `site.battery_soc is not None`. Battery watts now always shown in inverter section when battery is configured (even when 0W). Trailing `bat=(...)` section also shown only when battery is configured.

42. - [x] **Restore dedicated solar entity for inverter limit enforcement** — Commit 481ca84 removed the solar entity option (item 40), but derived-only mode can't enforce inverter limits when household loads are invisible to the grid CT (e.g. 6kW inverter, CT=0A, battery charging 4kW, house=2kW → engine estimates household=0, overallocates inverter). Restored with minimal branching approach (no dual code paths):
    - `const.py`: Re-added `CONF_SOLAR_PRODUCTION_ENTITY_ID`.
    - `models.py`: Re-added `solar_is_derived: bool = True` and `household_consumption_total: float | None = None` to `SiteContext`.
    - `target_calculator.py`: Two small changes in `_calculate_solar_surplus()` — inverter headroom check and final inverter limit check use accurate `household_consumption_total` when available (from solar entity energy balance), otherwise fall back to derived estimates. `_calculate_inverter_limit()` battery branch: derived mode uses remaining discharge capacity, dedicated mode uses full max_discharge.
    - `dynamic_ocpp_evse.py`: Reads solar entity when configured, computes `household_consumption_total = solar + battery_power - export` via energy balance after feedback. Only recalculates `solar_production_total` when `solar_is_derived`. Re-added `solar_surplus_power`/`solar_surplus_current` to result dict.
    - `config_flow.py`: Re-added solar entity selector to grid schema, normalization list, and auto-detection regex.
    - `sensor.py`: Re-added Solar Surplus Power/Current hub sensor definitions and hub_data propagation.
    - `translations`: Re-added `solar_production_entity_id` labels in en.json and sl.json (hub_grid, reconfigure_hub_grid, options hub_grid).
    - `run_tests.py`: Support `solar_production_direct: true` YAML flag, conditional feedback (derived vs dedicated), household_consumption_total computation.
    - 4 new test scenarios in `test_scenarios_solar_entity.yaml` including the core inverter limit enforcement scenario. 68/71 tests passing (3 pre-existing failures in asymmetric inverter tests).

43. - [x] **Config flow restructuring + charger phase mapping** — Two changes:
    - **Hub flow reorder**: Grid → Inverter → Battery (was Grid → Battery → Inverter). Solar entity selector moved from grid step to battery step (relevant only with batteries). Entry creation logic moved to battery step (final step). Applied to all 3 flows: initial setup, reconfigure, and options.
    - **Charger flow split into 3 steps**: (1) `charger_info` — editable name/entity_id (pre-filled from OCPP discovery) + priority. (2) `charger_current` — min/max current limits + L1/L2/L3 → A/B/C phase mapping dropdowns. (3) `charger_timing` — charge rate unit, profile validity mode, update frequency, timeouts, stack level. Applied to all 3 flows.
    - **Phase mapping feature**: New `CONF_CHARGER_L1_PHASE`, `CONF_CHARGER_L2_PHASE`, `CONF_CHARGER_L3_PHASE` constants. `ChargerContext` gets `l1_phase`/`l2_phase`/`l3_phase` fields with `get_site_phase_draw()` helper. `active_phases_mask` now derived from mapping. Feedback loop in `dynamic_ocpp_evse.py` uses phase mapping instead of assuming L1=A. Test simulation (`run_tests.py`) updated with phase mapping support. 3 phase mapping test scenarios.
    - Files: `const.py`, `models.py`, `config_flow.py`, `dynamic_ocpp_evse.py`, `en.json`, `sl.json`, `run_tests.py`, `test_config_flow.py`, `test_config_flow_e2e.py`, `conftest.py`.

## In Progress

## Backlog

## Other

1. - [ ] **Icon submission** — Submit `icon.png` to [HA brands repo](https://github.com/home-assistant/brands) (see dev/ISSUES.md)
