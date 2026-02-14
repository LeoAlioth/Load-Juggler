# Open Issues

1. **Icon not shown in HA/HACS** — HA does not load `icon.png` from the custom component directory. The icon must be submitted as a PR to the [Home Assistant brands repo](https://github.com/home-assistant/brands). The `icon.png` file exists at `custom_components/dynamic_ocpp_evse/icon.png` and is ready to submit.

2. ~~**Automatic detection of charge rate unit**~~ **FIXED** — Detection now queries the charger via OCPP `GetConfiguration` for the `ChargingScheduleAllowedChargingRateUnit` key (returns `"Current"`, `"Power"`, or `"Current,Power"`). If detection succeeds, the value is pre-filled in the dropdown. If it fails, the field is left empty for the user to choose. Detection also runs in reconfigure/options flows. Removed the old unreliable sensor UoM-based detection.