# CLINE.md - Dynamic OCPP EVSE Development Guide

This file helps me (Cline) understand and develop this Home Assistant integration for dynamic EVSE (Electric Vehicle Supply Equipment) charging management.

## Repository Overview

**Project**: Dynamic OCPP EVSE Home Assistant Integration  
**Purpose**: Intelligently manage EV charging based on solar production, battery state, and grid conditions  
**Language**: Python 3  
**Integration Type**: Home Assistant Custom Component  

## What This Integration Does

Dynamically controls EV charger current limits based on:
- Solar production (real-time)
- Battery state of charge
- Home consumption  
- Grid import/export limits
- Multiple charging modes (Standard, Eco, Solar, Excess)
- Multiple distribution modes (Priority, Shared, Strict, Optimized)

## Architecture

### New Architecture (Current - Post Refactoring)

**Core Calculation Engine**: `calculations/target_calculator.py`
- Single entry point: `calculate_all_charger_targets(site)`
- Works with `SiteContext` (site-wide) + multiple `ChargerContext` objects
- **Per-phase tracking** for proper 3-phase and mixed-phase scenarios
- All mode logic unified in one place

**Key Principle**: Everything is per-phase!
- 3-phase site: Track A, B, C independently
- Battery power can balance across phases (not tied to solar phases)
- Solar power is phase-specific (evenly distributed)
- Chargers consume from their connected phase(s)

### Data Models (`calculations/models.py`)

```python
@dataclass
class SiteContext:
    """Site-wide state"""
    voltage: float
    num_phases: int  # 1 or 3
    main_breaker_rating: float
    
    # Per-phase consumption & export
    phase_a_consumption: float
    phase_b_consumption: float  
    phase_c_consumption: float
    phase_a_export: float
    phase_b_export: float
    phase_c_export: float
    
    # Solar & battery
    solar_production_total: float
    battery_soc: float | None
    battery_max_charge_power: float
    battery_max_discharge_power: float
    
    # Settings
    charging_mode: str  # Standard/Eco/Solar/Excess
    distribution_mode: str  # Priority/Shared/Strict/Optimized
    allow_grid_charging: bool
    
    # Chargers
    chargers: list[ChargerContext]

@dataclass  
class ChargerContext:
    """Per-charger state"""
    charger_id: str
    entity_id: str
    min_current: float
    max_current: float
    phases: int  # Charger capability (1 or 3)
    priority: int
    
    # Phase tracking
    car_phases: int | None  # Actual car OBC phases
    active_phases_mask: str | None  # "A", "AB", "ABC", etc.
    connector_status: str  # OCPP status
    l1_current: float  # Per-phase readings
    l2_current: float
    l3_current: float
    
    # Result
    target_current: float  # Calculated by target_calculator
```

### Calculation Flow

1. **Filter active chargers** (skip "Available"/"Unknown")
2. **Calculate site limits** (per-phase breaker limits)
3. **Calculate solar available** (per-phase solar - consumption + battery balance)
4. **Calculate excess available** (if export > threshold)
5. **Determine target power** (based on charging mode)
6. **Distribute power** (based on distribution mode, per-phase aware)

### Key Insight: Per-Phase Budgeting

**For 3-phase sites:**
- Track 3 independent phase budgets: A, B, C
- Solar: Equally distributed (e.g., 10A/phase)
- Battery: Can balance anywhere (add to any phase needed)
- 3-phase charger: Uses same current from ALL phases
- 1-phase charger: Uses current from ONE phase

**Example:**
```
Site: 10A/phase solar, 18A battery discharge available
Charger 1 (3ph, priority 1): Gets 10A/phase → uses A+B+C
Charger 2 (1ph, priority 2): Gets 6A → uses remaining + battery on one phase
```

## Charging Modes

### Standard Mode
Use all available power (solar + grid up to limits + battery)

### Eco Mode  
Use max of (solar available, sum of minimums)
- Protects battery if below minimum SOC
- Can use battery discharge if above target SOC

### Solar Mode
Use ONLY solar available (solar export + battery discharge if SOC > target)
- Pure solar charging
- Battery acts as buffer

### Excess Mode
Charge ONLY when export > threshold
- Prevents grid import
- Waits for sufficient export

## Distribution Modes

### Priority Mode (Most Common)
Two-pass allocation by priority:
1. Give everyone minimum (by priority order)
2. Give remainder to highest priority first

### Shared Mode
Two-pass equal allocation:
1. Give everyone minimum
2. Split remainder equally

### Strict Mode
One-pass sequential: First charger gets max, then next, etc.

### Optimized Mode
Smart reduction: Reduce high-priority to allow low-priority minimum

## Files Structure

```
custom_components/dynamic_ocpp_evse/
├── __init__.py              # Integration setup
├── const.py                 # Constants
├── config_flow.py           # UI configuration
├── manifest.json            # Integration metadata
├── sensor.py                # Main sensor (triggers calculations)
├── switch.py, select.py, number.py, button.py  # UI entities
├── calculations/
│   ├── __init__.py          # Exports: SiteContext, ChargerContext, calculate_all_charger_targets
│   ├── models.py            # Data models
│   ├── target_calculator.py # Main calculation engine
│   ├── context.py           # Phase detection utility (determine_phases)
│   └── utils.py             # Shared utilities
└── translations/            # UI translations (en, sl)

tests/
├── run_tests.py             # Test runner
├── test_scenarios.yaml      # Test scenarios
└── README.md                # Test documentation
```

## Testing

**Run all tests:**
```bash
python tests/run_tests.py
```

**Run specific test:**
```bash
python tests/run_tests.py "test-name"
```

**Current test status:** 23/27 passing (85%)

### Known Failing Tests
1. `3ph-2c-solar-prio-with-bat-mixed-phases` - Mixed 1ph/3ph distribution
2. `3ph-2c-solar-prio-no-bat-mixed-phases` - Same without battery
3. `3ph-1c-solar-prio-with-bat-oscillation` - Oscillation detection
4. `1ph-1c-solar-prio-with-bat-oscillation` - Oscillation detection

## Development Notes

### Recent Changes
- ✅ Removed all legacy mode files (standard.py, solar.py, eco.py, excess.py, base.py)
- ✅ Removed legacy max_available.py
- ✅ Cleaned up calculations/__init__.py to export only new architecture
- ✅ Added phase detection (determine_phases) with car_phases, active_phases_mask
- ✅ Added connector status filtering (skip disconnected chargers)

### Current Work
- 🔧 Fixing mixed-phase distribution (1ph + 3ph chargers together)
- 🔧 Proper per-phase budget tracking with battery balancing

### TODO
- [ ] Complete per-phase distribution with battery balancing
- [ ] Update sensor.py to use new calculate_all_charger_targets() API
- [ ] Fix remaining 4 test failures
- [ ] Add oscillation protection
- [ ] Document sensor.py integration

## Important Concepts

### Battery Behavior
- **SOC < min**: Protect battery, no charging allowed (except Eco mode)
- **SOC < target**: Battery charges from solar first, reduces EV charging
- **SOC = target**: Battery idle, all solar to EV
- **SOC > target**: Battery can discharge to boost EV charging

### Phase Balancing
- Solar: Fixed per-phase (1/3 of total per phase for 3ph)
- Battery: Flexible, can be attributed to any phase for balancing
- Grid: Per-phase limited by breaker rating

### Min vs Max Current
- **min_current**: Minimum to start/continue charging (typically 6A)
- **max_current**: Charger/car capability limit (typically 16A, 32A)
- If available < min_current: charger stops (set to 0A)

## Common Patterns

### Reading Sensor State
```python
# In sensor.py update()
from .calculations import SiteContext, ChargerContext, calculate_all_charger_targets

# Build site context
site = SiteContext(
    voltage=230,
    num_phases=3,
    # ... all site data
)

# Add chargers
for charger_config in chargers:
    charger = ChargerContext(
        # ... charger data
    )
    site.chargers.append(charger)

# Calculate!
calculate_all_charger_targets(site)

# Read results
for charger in site.chargers:
    target = charger.target_current
    # Update OCPP charger setpoint
```

### Adding a New Charging Mode
1. Add constant to `const.py`
2. Add case in `_determine_target_power()` in target_calculator.py
3. Add translation strings
4. Add test scenarios

### Adding a New Distribution Mode
1. Add function `_distribute_<mode>()` in target_calculator.py
2. Add case in `_distribute_power()`
3. Add test scenarios

## Debugging Tips

### Enable Debug Logging
In Home Assistant `configuration.yaml`:
```yaml
logger:
  default: info
  logs:
    custom_components.dynamic_ocpp_evse: debug
```

### Check Calculations
Look for log entries:
```
Step 1 - Site limit: X.XA
Step 2 - Solar available: X.XA
Step 3 - Excess available: X.XA
Step 4 - Target power (Mode): X.XA
Final - charger_X: X.XA
```

### Common Issues
- **Charger gets 0A**: Check if below min_current or battery SOC too low
- **Wrong phase calc**: Check num_phases and per-phase consumption values
- **Not charging**: Check connector_status (must be "Charging" or similar)

## Git Remote

```
origin: https://gitea.alpacasbarn.com/LeoAlioth/Dynamic_OCPP_EVSE
```

## Contact

When in doubt, ask the user! They know the production system and real-world behavior.
