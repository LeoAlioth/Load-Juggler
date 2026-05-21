# Load Juggler — TODO

## In Progress

_Nothing in progress._

> **Recently completed (2026-05-20)**
>
> - **Codebase audit** — 3 Critical, 9 High and 7 Medium bugs fixed and verified.
> - **EVSE phase-mask entity** — `LoadJugglerPhaseMaskSensor`, 3-phase EVSEs only.
> - **Hub config flow: no grid CTs ⇒ battery required** — hard block in config /
>   reconfigure / options flows (`validate_offgrid_battery_requirement`).
> - **Hot Water Tank device type** — `hot_water_tank` device type: a binary load
>   driven through a `climate` entity. New `hot_water_tank.py`
>   (`resolve_tank_setpoint` + `send_hot_water_tank_command`),
>   `_build_hot_water_tank_charger` in `dynamic_ocpp_evse.py`, config/reconfigure/
>   options flow steps, 3 setpoint sliders, mode select (Freeze Protection /
>   Normal / Solar Only), and a tank status sensor. The engine treats the tank as
>   a plug — no engine changes. Verified: 141 calc-scenario + 112 HA-layer tests.
>   Note: the Normal-mode "excess available" trigger uses
>   `total_export_power > heating_element_power` (self-scaling) rather than the
>   13 kW `excess_export_threshold` — the EVSE threshold is too high for a tank.

## Backlog

- [ ] **Entity-backed inverter capacity** — the inverter max power (`CONF_INVERTER_MAX_POWER`) is a fixed value set at config time. Add an optional entity override, mirroring the Max Import Power slider + `CONF_MAX_IMPORT_POWER_ENTITY_ID` pattern: when an entity is configured, `run_hub_calculation` reads its live value each cycle. This lets an external automation drop the inverter capacity toward 0 on an inverter high-temperature alarm — a clean replacement for a per-load inverter-overheat failsafe (cf. the `evse.yaml` controller's stop-on-overheat) without a dedicated emergency-stop input.
- [ ] **Device-based OCPP discovery (refactor)** — discovery already works and auto-finds per-phase L1/L2/L3 entities, but by _guessing_ sibling entity names from a `base_name` prefix (`_discover_ocpp_chargers` in `config_flow.py`), and the OCPP device ID is a free-text field. Replace with a device-registry **device selector** + enumerate entities via `entity.device_id`, so it's robust to non-standard OCPP entity naming and supports per-phase separate entities reliably.
- [ ] **SG Ready device type** — 2-relay site-state mapping (Block/Normal/Recommend ON/Force ON), no user modes
- [ ] **Rename generic `charger` → `load`** _(low priority)_ — `charger` is used as the generic term for a managed device, but plugs and hot water tanks aren't chargers (the codebase already uses `LoadContext` / `LoadJuggler`). Large: ~3000 occurrences across ~50 files. Rename the **generic** uses → `load`: the `chargers` collections, `LoadContext.charger_id`, `SiteContext.chargers`, the `charger_*` hub-data keys (`charger_targets`, `charger_available`, `charger_modes`, `charger_rank`, `charger_draw`, `charger_names`, `charger_active_phases`, `charger_phase_masks`), the `hass.data[DOMAIN]` keys (`chargers`, `charger_allocations`, `charger_ranks`, `charger_status`), `ChargerEntityMixin` + `_write_to_charger_data`, engine funcs (`calculate_all_charger_targets`, `_sort_chargers`, `_add_chargers_to_site`, `get_chargers_for_hub`, `get_hub_for_charger`), `ENTRY_TYPE_CHARGER`, `CONF_CHARGER_ID` / `CONF_CHARGER_PRIORITY`. Renaming the stored config strings (`entry_type` value `"charger"`, `charger_priority`, `charger_id`) needs an `async_migrate_entry` bump, with the ~25 scenario YAMLs (`chargers:` / `charger_N` keys), 11 pytest fixtures, `strings.json` and both translations updated in lockstep. **Open decision when picked up:** whether to also rename genuinely EVSE-specific names (`_build_evse_charger`, OCPP `discover_chargers`, `CONF_CHARGER_L*_PHASE`, config-flow `charger_info`/`charger_current`/`charger_timing` steps, `validate_charger_settings`) or keep them — an EVSE genuinely _is_ a charger.

## Other

- [ ] **Icon submission** — Submit `icon.png` to [HA brands repo](https://github.com/home-assistant/brands) (see dev/ISSUES.md)
