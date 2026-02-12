# P1 Fixes Summary - Asymmetric Inverter Support

**Date**: 2026-02-12
**Status**: ✅ Complete
**Test Results**: 31/33 passing (93.9%) - up from 26/33 (78.8%)

## Overview

Fixed critical P1 issues related to asymmetric inverter power allocation for solar, battery, and excess modes. The core problem was that single-phase chargers on asymmetric inverter systems couldn't access the flexible solar/battery power pool.

---

## Root Cause Analysis

### The Problem

For **asymmetric inverters**, solar and battery power can be flexibly allocated to any phase (the inverter can rebalance power between phases). However, the code was treating this power as if it were fixed per-phase like symmetric inverters.

**Example Failure** (Excess mode):
```
Excess power available: 8A total
Old behavior:
  - Divided by 3 phases: 2.67A per phase
  - Single-phase charger on A: Limited to 2.67A
  - Result: 0A (can't meet 6A minimum!)

Correct behavior:
  - Asymmetric inverter can allocate all 8A to phase A
  - Single-phase charger on A: Can access full 8A
  - Result: 8A charging ✓
```

### Asymmetric vs Symmetric Inverter Rules

**Asymmetric Inverter** (`inverter_supports_asymmetric=True`):
- ✅ Solar power: Can be allocated to ANY phase
- ✅ Battery power: Can be allocated to ANY phase
- ❌ Grid power: CANNOT be moved between phases

**Symmetric Inverter** (`inverter_supports_asymmetric=False`):
- Solar/battery power is FIXED per-phase
- Each phase operates independently

---

## Changes Made

### 1. Fixed `_calculate_solar_available()`

**File**: `custom_components/dynamic_ocpp_evse/calculations/target_calculator.py`

**Asymmetric Logic**:
```python
# Calculate total solar available (after consumption and battery)
solar_available = solar_total - total_consumption - battery_charging + battery_discharge

# All phase constraints get the TOTAL (limited by per-phase inverter max)
constraints = {
    'A': min(solar_available, max_per_phase),
    'B': min(solar_available, max_per_phase),
    'C': min(solar_available, max_per_phase),
    'AB': solar_available,
    'AC': solar_available,
    'BC': solar_available,
    'ABC': solar_available
}
```

**Symmetric Logic** (preserved):
```python
# Calculate solar per phase (evenly distributed)
solar_per_phase = solar_total / 3

# Subtract consumption PER PHASE
phase_a_available = solar_per_phase - phase_a_consumption
# ... handle battery per phase ...

constraints = {
    'A': phase_a_available,
    'B': phase_b_available,
    'C': phase_c_available,
    # ... 2-phase and 3-phase combinations
}
```

---

### 2. Fixed `_calculate_excess_available()`

**File**: `custom_components/dynamic_ocpp_evse/calculations/target_calculator.py`

Applied the same asymmetric/symmetric logic for Excess mode:

**Asymmetric**:
```python
# Excess power above threshold
excess_available = (total_export_power - threshold) / voltage

# All constraints get the total
constraints = {A: 8, B: 8, C: 8, AB: 8, AC: 8, BC: 8, ABC: 8}
```

**Symmetric**:
```python
# Divide excess per phase
excess_per_phase = excess_available / 3

constraints = {A: 2.67, B: 2.67, C: 2.67, ...}
```

---

### 3. Fixed Test Runner Feedback Simulation

**File**: `tests/run_tests.py`

**Issue 1**: Hardcoded 3-phase assumption
```python
# OLD: solar_per_phase = initial_solar / 3 / voltage
# NEW:
num_phases = site.num_phases if site.num_phases > 0 else 1
if num_phases == 1:
    solar_per_phase_a = initial_solar / voltage
else:
    solar_per_phase = initial_solar / num_phases / voltage
```

**Issue 2**: Single-phase chargers ignored `connected_to_phase`
```python
# OLD: phase_a_load += charger.target_current  # Always phase A!
# NEW:
if charger.active_phases_mask:
    if 'A' in mask and 'B' not in mask and 'C' not in mask:
        phase_a_load += charger.target_current
    elif 'B' in mask and 'A' not in mask and 'C' not in mask:
        phase_b_load += charger.target_current
    elif 'C' in mask ...
```

**Issue 3**: Added 2-phase charger support
```python
elif charger.phases == 2:
    if 'A' in mask and 'B' in mask:
        phase_a_load += charger.target_current
        phase_b_load += charger.target_current
    # ... other 2-phase combinations
```

---

## Tests Fixed (5 scenarios)

### ✅ P1 Critical Fixes

1. **3ph-1c-excess-prio-with-bat-above-threshold**
   - Issue: Excess mode returned 0A when 8A expected
   - Fix: Asymmetric excess allocation
   - Result: PASS ✓

2. **1ph-1c-solar-prio-with-bat-oscillation**
   - Issue: Single-phase on phase B getting 8A instead of 16A
   - Fix: Asymmetric solar allocation + test runner feedback
   - Result: PASS ✓

3. **3ph-1c-solar-prio-with-bat-oscillation**
   - Issue: Getting 7A instead of 8A
   - Fix: Asymmetric solar allocation
   - Result: PASS ✓

### ✅ Secondary Fixes

4. **3ph-1c-solar-2ph-obc-with-battery**
   - Issue: 2-phase OBC getting 15A instead of 16A
   - Fix: Proper constraint dict handling for 2-phase
   - Result: PASS ✓

5. **3ph-1c-solar-prio-with-bat-unbalanced**
   - Issue: Getting 9.0A instead of 9.3A (rounding)
   - Fix: Better precision in asymmetric calculations
   - Result: PASS ✓

---

## Remaining Failures (2 scenarios - P2)

These are NOT asymmetric inverter issues - they're distribution logic problems:

### 1. **3ph-3c-standard-prio-with-bat-normal**
**Mode**: Standard
**Issue**: All 3 chargers undercharged
- Expected: C1=16A, C2=12A, C3=6A
- Actual: C1=11.4A, C2=6.0A, C3=0.0A

**Root Cause**: Standard mode site limit calculation issue. Total available is calculated as 111A (34A per phase) but chargers only getting ~17A total.

---

### 2. **3ph-2c-solar-prio-with-bat-mixed-phases**
**Mode**: Solar
**Issue**: Wrong allocation between 1-phase and 3-phase charger
- Expected: C1(3ph)=8A, C2(1ph)=6A
- Actual: C1=8A, C2=16A

**Root Cause**: Distribution priority logic not correctly handling mixed-phase scenarios with asymmetric inverters.

---

## Documentation Created

1. **TEST_FAILURE_ANALYSIS.md** - Comprehensive analysis of all 7 original failures
2. **TEST_RUNNER_ISSUES.md** - Detailed test runner feedback simulation issues
3. **P1_FIXES_SUMMARY.md** (this file) - Summary of fixes implemented

---

## Key Learnings

### Constraint Dict Philosophy

For the Multi-Phase Constraint Principle:
- **Asymmetric inverters**: All single-phase constraints should be the TOTAL available
- **Symmetric inverters**: Single-phase constraints are truly per-phase

### Example Constraint Dicts

**Asymmetric** with 8A total available:
```python
{
    'A': 8,   # Single-phase on A can access full 8A
    'B': 8,   # Single-phase on B can access full 8A
    'C': 8,   # Single-phase on C can access full 8A
    'AB': 8,  # Two-phase limited by total
    'AC': 8,
    'BC': 8,
    'ABC': 8  # Three-phase limited by total
}
```

**Symmetric** with 8A total available (2.67A per phase):
```python
{
    'A': 2.67,    # Phase A: 2.67A available
    'B': 2.67,    # Phase B: 2.67A available
    'C': 2.67,    # Phase C: 2.67A available
    'AB': 5.33,   # AB: sum of A+B
    'AC': 5.33,
    'BC': 5.33,
    'ABC': 8.0    # Total
}
```

---

## Next Steps (Optional - P2)

1. Investigate Standard mode multi-charger distribution issue
2. Fix mixed-phase charger priority distribution
3. Consider optimizing distribution algorithms for asymmetric systems

---

## Impact

**Before**: 26/33 tests passing (78.8%)
**After**: 31/33 tests passing (93.9%)
**Improvement**: +5 tests fixed, +15.1% pass rate

All P1 critical failures resolved! 🎉
