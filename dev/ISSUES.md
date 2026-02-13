# Open Issues

1. **Icon not shown in HA/HACS** — HA does not load `icon.png` from the custom component directory. The icon must be submitted as a PR to the [Home Assistant brands repo](https://github.com/home-assistant/brands). The `icon.png` file exists at `custom_components/dynamic_ocpp_evse/icon.png` and is ready to submit.
2. **Configure button throws an error**. For both site and evse, the configure button throws an error: Config flow could not be loaded: 500 Internal Server Error Server got itself in trouble
