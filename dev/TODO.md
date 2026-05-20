# Load Juggler — TODO

## In Progress

_Nothing in progress — next up is the Backlog below._

> The 2026-05-20 codebase audit is fully resolved: 3 Critical, 9 High and 7 Medium
> bugs all fixed and verified (141 calc-scenario + 93 HA-layer tests pass). The EVSE
> phase-mask entity (`LoadJugglerPhaseMaskSensor`, 3-phase EVSEs only) was also added.

## Backlog

- [ ] **Device-based OCPP discovery (refactor)** — discovery already works and auto-finds per-phase L1/L2/L3 entities, but by _guessing_ sibling entity names from a `base_name` prefix (`_discover_ocpp_chargers` in `config_flow.py`), and the OCPP device ID is a free-text field. Replace with a device-registry **device selector** + enumerate entities via `entity.device_id`, so it's robust to non-standard OCPP entity naming and supports per-phase separate entities reliably.
- [ ] **SG Ready device type** — 2-relay site-state mapping (Block/Normal/Recommend ON/Force ON), no user modes

### Hot Water Tank Device Type — Implementation Plan (rev. 2026-05-20)

**Overview:** Add a `hot_water_tank` device type — a binary (on/off) load driven through
a Home Assistant `climate` entity (e.g. a Generic Thermostat). Mixable with EVSEs and
smart loads on the same hub.

**Core design — "a plug that speaks `climate`":** the climate entity owns _all_
temperature regulation (current temperature, setpoint, hysteresis via cold/hot tolerance,
min cycle duration). Load Juggler only does power management — exactly two writes and one
read on the climate entity:

| Op | Action | Purpose |
| -- | ------ | ------- |
| write | `climate.set_temperature` → mode target | Normal vs Boost = a different setpoint |
| write | `climate.set_hvac_mode` → `heat` / `off` | `heat` = heating permitted; `off` = forbidden |
| read | `hvac_action` attribute | `heating` = drawing; `idle` = satisfied (free the power) |

The calculation engine treats the tank as a smart load (plug) — **no engine changes**.

#### Design decisions

| Decision | Rationale |
| -------- | --------- |
| Delegate all temperature logic to the climate entity | The Generic Thermostat already does hysteresis, min-cycle, sensor reading — don't reimplement it |
| Engine treats the tank as a plug (`power_rating` = element power) | Binary load identical to a smart load; zero engine churn |
| Normal vs Boost = setpoint only | Normal heats to a baseline target, Boost to a high target; same priority |
| `connector_status` derived from `hvac_action` | `idle` → inactive so the engine reallocates power away from an already-hot tank; self-corrects within a cycle |
| Element power: fixed W + optional live power entity/device | Mirrors the smart-load power-monitor pattern; live measurement preferred when present |

#### Mode → engine-mode mapping (done in the HA layer)

| Tank mode | Engine mode | Setpoint written |
| --------- | ----------- | ---------------- |
| Normal | `Continuous` | baseline target |
| Boost | `Continuous` | boost target |
| Solar Only | `Solar Only` | baseline target |
| Excess | `Excess` | baseline target |

---

#### Phase 1 — Constants (`const.py`)

```python
DEVICE_TYPE_HOT_WATER_TANK = "hot_water_tank"

CONF_CLIMATE_ENTITY_ID = "climate_entity_id"          # climate entity (read + control)
CONF_HEATING_ELEMENT_POWER = "heating_element_power"  # fixed element rating (W) — fallback
CONF_TANK_POWER_ENTITY_ID = "tank_power_entity_id"    # optional power sensor (live draw)
CONF_TANK_POWER_DEVICE_ID = "tank_power_device_id"    # optional: device whose power sensor we resolve
CONF_TANK_TARGET_TEMPERATURE = "tank_target_temperature"  # Normal/Solar/Excess setpoint
CONF_TANK_BOOST_TEMPERATURE = "tank_boost_temperature"    # Boost setpoint

OPERATING_MODE_NORMAL = "Normal"
OPERATING_MODE_BOOST = "Boost"
OPERATING_MODES_HOT_WATER_TANK = [
    OPERATING_MODE_NORMAL, OPERATING_MODE_BOOST,
    OPERATING_MODE_SOLAR_ONLY, OPERATING_MODE_EXCESS,
]

DEFAULT_TANK_TARGET_TEMPERATURE = 45  # °C
DEFAULT_TANK_BOOST_TEMPERATURE = 80   # °C
DEFAULT_HEATING_ELEMENT_POWER = 2000  # W
```

Phase connection reuses the existing `CONF_CONNECTED_TO_PHASE`. **No `LoadContext` or
calculation-engine changes** — the tank is a plug to the engine.

#### Phase 2 — Config flow (`config_flow.py`)

- Add "Hot Water Tank" as a third device-type option on the device-type selection step.
- New `async_step_hot_water_tank_config()` — schema:
  - Name + entity_id (reuse the `_entity_id_in_use` uniqueness check)
  - Climate entity selector (`domain: climate`) — required
  - Element power (number, W) **+** optional power-sensor entity selector
    (`device_class: power`) **+** optional device selector. A picked device is resolved
    to its `device_class: power` entity at config time and stored as
    `CONF_TANK_POWER_ENTITY_ID`, so runtime only ever sees one entity + the fallback W.
  - Phase connection (`CONF_CONNECTED_TO_PHASE`), operating mode, priority.
- Reconfigure step (`async_step_reconfigure_hot_water_tank`).
- Target / Boost temperatures are runtime number sliders (Phase 5), not config-flow fields.

#### Phase 3 — Tank LoadContext builder (`dynamic_ocpp_evse.py`)

`_build_hot_water_tank_charger()` — mirrors `_build_plug_charger`:

- Read the climate entity's `hvac_action` attribute.
- `connector_status` = inactive when `hvac_action == "idle"`, else active.
- `power_rating` = live power-entity reading when available, else `CONF_HEATING_ELEMENT_POWER`.
- Map the user's tank mode → engine operating mode (table above).
- **No temperature read, no hysteresis** — the climate entity owns that.

#### Phase 4 — Tank command (`hot_water_tank.py`, new module)

`send_hot_water_tank_command()` — mirrors `plug.py` `send_plug_command`, throttled:

- Heating permitted (engine allocation ≥ threshold): `climate.set_hvac_mode → heat`
  **and** `climate.set_temperature` to the active mode's target.
- Otherwise: `climate.set_hvac_mode → off`.
- Only call the climate services when the desired state actually changes.
- `load.py` — add a dispatch branch for `DEVICE_TYPE_HOT_WATER_TANK` alongside EVSE/plug.

#### Phase 5 — Entities (`load_sensors.py`, `number.py`, `select.py`)

- **Status sensor** (`load_sensors.py`) — state e.g. `Heating` / `Idle` / `Off`;
  attributes: current temperature, active setpoint, mode.
- **Number sliders** (`number.py`) — Target Temperature, Boost Temperature.
- **Mode select** (`select.py`) — reuse `OperatingModeSelect` with `OPERATING_MODES_HOT_WATER_TANK`.
- Tank entries are `ENTRY_TYPE_CHARGER` with `device_type = hot_water_tank`, so they flow
  through the existing charger platform setup in `__init__.py` / `sensor.py`.

#### Phase 6 — Tests, translations, docs

- **Scenario tests** (`dev/tests/scenarios/features/`) — tank as a plug-like load across
  Normal / Boost / Solar Only / Excess; verify it competes for power and goes inactive
  when `hvac_action == idle`.
- **HA-layer tests** — new config-flow step + entity-id uniqueness; the
  `_build_hot_water_tank_charger` mode mapping; `send_hot_water_tank_command` climate
  service calls. Run via `dev/Dockerfile.test`.
- **Translations** — `strings.json`, `translations/en.json`, `translations/sl.json`.
- **Docs** — `CHARGE_MODES_GUIDE.md` (tank modes table), `README.md` (supported devices).

#### Out of scope

- Heat-pump water heaters (variable power / COP awareness)
- Multiple staged heating elements
- Schedule- or tariff-aware heating

## Other

- [ ] **Icon submission** — Submit `icon.png` to [HA brands repo](https://github.com/home-assistant/brands) (see dev/ISSUES.md)
