# LJ-6: Pre-fill setup from the Energy dashboard

Repos: Load Juggler `~/Projects/Dynamic_OCPP_EVSE` (dev) and Load Insights
`~/Projects/Load_Insights` (dev, only to split one module). Approved by the owner 2026-10-12
(TODO "[FEATURE] Pre-fill the hub from the energy dashboard"). Personal project, so don't log it to
work-context. Commit locally on dev in both repos and don't push (dev auto-deploys).

## Facts this plan rests on (checked 2026-10-12)

- **LJ requires HA 2026.8.0** (`hacs.json`), so `power_config` (2026.3), `stat_soc` and `capacity` (2026.6)
  always exist in the prefs schema. We need **no version guards**. Reading with `.get()` handles a key
  that was never filled in.
- **The top-level `stat_rate` is already in LJ's sign convention.** HA's `EnergyManager._process_battery_power` /
  `_process_grid_power` (energy/data.py) always set the top-level `stat_rate`:
  - to `power_config.stat_rate` when that is given;
  - otherwise to HA's own generated transform sensor (`<src>_inverted`, or
    `sensor.energy_<type>_<from>_<to>_net_power`), with `device_class: power`.

  Positive means import / discharge, the same as LJ. So **LJ never flips a sign**: it offers `stat_rate`
  as it is. Kozolec already uses exactly that sensor today (`battery_power_entity_id:
  sensor.gx_device_dc_battery_power_inverted` = the prefs' battery `stat_rate`).
- **`config_entry_solar_forecast` holds config ENTRY ids, not device ids.** LJ's
  `CONF_SOLAR_FORECAST_DEVICE_IDS` holds device ids, filtered to `open_meteo_solar_forecast`. Mapping:
  `dr.async_entries_for_config_entry(dev_reg, entry_id)`, but only for entries whose `domain ==
  "open_meteo_solar_forecast"`. Other providers (`forecast_solar`) have no `watts` attribute, and
  `_validate_forecast_devices` would reject them anyway. All three entries at Home and Kozolec are
  open_meteo.
- **The dashboard has no phases, but it names the grid meter.** At Home, the grid `power_config` is
  `{stat_rate_inverted: sensor.solaredge_se17k_m1_ac_power}`, and that sensor's DEVICE (the M1) carries
  the per-phase readings.
  - History of 2026-10-11 at midnight, importing: `m1_ac_power_a = -1877 W`, so the M1's per-phase power
    is export-positive like its total, while `m1_ac_current_a = +8.21 A` is import-positive.
  - So the declared inversion holds for the meter's per-phase **power**, not its amps. The owner's working
    config (M1 amps, `invert_phases: false`) agrees.
  - Today's auto-detect (`_power_beside`, watts first) would offer the M1 watts with invert **off** at
    Home, which is the wrong sign. The dashboard's declaration fixes that.
- **LI's reader is `insights/model.py`** (`PowerSpec`, `Device`, `SiteModel.from_prefs`). It is stdlib
  only, has no HA imports and handles every prefs shape. `PowerSpec.rate` is `pc.stat_rate or
  src.stat_rate`, so it is the dashboard-convention sensor above. The same file also carries about 125
  lines of LI-only option helpers (`LOAD_PREFIX`, `migrate_inputs`, `relink`, `scoped`, `follow_renames`,
  `ENERGY_PREF_KEYS`, `dashboard_renames`, `suggest_inputs`, `add_inputs`).

## Decision: verbatim copy after a split in LI

The TODO says the reader "belongs in one place and is shared by copy". Copying `model.py` whole would
drag the LI option helpers into LJ as dead code, so they move out first and the copy is the bare reader.

1. **LI split (one commit in LI):**
   - Move `LOAD_PREFIX`, `migrate_inputs`, `relink`, `scoped`, `add_inputs` and `suggest_inputs` into
     `insights/inputs.py`. They are about inputs and links, and `inputs.py` imports only `.profile`, so
     there is no import cycle.
   - Move `follow_renames`, `ENERGY_PREF_KEYS` and `dashboard_renames` into a new `insights/renames.py`.
   - `model.py` keeps lines 1-200: `PowerSpec`, `Device`, `SiteModel`. That matches its own docstring
     again.
   - Re-point the imports:
     - `__init__.py:25`: `SiteModel` from model; `follow_renames`, `migrate_inputs`, `relink` from
       inputs/renames.
     - `config_flow.py:61`, `coordinator.py:51`, `repairs.py:24`.
   - Move the 12 `tests/test_model.py` cases for the moved helpers into `tests/test_inputs.py` and a new
     `tests/test_renames.py` (loaded as `insights.inputs` / `insights.renames`).
2. **The copy:** byte-for-byte `insights/model.py` → LJ `custom_components/dynamic_ocpp_evse/energy_prefs.py`.
   - The name differs because LJ already has `calculations/models.py`. The bytes are identical.
   - It sits at the package root like `phases.py`.
3. **The guard:** LI `tests/test_model.py` gets `test_load_juggler_carries_this_module_verbatim`. It is
   the `test_phases.py:376` test with the path `Dynamic_OCPP_EVSE/custom_components/dynamic_ocpp_evse/energy_prefs.py`
   and passes when that repo is absent.
4. **Docs:**
   - LI `AGENTS.md`: add a table row for `insights/model.py` in the style of the `phases.py` row (verbatim
     copy in LJ, change it here, copy it whole).
   - LJ `AGENTS.md` code structure: add `energy_prefs.py` next to `phases.py`, worded the same way:
     verbatim copy, canonical in LI `insights/model.py`, never edit it here, checked by LI's
     `tests/test_model.py`.

## Which field gets which pref

| LJ form (step) | Field | From the prefs | Rule |
|---|---|---|---|
| inverter create `inverter_features` | `CONF_INVERTER_FEATURES` | solar if `site.solar`, battery if `site.battery_out` | create flow only; battery_control is never suggested |
| inverter create `inverter_config` | `CONF_SOLAR_PRODUCTION_ENTITY_ID` | `site.solar_power[0]` | only when exactly 1 solar source **and** it has a `stat_rate`. Several arrays (Kozolec's two MPPTs) get nothing: LJ takes one total sensor, and summing would need a helper |
| same | `CONF_SOLAR_FORECAST_DEVICE_IDS` | every `site.solar_forecast_entries` → its open_meteo devices | merged across all solar sources |
| inverter create `inverter_battery` | `CONF_BATTERY_POWER_ENTITY_ID` | `site.battery_power[0].rate` | only with exactly 1 battery source (`len(site.battery_out) == 1`). The capacity sum and SOC are per battery |
| same | `CONF_BATTERY_SOC_ENTITY_ID` | `site.battery_soc[0]` | same |
| same | `CONF_BATTERY_CAPACITY_KWH` | `site.battery_capacity_kwh` (number, kWh, no unit conversion) | same |
| hub create `hub_grid` | phase A/B/C CT | per-phase readings on the grid meter's DEVICE | see below |
| same | `CONF_INVERT_PHASES` | `True` when the meter is declared `stat_rate_inverted` **and** the triple is its power | amps or a from/to pair → `False` |

These are **not** pre-filled:
- `CONF_BATTERY_CAPACITY_ENTITY_ID`: the dashboard has a number, not a sensor.
- Battery max charge/discharge: the dashboard doesn't know them, although the TODO's "max power" asked
  for it.
- Inverter output per phase.
- Anything else on the hub.

**Grid meter → CTs.**
- Use only when there is exactly one grid source with a PowerSpec.
- Pick the raw sensor: the first of `spec.rate_inverted` (inverted), `spec.rate_from` and `spec.rate`
  that has a registry `device_id`. Skip `stat_rate` itself, which is HA's or a template's derived sensor
  with no device at Home.
- Match that device's rows with the existing `match_meter_entities(rows, "grid")`, power winning a tie.
- If it yields at least one phase, it replaces `_same_device_fill`'s result as the suggested triple.
  Otherwise the current auto-detect runs unchanged.
- Kozolec's grid has no power sensor, so nothing changes there.

**Who gets suggestions.**
- Values already in the form always win. A suggestion only fills a key that is missing, `None`, `""` or
  `[]`.
- Suggestions apply only when the form is first shown (`user_input is None`). If validation fails, the
  form shows what the user typed, so a field they cleared stays cleared.
- An entity suggestion is dropped when `hass.states.get(eid)` is `None`. On submit it still goes through
  `_validate_entity_units`, which accepts W and kW, so kW sensors need no special handling.
- **Only on the initial setup forms** (2026-10-12): the hub wizard (`hub_grid`) and the inverter
  wizard while a new inverter entry is being created. Never on an options/Configure page: there an empty
  field may be one he cleared on purpose.
- Inverter suggestions go only to the hub's **first** inverter, i.e. when `get_inverters_for_hub(hass, hub_id)`
  is empty. On a fleet, the dashboard's single battery or array can't be attributed to one inverter, and the
  second inverter would just be offered the first one's sensors.
- Nothing is ever written to the prefs. LJ only calls `async_get_manager(hass)` and reads `.data`.
- Any exception reading them (store not loaded, odd shape) is logged at debug level and treated as "no
  dashboard", so setup is never blocked.

## Files

### LJ `custom_components/dynamic_ocpp_evse/energy_prefs.py` (new)
- Verbatim copy of LI `insights/model.py` after the split. Never edit it here.

### LJ `config_flow/helpers.py`
- **Imports:** `from homeassistant.components.energy.data import async_get_manager`;
  `from ..energy_prefs import SiteModel`; `dr.async_entries_for_config_entry`.
- **`async def _energy_site(hass) -> SiteModel`:** `SiteModel.from_prefs((await async_get_manager(hass)).data)`.
  Any exception → `SiteModel()`.
- **`def _inverter_suggestions(hass, site, first: bool) -> dict`:** the inverter rows of the table. `{}`
  when `not first`. Drops entities without a state.
- **`def _forecast_devices(hass, entry_ids) -> list[str]`:** entry id → open_meteo devices.
- **`def _meter_triple(hass, device_id) -> tuple[str, dict]`:** extract the "power or current, power on
  a tie" block from the end of `_same_device_fill` (lines ~407-415) so both callers share it. It returns
  `(kind, triple)`.
- **`def _grid_meter_suggestion(hass, site) -> dict`:** the grid rule above. Returns the three CT keys
  plus `CONF_INVERT_PHASES`, or `{}`.
- **`def _fill_empty(defaults, suggestions) -> dict`:**
  `{**defaults, **{k: v for k, v in suggestions.items() if defaults.get(k) in (None, "", [])}}`.
- `manifest.json`: leave unchanged. `energy` is core and `async_get_manager` loads its store on demand,
  which LI does too. Add `"energy"` to `after_dependencies` only if a test shows a race.

### LJ `config_flow/flow.py`
- `__init__`: `self._suggest: dict | None = None`.
- Add a method `async def _inverter_suggest(self)` that computes the suggestions once per flow:
  `_inverter_suggestions(self.hass, await _energy_site(self.hass), first=not get_inverters_for_hub(...))`.
- `async_step_inverter_features`: on first show the schema defaults become
  `_fill_empty(self._data, await self._inverter_suggest())`, features key only.
- `async_step_inverter_config`: on first show,
  `defaults = _fill_empty(defaults, {k: v for k in (production, forecast)})`.
- `async_step_inverter_battery`: on first show, fill power, SOC and capacity kWh.
- `async_step_hub_grid`: on first show,
  `meter = _grid_meter_suggestion(self.hass, await _energy_site(self.hass))`.
  - If it has any CT, use `meter` as the schema defaults.
  - Otherwise keep the `_same_device_fill` triple.
  - `CONF_INVERT_PHASES` comes from `meter` when it is present.

### LJ `config_flow/options.py`
- No change. The Configure pages never pre-fill (2026-10-12).

### LJ `config_flow/schemas.py`
- No change. The builders already read defaults, and the entity fields use `suggested_value`, so they
  stay clearable.

### LJ `translations/en.json` + `sl.json`
Append one sentence to the `description` of `config.step.hub_grid`, `config.step.inverter_features`,
`config.step.inverter_config` and `config.step.inverter_battery` (setup steps only, none under `options`).
It is static, with no placeholder.
- en: " Empty fields may be pre-filled from the Energy dashboard - check them before you continue."
- sl: " Prazna polja so lahko vnaprej izpolnjena iz nadzorne plošče Energija - preverite jih, preden nadaljujete."

### LJ `dev/tests/test_energy_prefill.py` (new)
- **Setup:** patch `custom_components.dynamic_ocpp_evse.config_flow.helpers.async_get_manager` with an
  `AsyncMock` returning `SimpleNamespace(data=PREFS, async_update=AsyncMock())`. `PREFS` are trimmed copies
  of `HA_Configs/{home,kozolec}/snapshots/2026-10-12/energy_prefs.json`, with only `energy_sources`.
- **Kozolec inverter create:**
  - features suggests `[solar, battery]`;
  - `inverter_config` suggests both forecast devices (two `MockConfigEntry(domain="open_meteo_solar_forecast")`
    with devices) and **no** production sensor (two solar sources);
  - `inverter_battery` suggests `sensor.gx_device_dc_battery_power_inverted`, `sensor.jk_bms_id_512_charge`
    and capacity 62.
- **Home inverter create:** production `sensor.solaredge_se17k_i1_ac_power`, one forecast device, no
  battery feature.
- **Second inverter on the hub:** no suggestion anywhere.
- **A suggested entity with no state:** not offered.
- **Typed values win:** inverter create, fail `inverter_battery` validation with SOC typed and power
  empty → the re-shown form keeps the typed SOC and suggests nothing.
- **Configure pages never pre-fill:** with prefs present, the options `inverter_solar`, `inverter_battery`
  and `hub_connection` pages of an entry with empty fields show no suggested value.
- **Cleared stays cleared:** submit `inverter_config` with the production field removed plus a unit error
  → the re-shown form has no production suggestion.
- **Hub grid:**
  - a device with `power_a..c` and `current_a..c` and prefs `stat_rate_inverted` → the power triple and
    `invert_phases` default True;
  - a meter with amps only → amps and False;
  - no grid power → the current `_same_device_fill` result unchanged.
- **Manager raises:** forms render, no suggestions.
- **Never writes:** `async_update` is not awaited in any test.
- **Assertions:** read `suggested_value` / `default` from `result["data_schema"].schema`, as the existing
  e2e tests do.

### LJ `RELEASE_NOTES.md`, under `## 2.1.6` → `### New Features`
- **Setup picks up the Energy dashboard**: adding an inverter now offers what the Energy dashboard already
  names:
  - its PV power sensor when there is one array;
  - the Open-Meteo forecast devices of every array;
  - with one battery: the battery's power sensor (the dashboard's own signed one, so no inversion is
    needed), its SOC sensor and its capacity.

  Hub setup takes the per-phase grid readings from the device of the dashboard's grid meter, and ticks
  *Invert phases* when the dashboard declares that meter's power inverted and the readings are its watts.
  These are only suggestions in empty fields of the setup forms, to check before continuing; the
  Configure pages never pre-fill. Values already set always win.
  The dashboard is only read, never changed. A second inverter on the same hub gets no suggestions,
  because the dashboard can't say which inverter its battery or array belongs to.

### LJ `dev/TODO.md`
- Remove the "[FEATURE] Pre-fill the hub from the energy dashboard" item when it lands.
- Add nothing for multi-array production unless the owner asks. It is a non-goal, recorded here.

## Order of work
1. LI: split `model.py`, move the tests, add the byte-for-byte test. Run `cd ~/Projects/Load_Insights && .venv/bin/python tests/run_all.py`
   (the verbatim test passes vacuously until step 2). Commit on LI dev.
2. LJ: `cp ../Load_Insights/custom_components/load_insights/insights/model.py custom_components/dynamic_ocpp_evse/energy_prefs.py`,
   update `AGENTS.md`, then make the helpers, flow and translations changes and add the tests.
3. Verify:
   - `cd ~/Projects/Dynamic_OCPP_EVSE && .venv/bin/python -m pytest dev/tests -q -n 8`
   - `.venv/bin/python -m pytest dev/tests/test_energy_prefill.py dev/tests/test_config_flow_e2e.py -q`
   - `cd ~/Projects/Load_Insights && .venv/bin/python tests/test_model.py`, which now checks
     the LJ copy for real.
   - The existing translation-key tests in both repos.
4. RELEASE_NOTES + TODO, then commit on LJ dev. No push, no HA install.

## Non-goals / open for the owner
- **Options / Configure pages: no pre-fill** (2026-10-12). It would go against fields cleared on purpose.
- **Multi-array production:** LJ takes one sensor; a min_max/template sum (Kozolec's `sensor.kozolec_total_pv_power`) stays manual.
- **Possible LI bug, outside LJ:** with HA 2026.3+ prefs, `PowerSpec` carries both `rate` (HA's
  generated sensor) and `rate_inverted` (the raw one), and `polarity` answers -1. That is right for the
  raw meter `detection.py:520` reads, but wrong if anyone ever pairs it with `.rate`. Nothing does today.
