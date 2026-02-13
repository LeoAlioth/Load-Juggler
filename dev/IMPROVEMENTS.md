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
**Status:** Not yet implemented  
**Complexity:** High  

### Current State
- Charging mode is set per-site (hub)
- All chargers under a hub use the same mode

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
```
Combine priority and mode into a single ranking:

rank = (priority * 10) + mode_priority

Where mode_priority:
- Standard = 4
- Eco = 3  
- Solar = 2
- Excess = 1

Example:
- Charger 1: priority=1, Eco → rank = 1*10 + 3 = 13
- Charger 2: priority=2, Standard → rank = 2*10 + 4 = 24

Charger 1 charges first (lower rank wins)
```

**Proposed Solution 2: Mode as Power Level Constraint**
```
Each mode defines a "target" current level:
- Standard: max_current
- Eco: min_current when no solar, max when solar available
- Solar: whatever solar can provide (capped at max)
- Excess: only activates above threshold

Distribution then maximizes the sum of:
- priority_weight * (allocated / target)
```

#### Problem 2: Solar Mode with Mixed Chargers
**Scenario:** Charger 1 = Solar, Charger 2 = Standard  
**Question:** If solar is available but Charger 1 isn't using it all, should Charger 2 get the remainder?

**Analysis:** This depends on whether you want to prioritize "solar self-consumption" or "charger priority."

**Proposed Solution: Mode-Based Pooling**
```
Separate power pools by mode:

1. Solar pool = available solar power (after battery charge)
   - Available to: Solar, Eco modes
   - Priority order within pool

2. Grid pool = available grid power up to breaker limit
   - Available to: Standard, Eco, Excess modes
   - Priority order within pool

3. Excess pool = export above threshold
   - Available to: Excess mode only
```

#### Implementation Steps (for future)
1. Add `charging_mode` field to `ChargerContext`
2. Modify `_determine_target_power()` to accept per-charger modes
3. Update distribution functions to handle mixed-mode constraints
4. Create new constraint types for each mode's power pool

---

## Adding support for "dumb" EVSE - smart sockets
**Status:** Not yet implemented  
**Complexity:** Medium  

### Use Cases
- 3-phase smart EVSE + single-phase "granny charger" on smart plug
- Smart plug without energy monitoring (just on/off)
- Smart plug with energy monitoring (actual power reading)

### Configuration Changes Needed

#### For Plugs Without Power Monitoring
```
New config fields for charger entry:
- device_type: evse | plug
- device_power_rating_watts: int (for non-monitoring plugs)

Behavior:
- Use device_power_rating_watts as both min_current and max_current
- When allocated >= minimum, turn plug ON
- When allocated < minimum, turn plug OFF
```

#### For Plugs With Power Monitoring
```
New config fields:
- device_power_entity_id: sensor entity with actual power draw
- use_actual_consumption: boolean (default true)

Behavior:
- Read actual power from sensor
- Track consumption during charging session
- Update max_current dynamically based on measured load
```

### Implementation Approach

**Step 1: Extend ChargerContext**
```python
@dataclass
class ChargerContext:
    device_type: str = "evse"  # evse or plug
    device_power_rating_watts: float | None = None  # For non-monitoring plugs
    device_power_entity_id: str | None = None  # For monitoring plugs
    
    @property
    def min_current(self) -> float:
        if self.device_type == "plug" and self.device_power_rating_watts:
            return self.device_power_rating_watts / voltage
        return self._min_current
        
    @property
    def max_current(self) -> float:
        # If monitoring, use measured power + some margin
        if self.device_type == "plug" and self.device_power_entity_id:
            actual_power = read_sensor(self.device_power_entity_id)
            return actual_power / voltage * 1.2  # 20% safety margin
        return self._max_current
```

**Step 2: Add Plug Control Entity**
- Create `switch.{charger_id}_enabled` for non-monitoring plugs
- When allocated_current >= min_current → switch ON
- When allocated_current < min_current → switch OFF

**Step 3: Handle Status Reporting**
- Plugs without monitoring: status = "Charging" when switch is ON
- Plugs with monitoring: status derived from actual power draw

### Example Configuration

```
Charger 1 (Main EVSE - 3-phase):
- device_type: evse
- max_current: 32A
- priority: 1

Charger 2 (Granny Charger - smart plug w/o monitoring):
- device_type: plug  
- device_power_rating_watts: 1500W (≈6.5A)
- priority: 2

Behavior:
- Solar available = 7kW total
- Main EVSE gets first 32A per phase (up to its limit)
- If remainder > 6.5A, granny charger turns ON
```

---

## Making this a general load management project
**Status:** Not yet implemented  
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

### Implementation Path

**Phase 1: Smart Plug Support** (medium effort)
- Add plug device type to ChargerContext
- Control non-EV devices via existing switch infrastructure
- Treat them as "loads" with fixed power ratings

**Phase 2: General Load Type** (higher effort)
- Create separate `Load` entity type alongside `Charger`
- Each load has its own control entities
- Unified priority-based distribution across all loads

**Phase 3: Temperature-Based Control** (high effort)
- Read temperature sensors
- Implement thermal models
- Schedule heating/cooling based on excess availability

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

## Adding an entity selection for actual solar power in the config_flow
**Status:** Not yet implemented  
**Complexity:** Low  

### Current State
Solar production is derived from: `consumption + export_current`

This works well when you have a single meter at the grid connection point.

### When Dedicated Solar Entity Is Useful

1. **Inverter-side metering**: Some systems report solar production separately from grid consumption
2. **Multiple inverters**: Need to know which solar is available vs grid power
3. **Better excess detection**: Direct solar measurement can be more accurate than derived values

### Potential Improvements with Dedicated Entity

**Current approach (derived):**
```
Solar = Consumption + Export
→ Could overestimate if grid import happens simultaneously
→ Could underestimate if inverter has internal consumption
```

**With dedicated solar entity:**
```
Solar = direct measurement
Excess = Solar - Consumption (more accurate for self-consumption optimization)
```

### Implementation

Add optional config field:
```python
CONF_SOLAR_POWER_ENTITY_ID: "sensor.{entity_id}_solar_power"
```

When provided, use this directly instead of deriving from consumption+export.

### Recommendation

**Low priority implementation** - useful but not essential. The derived approach works well for most sites, and having a dedicated solar meter isn't universal.