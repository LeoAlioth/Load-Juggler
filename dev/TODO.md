# TODO

## Completed

1. - [x] PhaseValues → Optional[float] + helpers
2. - [x] Available current feature
3. - [x] 1-phase grid limit bug fix + test scenarios
4. - [x] SiteContext.num_phases → derived property
5. - [x] Dedicated Solar Power Entity
6. - [x] Smart Load support (device_type evse/plug)
7. - [x] Charge rate unit detection via OCPP
8. - [x] Distribution mode string mismatch fix
9. - [x] PhaseConstraints refactoring
10. - [x] Smart Load UX improvements
11. - [x] Translations (en.json + sl.json)
12. - [x] Current rate limiting
13. - [x] Auto-reset for non-compliant chargers
14. - [x] Inverter configuration in config flow
15. - [x] Grid consumption feedback loop
16. - [x] Charge pause UX improvements
17. - [x] Dual-frequency update loop (site 5s, charger 15s)
18. - [x] Derived solar production formula fix
19. - [x] Multi-cycle test simulation + trace output
20. - [x] Battery-aware derived solar mode
21. - [x] Inverter output cap for battery discharge
22. - [x] Config flow restructuring + charger phase mapping
23. - [x] Per-phase inverter output entities + wiring topology
24. - [x] Test scenario reorganization (99 scenarios)
25. - [x] Eco mode fixes (night surplus, inactive charger minimums, battery min SOC)
26. - [x] Excess mode minimum current fallback
27. - [x] PhaseConstraints.normalize() + combination-field capping
28. - [x] Codebase refactoring (helpers, _read_entity, element_min/max)
29. - [x] Hide Phase B/C sensors on single-phase sites
30. - [x] HA service actions (set_charging_mode, set_distribution_mode, set_max_current)
31. - [x] User documentation / README.md rewrite
32. - [x] Entity selector UX
33. - [x] Auto-detection patterns (12 brand files)
34. - [x] Auto-detect battery + solar entities (SOC, power, charge/discharge)
35. - [x] Options flow auto-detection
36. - [x] Wiring topology auto-detect
37. - [x] Charger name prettification
38. - [x] EVSE charging status sensor
39. - [x] `data_description` help text — all steps
40. - [x] Number selectors with min/max/unit
41. - [x] Max import power → checkbox + slider + optional entity
42. - [x] Inverter power UX: battery power hint
43. - [x] stackLevel Decimal → int bug fix
44. - [x] Feedback loop per-phase draw clamping
45. - [x] Battery power sensor false-positive fix
46. - [x] Hub-aware charger config flow (hide unavailable phases)
47. - [x] Phase mapping help text update
48. - [x] Rename "Smart Plug" → "Smart Load"
49. - [x] Hub debug logging (raw reads, feedback, per-charger, allocation)
50. - [x] Fix entity state lookup bug — use `hass.data[DOMAIN]` shared store instead of entity ID guessing
51. - [x] Fix battery SOC target/min sliders not feeding into calculation + create missing MaxImportPowerSlider
52. - [x] Fix EVSE min/max current sliders missing value clamping
53. - [x] Remove unused `ButtonEntity` import from `__init__.py`
54. - [x] Connector status entity ID deduplication in sensor.py
55. - [x] Move `_read_inverter_output()` to module scope in dynamic_ocpp_evse.py
56. - [x] Extract `HubEntityMixin` + `ChargerEntityMixin` into `entity_mixins.py`
57. - [x] Deduplicate `_write_to_hub_data` / `_write_to_charger_data` via mixin `_hub_data_key`/`_charger_data_key`
58. - [x] Deduplicate `async_added_to_hass` restore pattern via `_restore_and_publish_number()`
59. - [x] Per-phase loops in `dynamic_ocpp_evse.py` (grid reads, feedback, headroom)
60. - [x] Split `run_hub_calculation()` into subfunctions (~560→~170 lines)
61. - [x] Move rate limiting from OCPP command to allocated current level (smooth sensor display, bypass on mode change)
62. - [x] Hub sensor cleanup: Solar Surplus → Solar Available Power (production - household), remove Solar Surplus Current, deduplicate battery sensors

## In Progress

1. - [ ] **[FEATURE] Auto-detect power monitoring sensor for plugs** — Add detection patterns for common smart plug power monitoring entities (Shelly, Sonoff, Tasmota). Wire into `async_step_plug_config` via `_auto_detect_entity()`.

## Backlog

1. - [ ] **[FEATURE] Auto-detect grid inversion — shared infrastructure**: Create `auto_detect.py` module with `AutoDetector` class. Track per-charger previous-cycle state. Files: `auto_detect.py` (new), `dynamic_ocpp_evse.py`.

2. - [ ] **[FEATURE] Auto-detect grid inversion — detection logic + config**: Add `CONF_AUTO_DETECT_INVERSION` to `const.py` (bool, default True). Fire `persistent_notification` when mismatch detected. Files: `const.py`, `auto_detect.py`, `dynamic_ocpp_evse.py`, `config_flow.py`, translations.

3. - [ ] **[FEATURE] Auto-detect phase mapping — detection logic**: Add `CONF_AUTO_DETECT_PHASE_MAPPING` to `const.py` (bool, default False). Correlate charger L-phase draws with grid phase deltas. Files: `const.py`, `auto_detect.py`.

4. - [ ] **[FEATURE] Auto-detect phase mapping — notification & application**: Fire persistent notification with detected vs configured mapping. Option to auto-update config entry. Files: `dynamic_ocpp_evse.py`, `config_flow.py`, translations.

5. - [ ] **[FEATURE] Auto-detection unit tests**: Test `AutoDetector` with simulated update cycles. File: `dev/tests/test_auto_detect.py` (new).

## Other

1. - [ ] **Icon submission** — Submit `icon.png` to [HA brands repo](https://github.com/home-assistant/brands) (see dev/ISSUES.md)
