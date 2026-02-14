# Open Issues

1. **Icon not shown in HA/HACS** — HA does not load `icon.png` from the custom component directory. The icon must be submitted as a PR to the [Home Assistant brands repo](https://github.com/home-assistant/brands). The `icon.png` file exists at `custom_components/dynamic_ocpp_evse/icon.png` and is ready to submit.

2. ~~**Automatic detection of charge rate unit**~~ **FIXED** — Detection now queries the charger via OCPP `GetConfiguration` for the `ChargingScheduleAllowedChargingRateUnit` key (returns `"Current"`, `"Power"`, or `"Current,Power"`). If detection succeeds, the value is pre-filled in the dropdown. If it fails, the field is left empty for the user to choose. Detection also runs in reconfigure/options flows. Removed the old unreliable sensor UoM-based detection.

3. ~~**Distribution mode "Sequential - Optimized" and "Sequential - Strict" silently defaulted to Priority**~~ **FIXED** — The `target_calculator.py` comparison used exact lowercase match (`"optimized"`, `"strict"`) but the HA select entity stores the full display strings (`"Sequential - Optimized"`, `"Sequential - Strict"`). Fixed to use substring matching (`"optimized" in mode`).

4. ~~**Reset service hardcoded 3 phases for Watts conversion**~~ **FIXED** — `__init__.py` used `voltage * 3` regardless of charger config. Now reads `CONF_PHASES` from the charger's config entry.



5. ~~**TypeError: battery_soc < battery_soc_target when target is None**~~ **FIXED** — Added `site.battery_soc_target is not None` guard to all 4 comparison sites in `target_calculator.py` (asymmetric solar surplus, symmetric solar surplus, ECO mode, Solar mode).

6. ~~**Case-sensitive charge rate unit detection**~~ **FIXED** — `config_flow.py` `_detect_charge_rate_unit()` now normalizes to lowercase before matching `"current"` / `"power"`, handling chargers that return lowercase values like `"power"`.