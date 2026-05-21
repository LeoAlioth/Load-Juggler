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
- [ ] **Split operating-mode constants per device type** — the operating modes in `const.py` are a mess: a single flat list of `OPERATING_MODE_*` strings shared across EVSE / plug / hot water tank, with the per-type sets (`OPERATING_MODES_EVSE`, `OPERATING_MODES_PLUG`, `OPERATING_MODES_HOT_WATER_TANK`) and the tank→engine-mode mapping scattered around them. Some labels mean different things per type (e.g. tank "Solar Only" maps to engine `Solar Priority`; "Normal"/"Freeze Protection" are tank-only), and `const.py` is getting long. Split the operating-mode definitions into a clear per-device-type structure — ideally its own module — so each device type owns its modes, default, and engine-mode mapping in one place.

## Other

- [ ] **Icon submission** — Submit `icon.png` to [HA brands repo](https://github.com/home-assistant/brands) (see dev/ISSUES.md)
