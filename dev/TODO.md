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

## In Progress

## Backlog

### Other

32. - [ ] **Icon submission** — Submit `icon.png` to [HA brands repo](https://github.com/home-assistant/brands) (see dev/ISSUES.md)
