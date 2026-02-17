# Open Issues

1. **Icon not shown in HA/HACS** — HA does not load `icon.png` from the custom component directory. The icon must be submitted as a PR to the [Home Assistant brands repo](https://github.com/home-assistant/brands). The `icon.png` file exists at `custom_components/dynamic_ocpp_evse/icon.png` and is ready to submit.

2. **`MaxImportPowerSlider` class missing** — Referenced at `number.py:65` but never defined. Would crash at setup if `enable_max_import=True` and no entity override is configured. Needs a new class similar to `PowerBufferSlider`.

All other issues have been moved to detailed TODOs in `TODO.md` (#75–#82).
