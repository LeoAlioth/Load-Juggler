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
14. - [x] Charge rate unit detection via OCPP (fixes ISSUES.md #2)
15. - [x] Distribution mode string mismatch (fixes ISSUES.md #3)
16. - [x] Reset service hardcoded 3 phases (fixes ISSUES.md #4)
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
28. - [x] battery_soc_target None crash (fixes ISSUES.md #5)
29. - [x] Charge rate unit case sensitivity (fixes ISSUES.md #6)
30. - [x] Eco mode fake solar surplus at night (partial — see #32)
31. - [x] Dual-frequency update loop (site 5s, charger 15s)
32. - [x] Derived solar production formula fix (CT-based, solar_is_derived flag)
33. - [x] Multi-cycle test simulation (30-cycle with ramp limiting)
34. - [x] Test trace output (--trace flag, per-cycle state)
35. - [x] Eco mode: inactive chargers inflating minimums (fixes ISSUES.md #9)
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
51. - [x] Remove unused methods (PhaseValues: active_mask, __neg__, clamp_min; PhaseConstraints: __sub__, scale) + extract _calculate_active_minimums() helper
52. - [x] Codebase refactoring: extract _send_plug_command/_send_ocpp_command, _allocate_minimums, _build_inverter_constraints; consolidate _read_entity; reuse _get_household_per_phase; unify element_min/element_max

## In Progress

## Backlog

53. - [x] Hide Phase B/C sensors on single-phase sites (requires_phase flag in sensor definitions)
54. - [x] Expose HA service actions (set_charging_mode, set_distribution_mode, set_max_current + services.yaml + translations)
55. - [x] User documentation / setup guide (README.md rewrite: quick start, config reference, services & automations, troubleshooting FAQ)
56. - [x] Entity selector UX — replaced raw entity ID dropdowns with proper HA entity selectors for optional fields; added per-step clearing support via `_normalize_optional_inputs(step_entity_keys)`
57. - [x] Extract auto-detection patterns — moved PHASE_PATTERNS and INVERTER_OUTPUT_PATTERNS to `detection_patterns.py`
58. - [x] Auto-detect battery SOC, battery power, and solar production entities — added BATTERY_SOC_PATTERNS, BATTERY_POWER_PATTERNS, SOLAR_PRODUCTION_PATTERNS; wired into `async_step_hub_battery` via `_auto_detect_entity()`
59. - [x] Per-brand detection pattern files — restructured `detection_patterns.py` → `detection_patterns/` package with 12 brand files (SolarEdge, Solarman/Deye, Fronius, Huawei, Enphase, Victron, Sofar, Sungrow, SMA, GoodWe, Growatt, Fox ESS) + generic catch-all

## Other

1. - [ ] **Icon submission** — Submit `icon.png` to [HA brands repo](https://github.com/home-assistant/brands) (see dev/ISSUES.md)
