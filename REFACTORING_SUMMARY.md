# Refactoring Summary - Centralized Hub Calculation

## Problem Statement

The current architecture has each charger independently calculating hub state, targets, and distribution. This causes:

1. **Power Oscillation**: Multiple chargers updating at slightly different times create feedback loops
2. **Double-Counting Solar**: Each charger calculates available solar independently, leading to over-allocation
3. **Race Conditions**: Distribution happens multiple times per cycle with different data
4. **Inefficiency**: Same calculations repeated N times for N chargers

## Solution Overview

Centralize all calculations at the hub level so they happen **once per update cycle**:
- Hub calculates state (grid, battery, solar) → ONCE
- Hub calculates all charger targets → ONCE (prevents double-counting)
- Hub distributes current → ONCE
- Chargers read their allocation → Passive

## Files Modified

### 1. `calculations/target_calculator.py` (NEW)

**Purpose**: Centralized calculation of charging targets for ALL chargers

**Key Functions:**
- `calculate_all_charger_targets()` - Main function that calculates targets for all chargers in one pass
- `_calculate_eco_target()` - Eco mode logic
- `_calculate_solar_target()` - Solar mode logic
- `_calculate_excess_target()` - Excess mode logic

**Why This Helps:**
- Solar/export power is calculated ONCE and shared across all chargers
- No double-counting of available resources
- Consistent calculations across all chargers

### 2. `calculations/__init__.py` (MODIFIED)

**Added Function:**
```python
calculate_hub_state_centralized(hass, hub_entry, chargers)
```

**What It Does:**
1. Reads hub state (grid currents, battery, solar) from sensors
2. Builds `hub_state` dict with shared resources
3. Calls `calculate_all_charger_targets()` to get targets
4. Calls `distribute_current_to_chargers()` to allocate
5. Returns `charger_commands` dict with allocated current for each charger

**Returns:**
```python
{
    "hub_state": {
        "battery_soc": 75,
        "total_export_current": 25.0,
        "total_export_power": 5750,
        ...
    },
    "charger_commands": {
        "charger_a_id": {
            "allocated_current": 16.0,
            "target_current": 16.0,
            "charging_mode": "Standard"
        },
        "charger_b_id": {
            "allocated_current": 0.0,
            "target_current": 0.0,
            "charging_mode": "Solar"
        }
    }
}
```

### 3. `calculations/utils.py` (MODIFIED)

**Updated Function:**
```python
get_sensor_data(hass, sensor, attribute=None)
```

**Added Support For:**
- Reading sensor attributes in addition to state
- Used by centralized calculation to read charger phases from sensor attributes

## Current Status

### ✅ Completed

1. **Created target_calculator.py** - Centralized target calculation logic
2. **Created calculate_hub_state_centralized()** - Hub-level calculation function
3. **Updated get_sensor_data()** - Support for reading attributes
4. **Documented the approach** - This file

### ⚠️ Not Yet Implemented

The following changes are needed to complete the refactoring:

1. **sensor.py - Hub Coordinator** (NOT DONE)
   - Create `DataUpdateCoordinator` for hub
   - Hub coordinator calls `calculate_hub_state_centralized()`
   - Stores results in `hass.data[DOMAIN]["charger_commands"]`

2. **sensor.py - Charger Sensors** (NOT DONE)
   - Remove individual `DataUpdateCoordinator` from each charger
   - Chargers listen to hub coordinator updates
   - Chargers read allocation from `hass.data` and send OCPP command
   - Become "passive" - just execute hub's commands

3. **Distribution Timing** (NOT DONE)
   - Move distribution call from charger sensors to hub coordinator
   - Ensure distribution happens exactly once per update cycle

## Why Not Complete?

The `sensor.py` refactoring is **complex and risky**:
- 600+ lines of code
- Multiple entity types (hub sensor, charger sensors)
- Coordinator lifecycle management
- OCPP command timing
- Pause timer logic
- State persistence

**Recommendation**: Test the new functions first before modifying sensor.py

## Testing Plan (Before Completing Refactoring)

### Phase 1: Unit Test New Functions

```python
# Test target_calculator
from calculations.target_calculator import calculate_all_charger_targets

hub_state = {
    "total_export_current": 20.0,  # 20A export available
    "battery_soc": 75,
    "voltage": 230,
}

chargers = [charger_a_config, charger_b_config]
targets = calculate_all_charger_targets(hass, hub_entry, hub_state, chargers)
# Verify: Total targets <= 20A (not double-counted)
```

### Phase 2: Test Centralized Calculation

```python
# Test full hub calculation
from calculations import calculate_hub_state_centralized

result = calculate_hub_state_centralized(hass, hub_entry, chargers)
# Verify: Solar counted once
# Verify: Distribution makes sense
# Verify: No oscillation in repeated calls
```

### Phase 3: Integration Test (When sensor.py is Ready)

- Single charger: Should work same as before
- Two chargers, Standard mode: Should distribute fairly
- Two chargers, Solar mode: Should NOT double-count solar
- Monitor for oscillation over 5-10 minutes

## How to Complete the Refactoring

### Step 1: Create Hub Coordinator (sensor.py)

Add to `async_setup_entry()` for hub entries:

```python
if entry_type == ENTRY_TYPE_HUB:
    # Create hub coordinator
    async def async_update_hub():
        from . import get_chargers_for_hub
        from .calculations import calculate_hub_state_centralized
        
        chargers = get_chargers_for_hub(hass, entry.entry_id)
        result = calculate_hub_state_centralized(hass, entry, chargers)
        
        # Store commands for chargers to read
        hass.data[DOMAIN]["charger_commands"] = result["charger_commands"]
        return result
    
    update_freq = entry.data.get(CONF_UPDATE_FREQUENCY, 15)
    coordinator = DataUpdateCoordinator(
        hass, _LOGGER,
        name="Dynamic OCPP Hub",
        update_method=async_update_hub,
        update_interval=timedelta(seconds=update_freq),
    )
    
    hass.data[DOMAIN]["hub_coordinators"][entry.entry_id] = coordinator
    await coordinator.async_config_entry_first_refresh()
```

### Step 2: Update Charger Sensors (sensor.py)

Remove charger coordinators, listen to hub instead:

```python
class DynamicOcppEvseChargerSensor(SensorEntity):
    def __init__(self, hass, config_entry, hub_entry, hub_coordinator):
        self.hub_coordinator = hub_coordinator
        # Subscribe to hub updates
        self.async_on_remove(
            hub_coordinator.async_add_listener(self._handle_hub_update)
        )
    
    async def _handle_hub_update(self):
        """Called when hub coordinator updates."""
        commands = self.hass.data[DOMAIN]["charger_commands"]
        my_command = commands.get(self.config_entry.entry_id)
        
        if my_command:
            allocated = my_command["allocated_current"]
            self._state = allocated
            self._allocated_current = allocated
            
            # Send OCPP command
            await self._send_ocpp_command(allocated)
            self.async_write_ha_state()
```

### Step 3: Test Thoroughly

1. Start with 1 charger - verify no regression
2. Add 2nd charger - verify no oscillation
3. Test all modes (Standard, Eco, Solar, Excess)
4. Monitor logs for 10+ minutes
5. Verify solar not double-counted

## Benefits After Completion

1. **No Oscillation** - Single update cycle eliminates feedback loops
2. **Accurate Solar Counting** - Calculated once, shared by all
3. **Better Performance** - O(1) calculations instead of O(N)
4. **Easier Debugging** - Single calculation point in logs
5. **Cleaner Architecture** - Clear separation: Hub calculates, Chargers execute

## Rollback Plan

If issues arise:
1. The new functions don't affect existing code yet
2. Can delete `target_calculator.py` and `calculate_hub_state_centralized()`
3. Existing sensor.py continues to work as before
4. Git revert if needed

## Next Steps

**Option A: Complete the refactoring now** (2-4 hours)
- Modify sensor.py as described above
- Test thoroughly
- Risk: Breaking existing functionality

**Option B: Test new functions first** (30 minutes)
- Write simple test script
- Verify target calculation works correctly
- Verify no double-counting
- Then proceed to sensor.py changes

**Option C: Gradual migration**
- Keep existing code
- Add new hub coordinator alongside
- Slowly migrate chargers one at a time
- Safest but most complex

## Recommendation

**Start with Option B** - Test the new functions to verify they work correctly, then proceed with sensor.py changes. This minimizes risk while making progress.

---

*Document created: 2026-02-09*
*Status: Foundation complete, sensor.py refactoring pending*
