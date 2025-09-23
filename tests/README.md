# Dynamic OCPP EVSE Tests

This folder contains test scripts to verify the functionality of the Dynamic OCPP EVSE integration fixes. All tests output results to CSV files for detailed analysis and graphing.

## Test Files

### `test_entity_migration.py`
Tests the entity migration functionality that ensures new entities are created during integration updates without requiring reconfiguration.

**What it tests:**
- All possible entity combinations (32 scenarios covering 2^5 combinations)
- Fresh installation scenario (no entities exist)
- Partial update scenarios (some entities exist, others need creation)
- Complete installation scenario (all entities exist, no migration needed)
- Entity unique ID consistency patterns

**Run with:**
```bash
python tests/test_entity_migration.py
```

**CSV Output Files:**
- `tests/entity_migration_results.csv` - Detailed migration test results
- `tests/entity_unique_ids.csv` - Entity unique ID patterns

**CSV Columns (entity_migration_results.csv):**
- Scenario, Existing_Entity_Count, Missing_Entity_Count, Migration_Needed
- Individual entity existence flags (Min_Current, Max_Current, etc.)
- Missing_Entities_List for detailed analysis

### `test_current_calculation.py`
Tests the current calculation logic fix that resolves the feedback loop issue in Standard charge mode.

**What it tests:**
- Detailed comparison between old (broken) and new (fixed) logic
- EVSE current range from 0A to 20A per phase (21 data points)
- Demonstrates feedback loop elimination with granular data
- Edge cases including high base loads and export scenarios
- Current stability analysis across full operating range

**Run with:**
```bash
python tests/test_current_calculation.py
```

**CSV Output Files:**
- `tests/current_calculation_results.csv` - Detailed current calculation data

**CSV Columns (current_calculation_results.csv):**
- EVSE_Current_Per_Phase_A, Total_EVSE_Current_A, Total_Import_Current_A
- Old_Logic_Available_A, New_Logic_Available_A, Difference_A
- Phase_Current_A, Non_EVSE_Import_A
- Remaining_Import_Old_A, Remaining_Import_New_A

## Issues Fixed

### Problem 1: Entity Creation During Updates
**Issue:** New entities (min/max current sliders) weren't created on integration updates unless reconfigured.

**Fix:** Added entity migration system that detects missing entities and creates them automatically.

### Problem 2: Standard Charge Mode Current Calculation
**Issue:** In Standard mode, as EVSE current increased, available current decreased due to feedback loop.

**Fix:** Modified calculation to exclude EVSE current from its own availability calculation.

## Test Results Summary

Both test scripts demonstrate that the fixes work correctly:

1. **Entity Migration:** ✅ All scenarios handled properly
2. **Current Calculation:** ✅ Feedback loop eliminated, stable current limits

## Usage Notes

- Tests are standalone and don't require Home Assistant to run
- They use mock objects to simulate the integration environment
- Tests demonstrate the logic fixes without needing actual hardware
- Run tests after making changes to verify functionality

## Integration Testing

For full integration testing:
1. Install the updated integration in Home Assistant
2. Verify new entities are created automatically on update
3. Test Standard charge mode with gradually increasing current
4. Confirm current limits remain stable as EVSE consumption increases
