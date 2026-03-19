# Load Juggler — TODO

## In Progress

### Add solar/excess mode grace period to EVSE and plug configuration

### integration already automatically calls reset occp profile if weird behaviour is detected. If that does not help, an escalation - reset the evse completely should be triggered

### Expose current phase mask on EVSE as a separate entity (on 3 phase evse only)

### update evse/plug status, to show that the grace period timer is running (countdown untill it stops)

## Backlog

- [ ] **Device-based OCPP discovery** — select OCPP device instead of entity, auto-find all entities (supports per-phase separate entities)
- [ ] **SG Ready device type** — 2-relay site-state mapping (Block/Normal/Recommend ON/Force ON), no user modes

### Hot Water Tank Device Type — Detailed Implementation Plan

**Overview:** Add support for hot water tanks with resistive heating elements controlled via thermostat. Unlike EVSE (variable current) or plugs (fixed power), hot water tanks are temperature-controlled binary loads (on/off) with hysteresis.

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

DEFAULT_TARGET_TEMPERATURE = 55  # °C
DEFAULT_TEMPERATURE_HYSTERESIS = 5  # °C
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

## Completed (107 items)

1. PhaseValues → Optional[float] + helpers
2. Available current feature
3. 1-phase grid limit bug fix + test scenarios
4. SiteContext.num_phases → derived property
5. Dedicated Solar Power Entity
6. Smart Load support (device_type evse/plug)
7. Charge rate unit detection via OCPP
8. Distribution mode string mismatch fix
9. PhaseConstraints refactoring
10. Smart Load UX improvements
11. Translations (en.json + sl.json)
12. Current rate limiting
13. Auto-reset for non-compliant chargers
14. Inverter configuration in config flow
15. Grid consumption feedback loop
16. Charge pause UX improvements
17. Dual-frequency update loop (site 5s, charger 15s)
18. Derived solar production formula fix
19. Multi-cycle test simulation + trace output
20. Battery-aware derived solar mode
21. Inverter output cap for battery discharge
22. Config flow restructuring + charger phase mapping
23. Per-phase inverter output entities + wiring topology
24. Test scenario reorganization (99 scenarios)
25. Eco mode fixes (night surplus, inactive charger minimums, battery min SOC)
26. Excess mode minimum current fallback
27. PhaseConstraints.normalize() + combination-field capping
28. Codebase refactoring (helpers, _read_entity, element_min/max)
29. Hide Phase B/C sensors on single-phase sites
30. HA service actions (set_charging_mode, set_distribution_mode, set_max_current)
31. User documentation / README.md rewrite
32. Entity selector UX
33. Auto-detection patterns (12 brand files)
34. Auto-detect battery + solar entities (SOC, power, charge/discharge)
35. Options flow auto-detection
36. Wiring topology auto-detect
37. Charger name prettification
38. EVSE charging status sensor
39. `data_description` help text — all steps
40. Number selectors with min/max/unit
41. Max import power → checkbox + slider + optional entity
42. Inverter power UX: battery power hint
43. stackLevel Decimal → int bug fix
44. Feedback loop per-phase draw clamping
45. Battery power sensor false-positive fix
46. Hub-aware charger config flow (hide unavailable phases)
47. Phase mapping help text update
48. Rename "Smart Plug" → "Smart Load"
49. Hub debug logging (raw reads, feedback, per-charger, allocation)
50. Fix entity state lookup bug — use `hass.data[DOMAIN]` shared store
51. Fix battery SOC target/min sliders not feeding into calculation + create missing MaxImportPowerSlider
52. Fix EVSE min/max current sliders missing value clamping
53. Remove unused `ButtonEntity` import from `__init__.py`
54. Connector status entity ID deduplication in sensor.py
55. Move `_read_inverter_output()` to module scope
56. Extract `HubEntityMixin` + `ChargerEntityMixin` into `entity_mixins.py`
57. Deduplicate `_write_to_hub_data` / `_write_to_charger_data` via mixin
58. Deduplicate `async_added_to_hass` restore pattern via `_restore_and_publish_number()`
59. Per-phase loops in `dynamic_ocpp_evse.py` (grid reads, feedback, headroom)
60. Split `run_hub_calculation()` into subfunctions (~560→~170 lines)
61. Move rate limiting from OCPP command to allocated current level
62. Hub sensor cleanup: Solar Surplus → Solar Available Power, deduplicate battery sensors
63. Debug log: show human-readable charger names instead of entry_id hashes
64. Hub sensor renames: shorter, consistent naming
65. Fix entity selector clearing: `suggested_value` instead of `default`
66. Fix options flow Submit → Next button on non-final steps
67. Add Sony Xperia phone exclusion to generic battery auto-detection patterns
68. Reload config entry on options change
69. EMA smoothing + Schmitt trigger dead band + faster site refresh (2s)
70. Input-level EMA smoothing on grid CT, solar, battery power, and inverter output
71. Debug log shows both raw and smoothed values
72. Auto-detect OCPP `MeterValueSampleInterval` for charger update frequency
73. Fix "Finishing"/"Faulted" connector status: treat as inactive
74. Auto-detect power monitoring sensor for smart plugs
75. Auto-detect grid CT inversion
76. Auto-detect phase mapping
77. Solar/Excess grace period (anti-flicker)
78. Charge pause duration unit change (seconds → minutes) with v2.1→v2.2 migration
79. Per-load operating modes — foundation
80. Per-load operating modes — calculation engine
81. Per-load operating modes — test scenarios (110 passing)
82. Per-load operating modes — HA integration
83. Per-load operating modes — translations & services
84. Rename ChargerContext → LoadContext
85. Fix case-insensitive OCPP phase attribute reading
86. Rename "Total EVSE Power" → "Total Managed Power"
87. Two-stage auto-detect phase mapping with swap logic
88. 2-phase car inactive line detection
89. Confidence-weighted auto-detect scoring
90. 10% clamping tolerance for W-based chargers
91. Fix W-based OCPP power multiplication
92. Per-device operating mode in debug logs
93. Charger targets log: show both allocated and available current
94. Expose `available_current` as sensor attribute in HA
95. Available Current sensor shows available (not allocated) current
96. Circuit Groups — shared breaker limits (9 test scenarios)
97. Grid CT stale detection with EMA holdover and 60s timeout
98. Site available power cap by `max_grid_import_power`
99. Resilience improvements — OCPP try-except, `_UNAVAILABLE` sentinel, NaN/inf guard, stale member filtering
100. Off-grid support — optional Phase A CT, unified solar derivation, inverter-based phase count fallback
101. Hub status sensor — config validation + runtime warnings
102. Cleanup — removed dead `car_phases` field, removed auto-detect state double-init
103. SuspendedEV handling — near-zero draw treated as inactive after 60s grace period
104. Battery SOC hysteresis — HA layer hysteresis with 14 boundary test scenarios
105. Charger finishing test scenarios — 8 scenarios for capacity redistribution
106. Fix test infrastructure — `minor_version=2`, Python 3.12 CI, pytest-homeassistant-custom-component>=0.13.110
107. Add HA integration tests to CI — calculation + pytest tests before releases

