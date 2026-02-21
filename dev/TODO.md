# Load Juggler — TODO

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
63. - [x] Debug log: show human-readable charger names instead of entry_id hashes in "Charger targets" line
64. - [x] Hub sensor renames: shorter, consistent naming (Current X Power / Available X Power), add Current Solar Power sensor
65. - [x] Fix entity selector clearing: `suggested_value` instead of `default` so X button truly clears the field
66. - [x] Fix options flow Submit → Next button on non-final steps (`last_step=False/True`)
67. - [x] Add Sony Xperia (`xq_`) phone exclusion to generic battery auto-detection patterns
68. - [x] Reload config entry on options change — removes stale battery sliders/switch when battery entities are cleared
69. - [x] EMA smoothing + Schmitt trigger dead band + faster site refresh (2s) to eliminate current oscillation
70. - [x] Input-level EMA smoothing on grid CT, solar, battery power, and inverter output readings before engine
71. - [x] Debug log shows both raw and smoothed values (smoothed(raw) format) for CT, solar, and battery power
72. - [x] Auto-detect OCPP `MeterValueSampleInterval` and use as default charger update frequency in config flow
73. - [x] Fix "Finishing"/"Faulted" connector status: treat as inactive (no power allocation), skip OCPP profiles and charge control toggle
74. - [x] Auto-detect power monitoring sensor for smart plugs (Shelly, Sonoff, Tasmota, Kasa, Tuya) in config flow
75. - [x] Auto-detect grid CT inversion — correlates charger draw vs grid current, fires persistent notification after 10/15 inverted signals
76. - [x] Auto-detect phase mapping — correlates total charger draw vs per-phase grid deltas, fires persistent notification on mismatch (opt-in via config flow)
77. - [x] Solar/Excess grace period (anti-flicker) — configurable hold-at-min timer before pausing when mode conditions drop, respects site limits for immediate stop
78. - [x] Charge pause duration unit change — seconds → minutes for consistency, with migration from v2.1 to v2.2
79. - [x] Per-load operating modes — foundation (rename constants, add `operating_mode` to `LoadContext`, remove hub-level `charging_mode`)
80. - [x] Per-load operating modes — calculation engine (dual-pool distribution, source-aware allocation, urgency+priority sorting)
81. - [x] Per-load operating modes — test scenarios (migrate YAML files, add 8 mixed-mode scenarios, 110 passing)
82. - [x] Per-load operating modes — HA integration (`__init__.py`, `dynamic_ocpp_evse.py`, `select.py`, `sensor.py`, `config_flow.py`)
83. - [x] Per-load operating modes — translations & services (`services.yaml`, `en.json`, `sl.json`)
84. - [x] Rename ChargerContext → LoadContext across codebase
85. - [x] Fix case-insensitive OCPP phase attribute reading (`_read_phase_attr` lowercase normalization)
86. - [x] Rename "Total EVSE Power" → "Total Managed Power" sensor display name
87. - [x] Two-stage auto-detect phase mapping — notify + auto-remap with swap logic, always-updated snapshots
88. - [x] 2-phase car inactive line detection — non-correlating grid phase reveals inactive charger line, full L1/L2/L3→A/B/C verification
89. - [x] Confidence-weighted auto-detect scoring — weighted scores (weight = min(|delta|, 15) / 5), soft decay, fast oscillation detection
90. - [x] 10% clamping tolerance for W-based chargers — avoids false clamping from voltage/rounding variance
91. - [x] Fix W-based OCPP power multiplication — use car's active phase count instead of charger hardware phases for W conversion
92. - [x] Per-device operating mode in debug logs
93. - [x] Charger targets log: show both allocated and available current
94. - [x] Expose `available_current` as sensor attribute in HA

95. - [x] Available Current sensor shows available (not allocated) current in HA
96. - [x] Circuit Groups — shared breaker limits (post-distribution capping, PhaseConstraints reuse, 9 test scenarios)
97. - [x] Grid CT stale detection — hold last EMA value, fallback to min_current after 60s timeout, `grid_stale` attribute on hub sensor
98. - [x] Site available power cap — `total_site_available_power` and `available_grid_power` now capped by `max_grid_import_power`

## In Progress

(none)

## Completed (cont.)

99. - [x] Resilience improvements — OCPP try-except + plug switch error handling, `_UNAVAILABLE` sentinel + EMA holdover for solar/battery/inverter sensors, NaN/inf guard in `_smooth()`, stale circuit group member filtering, voltage ≤0 guard, plug empty-phase crash fix
100. - [x] Off-grid support — Phase A CT optional in config flow, `_get_hub_phase_count` falls back to inverter output entities, unified solar derivation via `_derive_solar_production()` (inverter-based for series/parallel, export-based fallback), off-grid phases zeroed from inverter output config (no separate code paths)

## Backlog

1. - [ ] **Device-based OCPP discovery** — select OCPP device instead of entity, auto-find all entities (supports per-phase separate entities)
2. - [ ] **Hot Water Tank device type** — thermostat control (Normal/Boost), modes: Solar Only, Excess
3. - [ ] **SG Ready device type** — 2-relay site-state mapping (Block/Normal/Recommend ON/Force ON), no user modes

## Other

1. - [ ] **Icon submission** — Submit `icon.png` to [HA brands repo](https://github.com/home-assistant/brands) (see dev/ISSUES.md)
