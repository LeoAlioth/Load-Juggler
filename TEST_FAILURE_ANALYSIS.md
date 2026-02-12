# Test Failure Analysis

**Date**: 2026-02-12
**Test Run**: 33 scenarios - 26 passed, 7 failed (78.8% pass rate)

## Summary of Failures

| Test | Expected | Actual | Difference | Severity |
|------|----------|--------|------------|----------|
| 3ph-1c-solar-prio-with-bat-unbalanced | 9.3A | 9.0A | -0.3A | Minor |
| 3ph-1c-excess-prio-with-bat-above-threshold | 8A | 0.0A | -8A | **Critical** |
| 3ph-3c-standard-prio-with-bat-normal | 16/12/6A | 11.4/6/0A | All wrong | **Critical** |
| 3ph-2c-solar-prio-with-bat-mixed-phases | 8/6A | 10/0A | Inverted | **Critical** |
| 3ph-1c-solar-2ph-obc-with-battery | 16.0A | 15.0A | -1.0A | Minor |
| 3ph-1c-solar-prio-with-bat-oscillation | 8.0A | 7.0A | -1.0A | Minor |
| 1ph-1c-solar-prio-with-bat-oscillation | 16A | 8.0A | -8A | **Critical** |

## Common Failure Modes Identified

### 1. **Excess Mode Complete Failure** (Critical)

**Test**: `3ph-1c-excess-prio-with-bat-above-threshold`

**Scenario**:
- Solar production: 11,840W (51.5A total)
- Consumption: 0A
- Export threshold: 10,000W
- Expected: Charge at 8A (excess above threshold)
- **Actual: 0A (not charging at all)**

**Root Cause**: Excess mode logic is completely broken. The charger should charge when export exceeds the threshold, but it's not working.

**Impact**: Excess charging mode is unusable.

---

### 2. **Single-Phase Charger on Asymmetric Systems** (Critical)

**Test**: `1ph-1c-solar-prio-with-bat-oscillation`

**Scenario**:
- 1-phase charger connected to phase B
- 3-phase site with asymmetric inverter
- Solar: 30A total, consumption: 9A total (unbalanced)
- Available export: 21A total
- Expected: 16A (max current)
- **Actual: 8A (50% of expected)**

**Root Cause**: Single-phase chargers on asymmetric inverter systems are not accessing the total power pool. The code appears to be limiting them to per-phase export values instead of allowing them to access the full asymmetric power pool.

**Related Failing Test**: `3ph-2c-solar-prio-with-bat-mixed-phases` (1-phase + 3-phase chargers)

**Impact**: Single-phase chargers severely undercharged on asymmetric systems.

---

### 3. **Mixed-Phase Charger Distribution** (Critical)

**Test**: `3ph-2c-solar-prio-with-bat-mixed-phases`

**Scenario**:
- Charger 1: 3-phase, priority 1
- Charger 2: 1-phase, priority 2
- Solar available: 30A total after load
- Expected: C1=8A, C2=6A
- **Actual: C1=10A, C2=0A**

**Root Cause**: Distribution logic fails when mixing 1-phase and 3-phase chargers. Higher priority charger gets too much, lower priority gets nothing.

**Impact**: Multi-charger setups with mixed phase configurations don't work correctly.

---

### 4. **Standard Mode Multi-Charger Distribution** (Critical)

**Test**: `3ph-3c-standard-prio-with-bat-normal`

**Scenario**:
- 3 chargers, all 3-phase, standard mode
- Total available: 111A (34A per phase after consumption)
- Expected: C1=16A, C2=12A, C3=6A (priority-based distribution)
- **Actual: C1=11.4A, C2=6.0A, C3=0A**

**Root Cause**: Priority distribution in Standard mode is not calculating total available power correctly. All chargers are getting significantly less than expected.

**Impact**: Standard mode multi-charger setups undercharge all vehicles.

---

### 5. **2-Phase OBC Support** (Minor)

**Test**: `3ph-1c-solar-2ph-obc-with-battery`

**Scenario**:
- 2-phase charger (AB phases)
- Solar + battery available: 45A total
- Expected: 16A (max)
- **Actual: 15.0A**

**Root Cause**: Minor calculation error for 2-phase chargers. Almost correct but off by 1A.

**Impact**: Minor - 2-phase chargers work but slightly undercharge.

---

### 6. **Rounding/Precision Issues** (Minor)

**Tests**:
- `3ph-1c-solar-prio-with-bat-unbalanced` (expected 9.3A, got 9.0A)
- `3ph-1c-solar-prio-with-bat-oscillation` (expected 8.0A, got 7.0A)

**Root Cause**: Minor rounding or precision errors in calculations, particularly with unbalanced consumption.

**Impact**: Minor - chargers work but slightly undercharge by ~1A.

---

## Failure Mode Categories

### Category A: Complete Feature Failures (2 failures)
1. **Excess Mode** - Not working at all
2. **Single-phase on asymmetric inverters** - Getting 50% of available power

### Category B: Distribution Logic Failures (2 failures)
3. **Mixed-phase distribution** - Wrong allocation between 1ph and 3ph chargers
4. **Multi-charger standard mode** - All chargers undercharged

### Category C: Minor Calculation Errors (3 failures)
5. **2-phase OBC** - Off by 1A
6. **Unbalanced consumption** - Off by 0.3A
7. **Oscillation test** - Off by 1A

---

## Root Cause Analysis

### Primary Issue: Asymmetric Inverter Power Pool Access

**The core problem**: When `inverter_supports_asymmetric=True`, the code should allow chargers to access a **total power pool** that can be distributed across any phase. Currently:

❌ **What's happening**:
- Single-phase chargers are limited to their specific phase export
- Distribution logic doesn't account for asymmetric power flexibility

✅ **What should happen**:
- Single-phase chargers on asymmetric systems should access the total available power pool
- Distribution should consider that power can be moved between phases

**Example from failing test**:
```
Solar: 30A total
Consumption: 5A (phase A) + 2A (phase B) + 2A (phase C) = 9A total
Available: 30A - 9A = 21A total

Single-phase charger on phase B:
- Current behavior: Limited to phase B export only (~8A)
- Expected behavior: Can access full 21A pool (limited by max=16A)
```

### Secondary Issue: Excess Mode Implementation

The Excess mode appears to have a fundamental logic error. It should charge when:
```python
export_power > excess_export_threshold
```

But it's returning 0A even when export is 11,840W and threshold is 10,000W.

### Tertiary Issue: Distribution Algorithm

The distribution logic (`_distribute_power()`) doesn't properly handle:
- Mixed 1-phase and 3-phase chargers
- Asymmetric power pools
- Standard mode with multiple chargers

---

## Recommended Fix Priority

### Priority 1 (Critical - Blocks major features)
1. **Fix Excess Mode** - Complete feature failure
2. **Fix asymmetric single-phase access** - Affects all single-phase chargers on asymmetric systems

### Priority 2 (High - Affects multi-charger setups)
3. **Fix mixed-phase distribution** - Required for real-world multi-charger installations
4. **Fix standard mode distribution** - Affects most common charging mode

### Priority 3 (Low - Minor precision issues)
5. **Fix 2-phase OBC calculation** - Working but imprecise
6. **Fix rounding errors** - Minor precision improvements

---

## Next Steps

1. **Investigate constraint dict implementation** - The CLAUDE.md mentions transitioning to constraint dicts. This should help with the multi-phase allocation issues.

2. **Review Excess mode logic** - Check `calculations/modes/excess.py` for the bug causing 0A output.

3. **Review `_distribute_power_per_phase()`** - This function needs to properly handle asymmetric power pools for single-phase chargers.

4. **Add detailed logging** - Add debug logging to show:
   - Total power pool available
   - Per-phase vs total allocation decisions
   - Distribution algorithm steps

5. **Create minimal reproduction tests** - Simplify failing tests to isolate exact failure points.
