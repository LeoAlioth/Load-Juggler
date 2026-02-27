# Load Juggler — TODO

## In Progress

### Add solar/excess mode grace period to EVSE and plug configuration

### integration already automatically calls reset occp profile if weird behaviour is detected. If that does not help, an escalation - reset the evse completely should be triggered

### Expose current phase mask on EVSE as a separate entity (on 3 phase evse only)

### update evse/plug status, to show that the grace period timer is running (countdown untill it stops)

## Backlog

- [ ] **Device-based OCPP discovery** — select OCPP device instead of entity, auto-find all entities (supports per-phase separate entities)
- [ ] **Hot Water Tank device type** — thermostat control (Normal/Boost), modes: Solar Only, Excess
- [ ] **SG Ready device type** — 2-relay site-state mapping (Block/Normal/Recommend ON/Force ON), no user modes

## Other

- [ ] **Icon submission** — Submit `icon.png` to [HA brands repo](https://github.com/home-assistant/brands) (see dev/ISSUES.md)

## Completed (107 items)

1. PhaseValues → Optional[float] + helpers
2. Available current feature
3. 1-phase grid limit bug fix + test scenarios
4. SiteContext.num_phases → derived property
5. Dedicated Solar Power Entity
6. Smart Load support (device_type evse/plug)
7. Charge rate unit detection via OCPP
8. Distribution mode string mismatch fix
9. PhaseConstraints refactoring
10. Smart Load UX improvements
11. Translations (en.json + sl.json)
12. Current rate limiting
13. Auto-reset for non-compliant chargers
14. Inverter configuration in config flow
15. Grid consumption feedback loop
16. Charge pause UX improvements
17. Dual-frequency update loop (site 5s, charger 15s)
18. Derived solar production formula fix
19. Multi-cycle test simulation + trace output
20. Battery-aware derived solar mode
21. Inverter output cap for battery discharge
22. Config flow restructuring + charger phase mapping
23. Per-phase inverter output entities + wiring topology
24. Test scenario reorganization (99 scenarios)
25. Eco mode fixes (night surplus, inactive charger minimums, battery min SOC)
26. Excess mode minimum current fallback
27. PhaseConstraints.normalize() + combination-field capping
28. Codebase refactoring (helpers, _read_entity, element_min/max)
29. Hide Phase B/C sensors on single-phase sites
30. HA service actions (set_charging_mode, set_distribution_mode, set_max_current)
31. User documentation / README.md rewrite
32. Entity selector UX
33. Auto-detection patterns (12 brand files)
34. Auto-detect battery + solar entities (SOC, power, charge/discharge)
35. Options flow auto-detection
36. Wiring topology auto-detect
37. Charger name prettification
38. EVSE charging status sensor
39. `data_description` help text — all steps
40. Number selectors with min/max/unit
41. Max import power → checkbox + slider + optional entity
42. Inverter power UX: battery power hint
43. stackLevel Decimal → int bug fix
44. Feedback loop per-phase draw clamping
45. Battery power sensor false-positive fix
46. Hub-aware charger config flow (hide unavailable phases)
47. Phase mapping help text update
48. Rename "Smart Plug" → "Smart Load"
49. Hub debug logging (raw reads, feedback, per-charger, allocation)
50. Fix entity state lookup bug — use `hass.data[DOMAIN]` shared store
51. Fix battery SOC target/min sliders not feeding into calculation + create missing MaxImportPowerSlider
52. Fix EVSE min/max current sliders missing value clamping
53. Remove unused `ButtonEntity` import from `__init__.py`
54. Connector status entity ID deduplication in sensor.py
55. Move `_read_inverter_output()` to module scope
56. Extract `HubEntityMixin` + `ChargerEntityMixin` into `entity_mixins.py`
57. Deduplicate `_write_to_hub_data` / `_write_to_charger_data` via mixin
58. Deduplicate `async_added_to_hass` restore pattern via `_restore_and_publish_number()`
59. Per-phase loops in `dynamic_ocpp_evse.py` (grid reads, feedback, headroom)
60. Split `run_hub_calculation()` into subfunctions (~560→~170 lines)
61. Move rate limiting from OCPP command to allocated current level
62. Hub sensor cleanup: Solar Surplus → Solar Available Power, deduplicate battery sensors
63. Debug log: show human-readable charger names instead of entry_id hashes
64. Hub sensor renames: shorter, consistent naming
65. Fix entity selector clearing: `suggested_value` instead of `default`
66. Fix options flow Submit → Next button on non-final steps
67. Add Sony Xperia phone exclusion to generic battery auto-detection patterns
68. Reload config entry on options change
69. EMA smoothing + Schmitt trigger dead band + faster site refresh (2s)
70. Input-level EMA smoothing on grid CT, solar, battery power, and inverter output
71. Debug log shows both raw and smoothed values
72. Auto-detect OCPP `MeterValueSampleInterval` for charger update frequency
73. Fix "Finishing"/"Faulted" connector status: treat as inactive
74. Auto-detect power monitoring sensor for smart plugs
75. Auto-detect grid CT inversion
76. Auto-detect phase mapping
77. Solar/Excess grace period (anti-flicker)
78. Charge pause duration unit change (seconds → minutes) with v2.1→v2.2 migration
79. Per-load operating modes — foundation
80. Per-load operating modes — calculation engine
81. Per-load operating modes — test scenarios (110 passing)
82. Per-load operating modes — HA integration
83. Per-load operating modes — translations & services
84. Rename ChargerContext → LoadContext
85. Fix case-insensitive OCPP phase attribute reading
86. Rename "Total EVSE Power" → "Total Managed Power"
87. Two-stage auto-detect phase mapping with swap logic
88. 2-phase car inactive line detection
89. Confidence-weighted auto-detect scoring
90. 10% clamping tolerance for W-based chargers
91. Fix W-based OCPP power multiplication
92. Per-device operating mode in debug logs
93. Charger targets log: show both allocated and available current
94. Expose `available_current` as sensor attribute in HA
95. Available Current sensor shows available (not allocated) current
96. Circuit Groups — shared breaker limits (9 test scenarios)
97. Grid CT stale detection with EMA holdover and 60s timeout
98. Site available power cap by `max_grid_import_power`
99. Resilience improvements — OCPP try-except, `_UNAVAILABLE` sentinel, NaN/inf guard, stale member filtering
100. Off-grid support — optional Phase A CT, unified solar derivation, inverter-based phase count fallback
101. Hub status sensor — config validation + runtime warnings
102. Cleanup — removed dead `car_phases` field, removed auto-detect state double-init
103. SuspendedEV handling — near-zero draw treated as inactive after 60s grace period
104. Battery SOC hysteresis — HA layer hysteresis with 14 boundary test scenarios
105. Charger finishing test scenarios — 8 scenarios for capacity redistribution
106. Fix test infrastructure — `minor_version=2`, Python 3.12 CI, pytest-homeassistant-custom-component>=0.13.110
107. Add HA integration tests to CI — calculation + pytest tests before releases

