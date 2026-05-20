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

- [ ] **Hot Water Tank — user docs** — update `CHARGE_MODES_GUIDE.md` (tank modes
  table: Freeze Protection / Normal / Solar Only and the away/normal/boost
  setpoints) and `README.md` (add Hot Water Tank to supported device types). The
  device type itself is implemented; only the user-facing markdown guides remain.
- [ ] **Device-based OCPP discovery (refactor)** — discovery already works and auto-finds per-phase L1/L2/L3 entities, but by _guessing_ sibling entity names from a `base_name` prefix (`_discover_ocpp_chargers` in `config_flow.py`), and the OCPP device ID is a free-text field. Replace with a device-registry **device selector** + enumerate entities via `entity.device_id`, so it's robust to non-standard OCPP entity naming and supports per-phase separate entities reliably.
- [ ] **SG Ready device type** — 2-relay site-state mapping (Block/Normal/Recommend ON/Force ON), no user modes

## Other

- [ ] **Icon submission** — Submit `icon.png` to [HA brands repo](https://github.com/home-assistant/brands) (see dev/ISSUES.md)
