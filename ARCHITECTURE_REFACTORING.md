# Architecture Refactoring - SiteContext and ChargerContext

## Overview

We've successfully refactored the calculation architecture to use a cleaner object-oriented approach with `SiteContext` and `ChargerContext` classes.

## ✅ What Was Completed

### 1. New Data Classes (`context.py`)

#### `SiteContext`
**Purpose**: Represents the entire electrical site (home/building)

**Key Fields:**
- **Grid/Power**: `voltage`, `main_breaker_rating`, `max_import_power`
- **Grid Currents**: `grid_phase_a_current`, `grid_phase_b_current`, `grid_phase_c_current`
- **Solar/Export**: `total_export_current`, `total_export_power`, `phase_x_export_current`
- **Battery**: `battery_soc`, `battery_power`, `battery_soc_target`, `battery_soc_min`
- **Site Available Power**: `site_grid_available_power`, `site_battery_available_power`
- **Chargers**: `chargers: list[ChargerContext]` - Array of all chargers at the site

#### `ChargerContext`
**Purpose**: Represents a single EVSE/charger

**Key Fields:**
- **Identity**: `charger_id`, `entity_id`
- **Configuration**: `min_current`, `max_current`, `phases`, `priority`
- **State**: `charging_mode`, `current_import`, `current_offered`
- **Calculated**: `target_current`, `allocated_current`, `max_available`
- **OCPP**: `ocpp_device_id`, `stack_level`, `charge_rate_unit`

### 2. Refactored Target Calculator (`target_calculator.py`)

**Before:**
```python
def calculate_all_charger_targets(
    hass, hub_entry, hub_state: dict, chargers: list[ConfigEntry]
) -> dict[str, float]:
    # Complex parameter passing
    # Returns dict of targets
```

**After:**
```python
def calculate_all_charger_targets(site: SiteContext) -> None:
    """Modifies charger.target_current in place"""
    for charger in site.chargers:
        if charger.charging_mode == CHARGING_MODE_ECO:
            charger.target_current = _calculate_eco_target(site, charger)
        # ... etc
```

**Helper Functions Now Use Objects:**
```python
def _calculate_eco_target(site: SiteContext, charger: ChargerContext) -> float:
    # Clean access to all site and charger data
    if site.battery_soc < site.battery_soc_min:
        return 0
    
    available_per_phase = site.total_export_current / charger.phases
    return max(charger.min_current, min(available_per_phase, charger.max_current))
```

### 3. Legacy Compatibility Maintained

**`ChargeContext` kept as deprecated alias:**
- Existing code using `ChargeContext` still works
- Allows gradual migration
- Marked with deprecation comment

---

## 🎁 Benefits Achieved

### 1. Cleaner Code
```python
# Before
target = _calculate_eco_target(
    battery_soc, battery_soc_min, battery_soc_target,
    total_export_current, charger_phases,
    min_current, max_current
)

# After
charger.target_current = _calculate_eco_target(site, charger)
```

### 2. Better Type Safety
```python
def _calculate_solar_target(site: SiteContext, charger: ChargerContext) -> float:
    # IDE autocomplete works
    # Type errors caught early
    # Clear what data is available
```

### 3. Logical Data Organization
```
SiteContext
├─ Grid data (voltage, breaker rating, phase currents)
├─ Battery data (SOC, power, limits)
├─ Solar data (export current/power)
└─ chargers: []
    ├─ ChargerContext (Charger A)
    │   ├─ Configuration (min/max/phases)
    │   ├─ State (mode, current import)
    │   └─ Results (target, allocated)
    └─ ChargerContext (Charger B)
        └─ ...
```

### 4. Single Source of Truth
- Site data calculated once, stored in `SiteContext`
- Shared by all chargers
- No double-counting of solar
- No race conditions

### 5. Easier Testing
```python
# Create test site with mock data
site = SiteContext(
    voltage=230,
    total_export_current=20.0,
    battery_soc=75,
    chargers=[
        ChargerContext(
            charger_id="test_a",
            entity_id="charger_a",
            min_current=6,
            max_current=16,
            phases=3,
            charging_mode="Solar"
        ),
        ChargerContext(
            charger_id="test_b",
            entity_id="charger_b",
            min_current=6,
            max_current=32,
            phases=3,
            charging_mode="Standard"
        ),
    ]
)

# Run calculation
calculate_all_charger_targets(site)

# Verify results
assert site.chargers[0].target_current == 6.67
assert site.chargers[1].target_current == 32
```

---

## 📋 What Still Needs To Be Done

### 1. Update `calculate_hub_state_centralized()` 
**File:** `calculations/__init__.py`

**Current:** Uses old dict-based approach
**Needed:** Build `SiteContext` and `ChargerContext` objects

```python
def calculate_hub_state_centralized(hass, hub_entry, charger_configs):
    # 1. Build SiteContext
    site = SiteContext(
        voltage=hub_entry.data.get(CONF_PHASE_VOLTAGE),
        main_breaker_rating=hub_entry.data.get(CONF_MAIN_BREAKER_RATING),
        # ... read all hub data
    )
    
    # 2. Build ChargerContext for each charger
    for config in charger_configs:
        charger = ChargerContext(
            charger_id=config.entry_id,
            entity_id=config.data[CONF_ENTITY_ID],
            min_current=config.data[CONF_MIN_CURRENT],
            max_current=config.data[CONF_MAX_CURRENT],
            phases=get_charger_phases(hass, config),
            charging_mode=get_charger_mode(hass, config),
            priority=config.data[CONF_CHARGER_PRIORITY],
        )
        site.chargers.append(charger)
    
    # 3. Calculate targets (modifies charger.target_current)
    calculate_all_charger_targets(site)
    
    # 4. Distribute (modifies charger.allocated_current)
    distribute_current(site)
    
    return site
```

### 2. Update `distribute_current_to_chargers()`
**File:** `__init__.py`

**Current:**
```python
def distribute_current_to_chargers(
    hass, hub_entry_id, total_available, charger_targets: dict
) -> dict:
```

**Needed:**
```python
def distribute_current(site: SiteContext) -> None:
    """Modifies charger.allocated_current in place"""
    # Get distribution mode
    mode = get_distribution_mode(site)
    
    # Sort by priority
    sorted_chargers = sorted(site.chargers, key=lambda c: c.priority)
    
    # Apply algorithm
    if mode == "shared":
        _distribute_shared(sorted_chargers, site.total_available_current)
    # etc.
```

### 3. Update Distribution Helper Functions
**Functions to update:**
- `_distribute_shared()` → Use `ChargerContext` list
- `_distribute_priority()` → Use `ChargerContext` list
- `_distribute_sequential_optimized()` → Use `ChargerContext` list
- `_distribute_sequential_strict()` → Use `ChargerContext` list

**Before:**
```python
def _distribute_shared(charger_info: list, total_available_current: float):
    for charger in charger_info:
        charger["allocated"] = ...
```

**After:**
```python
def _distribute_shared(chargers: list[ChargerContext], total_available: float):
    for charger in chargers:
        charger.allocated_current = ...
```

### 4. Update sensor.py
**When ready to integrate:**
- Hub coordinator calls `calculate_hub_state_centralized()`
- Returns `SiteContext` object
- Chargers read from `site.chargers[i].allocated_current`

---

## 🎯 Migration Strategy

### Phase 1: Foundation (✅ COMPLETED)
- [x] Create `SiteContext` class
- [x] Create `ChargerContext` class
- [x] Keep `ChargeContext` as legacy alias
- [x] Update `target_calculator.py` to use new classes

### Phase 2: Core Functions (IN PROGRESS)
- [ ] Update `calculate_hub_state_centralized()` to build `SiteContext`
- [ ] Update `distribute_current_to_chargers()` to use `SiteContext`
- [ ] Update distribution helper functions

### Phase 3: Integration (NOT STARTED)
- [ ] Update hub coordinator in sensor.py
- [ ] Update charger sensors to read from `SiteContext`
- [ ] Test with single charger
- [ ] Test with multiple chargers

### Phase 4: Cleanup (NOT STARTED)
- [ ] Remove deprecated `ChargeContext` class
- [ ] Remove old dict-based functions
- [ ] Update all references

---

## 🔍 Example Usage (Once Complete)

```python
# Hub coordinator update cycle
async def async_update_hub():
    # Build site with all data
    site = calculate_hub_state_centralized(hass, hub_entry, chargers)
    
    # Site now contains:
    # - All site data (grid, battery, solar)
    # - All chargers with calculated targets
    # - All chargers with allocated currents
    
    # Send commands to each charger
    for charger in site.chargers:
        await send_ocpp_command(
            charger.ocpp_device_id,
            charger.allocated_current
        )
    
    return site
```

---

## 📊 Comparison: Before vs After

### Data Flow - Before
```
charger_sensor.update():
  ├─ Read hub state (grid, battery, solar)
  ├─ Calculate for THIS charger
  ├─ Calculate targets for ALL chargers (redundant!)
  └─ Distribute (using stale data from other chargers)
  
charger_sensor_2.update() (0.1s later):
  ├─ Read hub state (slightly different now!)
  ├─ Calculate for THIS charger
  ├─ Calculate targets for ALL chargers (again!)
  └─ Distribute (different result = oscillation!)
```

### Data Flow - After
```
hub_coordinator.update():
  ├─ Build SiteContext (grid, battery, solar) - ONCE
  ├─ Build ChargerContext for each charger - ONCE
  ├─ Calculate targets for all chargers - ONCE
  │   └─ site.chargers[0].target_current = 16A
  │   └─ site.chargers[1].target_current = 0A
  ├─ Distribute current among chargers - ONCE
  │   └─ site.chargers[0].allocated_current = 16A
  │   └─ site.chargers[1].allocated_current = 0A
  └─ Return complete SiteContext

charger_sensors[0].update():
  └─ Read site.chargers[0].allocated_current
  └─ Send OCPP command

charger_sensors[1].update():
  └─ Read site.chargers[1].allocated_current
  └─ Send OCPP command
```

---

## 🚀 Next Steps

1. **Complete Phase 2** - Update core calculation functions to use `SiteContext`
2. **Test independently** - Unit test the new functions before integration
3. **Integrate gradually** - Start with hub coordinator
4. **Monitor closely** - Check for oscillation issues
5. **Clean up** - Remove deprecated code once stable

---

*Document created: 2026-02-10*
*Status: Phase 1 complete, Phase 2 in progress*
