# Open Issues

1. **Icon not shown in HA/HACS** — HA does not load `icon.png` from the custom component directory. The icon must be submitted as a PR to the [Home Assistant brands repo](https://github.com/home-assistant/brands). The `icon.png` file exists at `custom_components/dynamic_ocpp_evse/icon.png` and is ready to submit.

2. **Atomatic detection of charge rate unit** During the setup process of the ocpp enabled evse, the logic for detecting whether the charger supports A, W, or both does not seem to work. Also, If it cant be detected, it should be left empty for the user to choose.