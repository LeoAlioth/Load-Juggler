# Improvements

This document is used for keeping notes of ideas for future implementations in no particular order. As long as the developer does not expicitly say to start implementing them, you can just use this as a reference for what might come, if that has any effect on current decisions. This will also keep any discussions about ideas. This way you can plan them easier with the developer.

## Supports for chargers with multiple plugs
**Status:** Not yet implemented
**Complexity:** Medium

### Current State
- Each charger is a single config entry with one OCPP connection
- One physical EVSE = one HA configuration entry

### Implementation Approach
Add `plug_id` field to `ChargerContext`. Multiple plugs can:
- Share the same OCPP connection (if the charger supports multiple concurrent sessions)
- Have separate current/power sensors for each plug
- Be configured with individual priorities and modes

### Technical Considerations
1. **OCPP Protocol**: Check if your chargers support multi-session charging via OCPP 1.6J or need OCPP 2.0
2. **Separate entity mapping**: Each plug needs its own sensor entities for current, power, status
3. **Hardware limitation**: Most household EVSEs charge one car at a time (sequential), not concurrent

### Proposed Solution A: Sequential Charging (Simpler)
```
Configuration:
- 1 physical charger with 2 plugs = 2 HA entries pointing to same OCPP device
- Shared max_current constraint from the physical unit
- Priority determines which plug gets charge first

Behavior:
- Plug 1 (priority 1): Gets allocated first, up to max
- Plug 2 (priority 2): Only charges if Plug 1 isn't using full capacity
```

### Proposed Solution B: Concurrent Charging (More Complex)
```
Configuration:
- 1 HA entry with multiple plug configurations
- Each plug has independent current sensors
- OCPP connection supports multiple sessions simultaneously

Behavior:
- Both plugs can charge at the same time, sharing total available power
- Each plug tracks its own session state independently
```

---

## Moving charge mode selection from the site to the individual EVSE
**Status:** Not yet implemented (const `CONF_CHARGER_CHARGING_MODE_ENTITY_ID` declared but unused)
**Complexity:** High

### Current State
- Charging mode is set per-site (hub)
- All chargers under a hub use the same mode
- `CONF_CHARGER_CHARGING_MODE_ENTITY_ID` exists in const.py as a placeholder

### Design Questions & Solutions

#### Problem 1: Mixed Mode Priority Conflicts
**Scenario:** Charger 1 (priority 1) = Eco, Charger 2 (priority 2) = Standard
**Question:** If very little power is available, should Charger 1 get minimum rate while Charger 2 gets more?

**Analysis:**
- Priority mode currently orders by charger priority number (lower = higher priority)
- With mixed modes, we have two competing orderings:
  - `priority`: which charger goes first
  - `mode urgency`: Standard > Eco > Solar > Excess

**Proposed Solution 1: Mode-Aware Priority**
We take charging modes as a priority, and only use the priority numbers to solve within the same charge mode.
So a charger on Standard mode, always gets priority over a charger in eco mode.

#### Implementation Steps (for future)
1. Add `charging_mode` field to `ChargerContext`
2. Modify `_determine_target_power()` to accept per-charger modes
3. Update distribution functions to handle mixed-mode constraints
4. Create new constraint types for each mode's power pool
5. Wire up config flow to use `CONF_CHARGER_CHARGING_MODE_ENTITY_ID`
6. Create per-charger charging mode select entity

---

## ~~Adding support for "dumb" EVSE - smart sockets~~
**Status:** Implemented (v2.0)

Implemented as TODO item 13 ("Smart Plug / Relay Support"). Key decisions made:
- `device_type` field on charger entries: `"evse"` or `"plug"`
- Plugs modeled as `ChargerContext` with `min_current == max_current == power_rating / (voltage * phases)`
- Binary on/off behavior falls out naturally from the engine's min-current threshold
- Config flow has device type selection step, plug-specific config with `_plug_schema()`
- `sensor.py` branches on device_type — plugs use `switch.turn_on/off`, EVSEs use OCPP profiles
- Supports `connected_to_phase` for phase-aware allocation
- Optional power monitoring entity for more reliable status detection
- 6 test scenarios in `test_scenarios_plugs.yaml`, all passing

---

## Making this a general load management project
**Status:** Phase 1 complete (smart plug support), Phases 2-3 not started
**Complexity:** High (but incremental path possible)

### Vision
Extend beyond EV charging to manage any controllable load:
- Hot water boilers (thermal storage)
- HVAC systems (space heating/cooling)
- Water pumps
- Other flexible appliances

### Why It Fits Well

The current architecture is already quite general:

| Current EVSE Concept | General Load Equivalent |
|---------------------|------------------------|
| `ChargerContext` | `LoadContext` |
| `min_current` / `max_current` | `min_power` / `max_power` |
| Charging mode | Control strategy |
| Priority distribution | Load prioritization |

### Progress

**Phase 1: Smart Plug Support** — Done. See "Adding support for smart sockets" above.

**Phase 2: General Load Type** (higher effort, not started)
- Create separate `Load` entity type alongside `Charger`
- Each load has its own control entities
- Unified priority-based distribution across all loads

**Phase 3: Temperature-Based Control** (high effort, not started)
- Read temperature sensors
- Implement thermal models
- Schedule heating/cooling based on excess availability

### Control Strategies for Non-EV Loads

#### Hot Water Tank
```
Normal operation:
- Target temp: 45°C
- Hysteresis: ±3°C (cycles between 42-48°C)

Excess mode:
- Boost to 70°C (uses excess power)
- Extended hot water availability

Power allocation:
- Minimum: keep at baseline temp
- Normal: maintain target range
- Excess: heat beyond target for storage
```

#### HVAC System
```
Normal operation:
- Target temp: 21°C
- Hysteresis: ±0.5°C

Excess mode:
- Pre-cool to 19°C (summer) or pre-heat to 23°C (winter)
- Uses excess power for thermal mass management

Thermal parameters needed:
- Thermal time constant (how fast temp changes)
- Heat loss rate (insulation quality)
- Max heating/cooling capacity
```

### Proposed Load Context Extension

```python
@dataclass
class LoadContext:
    load_id: str
    entity_id: str  # Control entity (switch, number, input_select)

    # Power limits
    min_power_watts: float
    max_power_watts: float

    # Thermal properties (for temperature-based loads)
    thermal_time_constant_hours: float | None = None
    target_temperature_c: float | None = None
    temperature_hysteresis_c: float = 1.0

    # Current state
    current_power_watts: float = 0
    current_temperature_c: float | None = None

    # Mode-specific behavior
    excess_mode_temp_offset_c: float = 2.0  # How much to deviate in excess mode
```

### Example Configuration

```
Hub Configuration:
- Distribution mode: Priority
- Charging mode: Excess (only when export > 10kW)

Loads:
1. Hot Water Tank
   - max_power: 3500W
   - priority: 1
   - target_temp: 45°C
   - excess_temp_boost: 25°C

2. Heat Pump
   - max_power: 2000W
   - priority: 2
   - target_temp: 21°C
   - excess_temp_offset: -2°C (pre-cool)

3. EV Charger
   - max_current: 32A
   - priority: 3

Behavior:
- If export = 15kW, first allocate to hot water tank (3.5kW)
- Remaining 11.5kW → heat pump gets 2kW
- Remaining 9.5kW → EV charger starts if car connected
```

---

## ~~Adding an entity selection for actual solar power in the config_flow~~

**Status:** Implemented (v2.0)

Implemented as TODO item 12 ("Dedicated Solar Power Entity"). Key decisions made:
- Added `CONF_SOLAR_PRODUCTION_ENTITY_ID` as optional field in hub grid schema
- When configured, `dynamic_ocpp_evse.py` reads the solar entity directly instead of deriving from `consumption + export`
- Dropdown in config flow lists power-class sensor entities
- Falls back to the derived approach when the entity is not configured
- 3 test scenarios in `test_scenarios_solar_entity.yaml`, all passing
