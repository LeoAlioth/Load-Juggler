# TODO

## Completed

1. - [x] PhaseValues → Optional[float] (None = phase doesn't exist)
2. - [x] PhaseValues helpers (active_count, active_mask, None-aware ops)
3. - [x] Available current feature (idle charger display)
4. - [x] 1-phase grid limit bug fix
5. - [x] 1-phase available current test scenarios
6. - [x] SiteContext.num_phases → derived property
7. - [x] target_calculator.py: replace num_phases checks with consumption-based
8. - [x] run_tests.py: infer phases from YAML
9. - [x] YAML scenarios: remove num_phases field
10. - [x] dynamic_ocpp_evse.py: pass None for unconfigured phases
11. - [x] All tests passing after refactor (61/61, 2026-02-14)
12. - [x] Dedicated Solar Power Entity
13. - [x] Smart Plug / Relay Support (device_type evse/plug)
14. - [x] Charge rate unit detection via OCPP
15. - [x] Distribution mode string mismatch
16. - [x] Reset service hardcoded 3 phases
17. - [x] PhaseConstraints.copy() → dataclasses.replace()
18. - [x] Rename misleading variable/function names
19. - [x] Smart Plug UX improvements (device info, rounding, power slider, auto-adjust)
20. - [x] Missing translations (en.json + sl.json)
21. - [x] Current rate limiting (ramp up 0.1A/s, down 0.2A/s)
22. - [x] Auto-reset for non-compliant chargers
23. - [x] Round current values in calculation engine
24. - [x] Total EVSE Power shows 0 (per-phase attribute reading fix)
25. - [x] Inverter configuration in config flow
26. - [x] Grid consumption feedback loop (subtract charger draws from CT)
27. - [x] Charge pause UX improvements (remaining seconds, mode change cancel)
28. - [x] battery_soc_target None crash
29. - [x] Charge rate unit case sensitivity
30. - [x] Eco mode fake solar surplus at night (partial — see #32)
31. - [x] Dual-frequency update loop (site 5s, charger 15s)
32. - [x] Derived solar production formula fix (CT-based, solar_is_derived flag)
33. - [x] Multi-cycle test simulation (30-cycle with ramp limiting)
34. - [x] Test trace output (--trace flag, per-cycle state)
35. - [x] Eco mode: inactive chargers inflating minimums
36. - [x] Self-consumption battery model in test simulation
37. - [x] Battery-aware derived solar mode (battery_power in engine)
38. - [x] Test trace scenario parameters display
39. - [x] Inverter output cap for battery discharge
40. - [x] Remove dedicated solar entity (reverted in #42)
41. - [x] Update trace test output format
42. - [x] Restore dedicated solar entity for inverter limit enforcement
43. - [x] Config flow restructuring + charger phase mapping (L1/L2/L3 → A/B/C)
44. - [x] Per-phase inverter output entities + wiring topology
45. - [x] Unify phase mapping format + reorganize test scenarios into subfolders
46. - [x] Populate empty scenario files + file path in test output (93 scenarios)
47. - [x] Excess mode minimum current fallback
48. - [x] Eco mode: charge at minimum when battery below min SOC + wire allow_grid_charging in tests
49. - [x] Grid/inverter/solar limit: replace proportional scaling with combination-field capping
50. - [x] Use PhaseConstraints.normalize() for combination-field cascading (removes 3x repeated pattern)
51. - [x] Remove unused methods + extract _calculate_active_minimums() helper
52. - [x] Codebase refactoring: extract helpers, consolidate _read_entity, unify element_min/element_max
53. - [x] Hide Phase B/C sensors on single-phase sites
54. - [x] Expose HA service actions (set_charging_mode, set_distribution_mode, set_max_current)
55. - [x] User documentation / setup guide (README.md rewrite)
56. - [x] Entity selector UX — proper HA entity selectors + per-step clearing
57. - [x] Extract auto-detection patterns → `detection_patterns/` package with 12 brand files
58. - [x] Auto-detect battery SOC, battery power, and solar production entities
59. - [x] Per-brand detection pattern files
60. - [x] Fix false-positive battery detection — removed generic `battery_level$` pattern
61. - [x] Auto-detect battery max charge/discharge power
62. - [x] Options flow auto-detection
63. - [x] Wiring topology auto-detect
64. - [x] Charger name prettification
65. - [x] EVSE charging status sensor
66. - [x] `data_description` help text — all steps
67. - [x] Raw `int` → number selectors with min/max/unit
68. - [x] Max import power → checkbox + slider + optional entity
69. - [x] Inverter power UX: battery power hint
70. - [x] **[BUG] stackLevel sent as Decimal instead of int** — Cast to `int()` in `sensor.py` and `__init__.py`.
71. - [x] **[BUG] Feedback loop overcorrects — per-phase draw exceeds max_current** — Charger per-phase current readings can exceed max_current (cause unclear — possibly charger firmware reporting total instead of per-phase, or other OCPP entity issues). Clamped each per-phase draw at `max_current` in `dynamic_ocpp_evse.py` with a warning log.
72. - [x] **[BUG] Battery power sensor false-positive auto-detection** — Added negative lookahead to generic `BATTERY_POWER` pattern to exclude mobile device entities (phones, tablets, laptops, watches).
73. - [x] **[BUG] Hub-aware charger config flow — hide unavailable phases** — Config flow now hides L2/L3 phase mapping fields based on hub's phase count. Engine clamps `active_phases_mask` to site phases as safety net. Handles reconfigure and options flows. Works with split-phase (2-wire) systems.

## In Progress

1. - [ ] **[FEATURE] Phase mapping help text update** — Change data_description for `charger_l1/l2/l3_phase` to "Only change if your charger is wired differently and/or you see unexpected behaviour regarding per-phase power limits." Apply to all 3 phases × 3 flows in `strings.json`, `en.json`, `sl.json`.

2. - [ ] **[FEATURE] Rename "Smart Plug" → "Smart Load"** — Replace all user-facing occurrences in device model strings, config flow labels, step titles. Keep `DEVICE_TYPE_PLUG` internal name unchanged.

3. - [ ] **[FEATURE] Auto-detect power monitoring sensor for plugs** — Add detection patterns for common smart plug power monitoring entities (Shelly, Sonoff, Tasmota). Wire into `async_step_plug_config` via `_auto_detect_entity()`.

4. - [ ] **[FEATURE] Improve hub debug logging** — Restructure debug output into two clear blocks:
    - **Block 1 — Raw entity reads** (`dynamic_ocpp_evse.py`): All values as read from HA entities before any processing.
    - **Block 2 — Derived/computed values** (`dynamic_ocpp_evse.py`): All values after feedback loop and derivations.
    - **Allocation line** (`target_calculator.py`): Add `current_import` and `current_offered` to the Final allocation line.
    - **Charger info line** (`target_calculator.py`): Show effective vs configured, e.g. `mask=A (1ph), hw_phases=3`.
    - **Household clamp warning** (`dynamic_ocpp_evse.py`): Log warning when household clamps to 0 due to feedback overcorrection.

## Backlog

1. - [ ] **[FEATURE] Auto-detect grid inversion — shared infrastructure**: Create `auto_detect.py` module with `AutoDetector` class. Track per-charger previous-cycle state. Files: `auto_detect.py` (new), `dynamic_ocpp_evse.py`.

2. - [ ] **[FEATURE] Auto-detect grid inversion — detection logic + config**: Add `CONF_AUTO_DETECT_INVERSION` to `const.py` (bool, default True). Fire `persistent_notification` when mismatch detected. Files: `const.py`, `auto_detect.py`, `dynamic_ocpp_evse.py`, `config_flow.py`, translations.

3. - [ ] **[FEATURE] Auto-detect phase mapping — detection logic**: Add `CONF_AUTO_DETECT_PHASE_MAPPING` to `const.py` (bool, default False). Correlate charger L-phase draws with grid phase deltas. Files: `const.py`, `auto_detect.py`.

4. - [ ] **[FEATURE] Auto-detect phase mapping — notification & application**: Fire persistent notification with detected vs configured mapping. Option to auto-update config entry. Files: `dynamic_ocpp_evse.py`, `config_flow.py`, translations.

5. - [ ] **[FEATURE] Auto-detection unit tests**: Test `AutoDetector` with simulated update cycles. File: `dev/tests/test_auto_detect.py` (new).

## Other

1. - [ ] **Icon submission** — Submit `icon.png` to [HA brands repo](https://github.com/home-assistant/brands) (see dev/ISSUES.md)
