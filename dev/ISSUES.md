# Open Issues

1. **Options flow clears config** — `async_create_entry(data={})` in `config_flow.py` overwrites `entry.options` with `{}`. Do NOT use the HA UI options flow to change hub/charger settings until this is fixed. Fix: return `async_create_entry(data={**self.config_entry.options, **self._data})`. Tests documenting this bug: `test_options_flow_hub_saves_changes`, `test_options_flow_charger_saves_changes` in `test_config_flow_e2e.py`.
2. **Image not available**. The icon.png is not being shown in HA integratoins nor HACS
