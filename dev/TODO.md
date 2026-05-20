# Load Juggler — TODO

## In Progress

_Nothing in progress — see "Bugs" below for the active work queue._

> All four former "In Progress" items are now done: grace period in EVSE/plug config,
> escalation to hard reset, and the grace-period countdown in status (all three found
> already implemented during the 2026-05-20 audit), plus the EVSE phase-mask entity —
> `LoadJugglerPhaseMaskSensor`, created only for 3-phase EVSEs, showing the live
> site-phase mask (e.g. `A` / `AB` / `ABC`, `Idle` when not drawing).

## Bugs — Codebase Audit 2026-05-20

Static audit of all ~9,600 lines. The 3 Critical and all 9 High-severity items
were fixed on 2026-05-20; the 7 Medium items below remain open.

### Medium — open

- [ ] **Two smart loads default to `entity_id="lj_smart_load"`** → identical `unique_id`s → HA silently drops the second plug's entities. Config flow doesn't validate `CONF_ENTITY_ID` uniqueness (same for a 2nd hub / circuit group).
- [ ] **No hysteresis on `excess_export_threshold`** — battery SOC has hysteresis, the export threshold doesn't → charger contactor chatter when export hovers near the threshold.
- [ ] **Circuit-group pool zeroes unmetered phases** (`target_calculator.py:165`) — on a partially-metered site (CT on phase A only), a 3-phase charger in a group is capped to 0.
- [ ] **`_migrate_hub_entities_if_needed` calls `async_update_entry` unconditionally** on every startup → at least one extra full hub reload on first boot. Guard with an "only if changed" check.
- [ ] **Auto-detect mismatch notification can re-fire repeatedly** (`auto_detect.py` `_evaluate_score`) — score decay resets `notify_sent_1ph` so the same notification spams on noisy multi-phase sites.
- [ ] **Service handlers don't enforce min ≤ max** — `set_max_current`/`set_min_current` can set min above max; the engine then sees `min > max`.
- [ ] **`compliance.py:100` tolerance uses command freq, not site freq** — a legitimately ramping charger can be flagged non-compliant and auto-reset mid-ramp, fighting the smoothing logic.

## Backlog

- [ ] **Device-based OCPP discovery** — select OCPP device instead of entity, auto-find all entities (supports per-phase separate entities)
- [ ] **SG Ready device type** — 2-relay site-state mapping (Block/Normal/Recommend ON/Force ON), no user modes

### Hot Water Tank Device Type — Detailed Implementation Plan

**Overview:** Add support for hot water tanks with resistive heating elements controlled via thermostat. Unlike EVSE (variable current) or plugs (fixed power), hot water tanks are temperature-controlled binary loads (on/off) with hysteresis.

> **⚠️ Plan needs remapping after the sensor.py refactor (2026-05-20).** The file/function
> map below predates the split of `sensor.py` into `hub.py` / `load.py` / `load_sensors.py` /
> `ocpp.py` / `plug.py` / `status.py` etc. Before implementing:
>
> - **Phase 2** — there is no `_compute_charger_ceiling()` and no per-device "ceiling
>   function" pattern. The engine uses `_source_limit()` in `target_calculator.py` and has
>   **no `device_type` branching at all**. Binary on/off conversion is done in the HA layer
>   (`plug.py`), not the calc engine. Either add `device_type` branching to `_source_limit`,
>   or model the tank as "a plug with a thermostat gate" entirely in the HA layer.
> - **Phase 3.3** — switch on/off control already lives in `plug.py` (`plug.py:22-32`), not
>   `dynamic_ocpp_evse.py`. Put tank switch control there or in a new module.
> - **Phase 4.1** — "sensor.py — new sensors" is stale; charger/load sensors now live in
>   `load.py` / `load_sensors.py`.
> - `const.py` and `calculations/models.py` (`LoadContext`) references are still accurate.

---

#### Phase 1: Data Model & Constants

##### 1.1 const.py — New constants

```python
# Device type
DEVICE_TYPE_HOT_WATER_TANK = "hot_water_tank"

# Configuration keys
CONF_THERMOSTAT_ENTITY_ID = "thermostat_entity_id"      # HA climate entity or temperature sensor
CONF_TEMPERATURE_SENSOR_ENTITY_ID = "temperature_sensor_entity_id"  # Optional: separate temp sensor
CONF_TARGET_TEMPERATURE = "target_temperature"          # Target water temp (°C)
CONF_TEMPERATURE_HYSTERESIS = "temperature_hysteresis"  # Deadband (°C, default 5)
CONF_HEATING_ELEMENT_POWER = "heating_element_power"    # Fixed power rating (W)
CONF_HEATING_ELEMENT_PHASES = "heating_element_phases"  # Phase connection (A, AB, ABC, etc.)

# Operating modes for hot water tank
OPERATING_MODE_NORMAL = "Normal"        # Heat from any source when temp below target
OPERATING_MODE_BOOST = "Boost"          # Force heating regardless of solar/temp
# Solar Only and Excess inherited from existing modes

OPERATING_MODES_HOT_WATER_TANK = [
    OPERATING_MODE_NORMAL,
    OPERATING_MODE_BOOST,
    OPERATING_MODE_SOLAR_ONLY,
    OPERATING_MODE_EXCESS,
]

DEFAULT_TARGET_TEMPERATURE = 45  # °C
DEFAULT_HIGH_TEMP_TARGET = 80 # °C
DEFAULT_TEMPERATURE_HYSTERESIS = 1  # °C
DEFAULT_HEATING_ELEMENT_POWER = 2000  # W
```

##### 1.2 models.py — Extend LoadContext

```python
@dataclass
class LoadContext:
    # ... existing fields ...
    
    # Hot water tank specific (only used when device_type == "hot_water_tank")
    current_temperature: float | None = None      # Current water temperature (°C)
    target_temperature: float = 55.0              # Target temperature (°C)
    temperature_hysteresis: float = 5.0           # Deadband (°C)
    heating_element_power: float = 2000.0         # Fixed power (W)
    
    # Computed property for temperature control
    @property
    def needs_heating(self) -> bool:
        """Returns True if tank needs heating based on temperature and hysteresis."""
        if self.current_temperature is None:
            return False  # No sensor reading, don't heat
        threshold = self.target_temperature - self.temperature_hysteresis
        return self.current_temperature < threshold
```

---

#### Phase 2: Calculation Engine

##### 2.1 target_calculator.py — New ceiling function

Add `_compute_hot_water_tank_ceiling()` that returns binary decision (ON/OFF) converted to allocated_current:

```python
def _compute_hot_water_tank_ceiling(
    charger: LoadContext,
    site: SiteContext,
    physical: PhaseConstraints,
    solar: PhaseConstraints,
    excess: PhaseConstraints,
) -> float:
    """Compute allocated current for hot water tank (binary ON/OFF).
    
    Returns:
        0.0 = OFF (don't heat)
        charging_current = ON (heat at element's rated power)
    """
    mode = charger.operating_mode
    mask = charger.active_phases_mask
    
    # Temperature check (except Boost mode)
    if mode != OPERATING_MODE_BOOST:
        if not charger.needs_heating:
            return 0.0  # Temperature satisfied, don't heat
    
    # Convert heating element power to current
    element_current = charger.heating_element_power / site.voltage
    
    # Mode-specific logic
    if mode == OPERATING_MODE_BOOST:
        # Always ON if temperature not at target, regardless of power source
        if charger.needs_heating:
            phys_avail = physical.get_available(mask)
            return min(element_current, phys_avail)
        return 0.0
    
    if mode == OPERATING_MODE_NORMAL:
        # Heat from any available source when temperature is low
        phys_avail = physical.get_available(mask)
        if phys_avail >= element_current:
            return element_current
        return 0.0
    
    if mode == OPERATING_MODE_SOLAR_ONLY:
        # Heat only from solar surplus
        solar_avail = solar.get_available(mask)
        if solar_avail >= element_current:
            return element_current
        return 0.0
    
    if mode == OPERATING_MODE_EXCESS:
        # Heat only when export exceeds threshold
        excess_avail = excess.get_available(mask)
        if excess_avail >= element_current:
            return element_current
        return 0.0
    
    return 0.0
```

##### 2.2 target_calculator.py — Update `_compute_charger_ceiling()`

Add branch for hot_water_tank device type:

```python
def _compute_charger_ceiling(...):
    if charger.device_type == DEVICE_TYPE_HOT_WATER_TANK:
        return _compute_hot_water_tank_ceiling(charger, site, physical, solar, excess)
    # ... existing EVSE/plug logic ...
```

**2.3 target_calculator.py — Mode urgency for Normal/Boost**

```python
MODE_URGENCY = {
    # ... existing modes ...
    OPERATING_MODE_BOOST: 0,      # Same urgency as Standard (highest)
    OPERATING_MODE_NORMAL: 1,     # Same urgency as Solar Priority
}
```

---

#### Phase 3: HA Integration Layer

##### 3.1 config_flow.py — New config step

Add `async_step_hot_water_tank_config()` with schema:

- Name + entity_id (standard fields)
- Thermostat entity selector (climate domain) OR temperature sensor selector
- Target temperature (number slider, 30-80°C)
- Temperature hysteresis (number slider, 1-15°C)
- Heating element power (number, 500-6000W)
- Phase connection (select: A, B, C, AB, BC, AC, ABC)
- Switch entity to control heating element (required)
- Priority (1-10)
- Operating mode (select)
- Grace period (for Solar/Excess modes)

##### 3.2 dynamic_ocpp_evse.py — Temperature reading

Add `_read_tank_temperature()` helper:

```python
def _read_tank_temperature(hass, config: dict) -> float | None:
    """Read current tank temperature from climate entity or sensor."""
    thermostat_id = config.get(CONF_THERMOSTAT_ENTITY_ID)
    if thermostat_id:
        state = hass.states.get(thermostat_id)
        if state and state.state not in ("unknown", "unavailable"):
            # Climate entity: get current_temperature attribute
            if state.domain == "climate":
                return state.attributes.get("current_temperature")
            # Sensor entity: state is the temperature
            try:
                return float(state.state)
            except (ValueError, TypeError):
                return None
    return None
```

##### 3.3 dynamic_ocpp_evse.py — Switch control

Add `_write_hot_water_tank_state()` helper:

```python
def _write_hot_water_tank_state(charger: LoadContext, turn_on: bool) -> None:
    """Turn hot water tank heating element on/off via switch entity."""
    switch_entity = charger.extra_state.get("switch_entity_id")
    if switch_entity:
        service = "switch.turn_on" if turn_on else "switch.turn_off"
        hass.services.async_call("switch", service.split(".")[1], 
                                  {"entity_id": switch_entity})
```

---

#### Phase 4: Entities & UI

##### 4.1 sensor.py — New sensors

- Hot Water Tank Status sensor (attribute: current_temperature, target_temperature)
- Allocated Power sensor (0 or element_power)

##### 4.2 select.py — Operating mode selector

- Reuse existing `OperatingModeSelect` with `OPERATING_MODES_HOT_WATER_TANK`

##### 4.3 number.py — Configuration sliders

- Target Temperature (30-80°C, default 55°C)
- Temperature Hysteresis (1-15°C, default 5°C)

---

#### Phase 5: Tests

##### 5.1 Unit tests — calculations/**

Create `test_hot_water_tank.py`:

- Test temperature hysteresis (heat below target - hysteresis, stop at target)
- Test Solar Only mode (only heat when solar available)
- Test Excess mode (only heat when export > threshold)
- Test Normal mode (heat whenever power available, temperature permitting)
- Test Boost mode (always heat until target reached)

##### 5.2 Scenario tests — dev/tests/scenarios/

Create `features/test_hot_water_tank.yaml`:

- Tank at 40°C, target 55°C, solar available → ON in Normal mode
- Tank at 55°C (target) → OFF
- Tank at 53°C, hysteresis 5°C → OFF (within deadband)
- Tank at 48°C, hysteresis 5°C, target 55°C → ON (below 50°C threshold)
- Solar Only mode, no solar → OFF
- Solar Only mode, solar surplus → ON
- Excess mode, export below threshold → OFF
- Excess mode, export above threshold → ON

---

#### Phase 6: Documentation

##### 6.1 CHARGE_MODES_GUIDE.md — Add Hot Water Tank section

```markdown
## Hot Water Tank Modes

| Mode | Behavior |
| -----|----------|
| **Normal** | Heat from any power source when temperature below target-hysteresis |
| **Boost** | Always heat (ignores solar constraints, respects temperature limit) |
| **Solar Only** | Heat only from solar surplus when temperature low |
| **Excess** | Heat only when export exceeds threshold |
```

##### 6.2 README.md — Add hot water tank to supported devices

##### 6.3 Translations — en.json, sl.json

Add translation keys for:

- Device type name: "Hot Water Tank"
- Mode names and descriptions
- Configuration field labels

---

#### Implementation Order

1. **Phase 1** — Constants and data model (foundation)
2. **Phase 2** — Calculation engine (pure Python, testable)
3. **Phase 3** — Config flow and HA integration
4. **Phase 4** — Entities and UI
5. **Phase 5** — Tests
6. **Phase 6** — Documentation

---

#### Key Design Decisions

| Decision | Rationale |
| -------- | --------- |
| Binary output (ON/OFF) | Resistive heating elements cannot modulate power |
| Temperature hysteresis | Prevents rapid cycling, extends element life |
| Fixed power rating | Simpler than variable current allocation |
| Reuse Solar/Excess modes | Consistent with existing plug behavior |
| New Normal/Boost modes | Unique to temperature-controlled loads |
| Switch entity control | Compatible with any HA-controllable relay/thermostat |

---

#### Out of Scope (Future Enhancements)

- Heat pump water heaters (variable power, COP awareness)
- Multiple heating elements (staged control)
- Schedule-based heating (time-of-use optimization)
- Grid tariff integration (heat when electricity is cheap)

## Other

- [ ] **Icon submission** — Submit `icon.png` to [HA brands repo](https://github.com/home-assistant/brands) (see dev/ISSUES.md)
