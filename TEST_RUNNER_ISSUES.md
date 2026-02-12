# Test Runner Issues and Recommendations

## Issues Found

### 1. **Hardcoded 3-Phase Assumption in Feedback Simulation** (Minor)
**Location**: `tests/run_tests.py:30`

**Issue**:
```python
solar_per_phase = initial_solar / 3 / voltage
```

Assumes 3 phases, should respect `site.num_phases`.

**Impact**: 1-phase test scenarios get incorrect feedback simulation (though most tests are 3-phase).

**Fix**:
```python
num_phases = site.num_phases if site.num_phases > 0 else 1
solar_per_phase = initial_solar / num_phases / voltage
```

---

### 2. **Single-Phase Charger Load Ignored Phase Assignment** (Medium)
**Location**: `tests/run_tests.py:41-43`

**Issue**:
```python
if charger.phases == 1:
    # Single-phase: add to one phase (assume phase A for now)
    phase_a_load += charger.target_current
```

All single-phase chargers are assumed to be on phase A, regardless of `connected_to_phase`.

**Impact**: Tests with single-phase chargers on phases B or C get incorrect feedback (e.g., `1ph-1c-solar-prio-with-bat-oscillation` which has charger on phase B).

**Fix**: Use `charger.active_phases_mask` to determine which phase(s) to add load:
```python
if charger.phases == 1:
    if 'A' in charger.active_phases_mask:
        phase_a_load += charger.target_current
    elif 'B' in charger.active_phases_mask:
        phase_b_load += charger.target_current
    elif 'C' in charger.active_phases_mask:
        phase_c_load += charger.target_current
elif charger.phases == 2:
    # 2-phase charger
    if 'A' in charger.active_phases_mask and 'B' in charger.active_phases_mask:
        phase_a_load += charger.target_current
        phase_b_load += charger.target_current
    # ... other 2-phase combinations
elif charger.phases == 3:
    phase_a_load += charger.target_current
    phase_b_load += charger.target_current
    phase_c_load += charger.target_current
```

---

### 3. **No Handling for 2-Phase Chargers in Feedback** (Minor)
**Location**: `tests/run_tests.py:38-49`

**Issue**: Only handles 1-phase and 3-phase chargers in feedback simulation.

**Impact**: 2-phase OBC tests may have incorrect feedback.

**Fix**: Add 2-phase handling as shown above.

---

### 4. **Single-Phase Charger Default Phase Assignment** (Informational)
**Location**: `tests/run_tests.py:171-181`

**Code**:
```python
if not active_phases_mask:
    connected_to_phase = charger_data.get('connected_to_phase')
    if connected_to_phase:
        active_phases_mask = connected_to_phase
    elif phases == 3:
        active_phases_mask = "ABC"
    elif phases == 2:
        active_phases_mask = "AB"
    # else: phases == 1 without connected_to_phase remains None
```

**Observation**: Single-phase chargers without `connected_to_phase` get `active_phases_mask=None`, but `ChargerContext.__post_init__()` should default it to 'A'. This should work correctly due to the __post_init__ logic.

**Recommendation**: For clarity, either:
- Add `else: active_phases_mask = "A"` to match the __post_init__ behavior explicitly, OR
- Add a comment explaining that __post_init__ will handle the default

---

## Missing Information Assessment

### Current Test Scenario Data
The test scenarios provide:
- ✅ Solar production (total)
- ✅ Per-phase consumption
- ✅ Battery SOC, min, target
- ✅ Battery max charge/discharge power
- ✅ Inverter max power (total and per-phase)
- ✅ Asymmetric inverter support flag
- ✅ Charger phases, min/max current, priority
- ✅ Charger connected_to_phase (for explicit phase assignment)

### Potentially Missing for Full Simulation
None identified. The test scenarios have sufficient data to calculate expected results.

**However**, for real HA integration context building (not test scenarios), we may need:
- Grid power import/export sensors (for non-battery systems)
- Phase voltage sensors (if not assuming 230V)
- Real-time charger current draw (for feedback)

---

## Recommendations

### Priority 1: Fix Single-Phase Load Allocation
This affects failing oscillation tests and any test with single-phase chargers on specific phases.

### Priority 2: Add 2-Phase Charger Support to Feedback
For completeness and future 2-phase OBC tests.

### Priority 3: Handle 1-Phase Systems in Feedback
For completeness, though most tests are 3-phase.

### Optional: Add Validation
Add validation to test runner:
- Warn if single-phase charger has no `connected_to_phase` specified
- Warn if test scenario uses features not yet supported (e.g., asymmetric inverter with grid power from different phases)
