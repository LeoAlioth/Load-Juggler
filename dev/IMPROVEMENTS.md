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

## ~~Derived solar surplus with battery awareness~~

**Status:** Implemented (TODO #37, #39)

### Problem

When `solar_is_derived=True` (no dedicated solar entity) and a battery system is present, self-consumption zeroes out grid export. The engine's derived path uses `export_current` directly as surplus → sees 0 → can't allocate power to chargers. Currently forces battery users to configure a dedicated solar entity.

### Insight

Solar surplus can be computed from observable data even without a dedicated solar entity:

```
solar_surplus = grid_export + battery_charge_power + charger_draws
```

This is mathematically exact for surplus (solar minus household), because:
- `solar - household = grid_export + battery_charge + charger_draws`
- Charger draws are already recovered by the feedback loop (subtracted from grid → revealed as export)
- Battery charge power is available from `site.battery_power` entity (already in SiteContext, just unused)

### Example

- Solar: 10kW, Household: 2kW, Battery charges at 8kW
- Grid CT shows 0 (self-consumption balanced)
- Charger starts at 6A (1.38kW), battery reduces to 6.62kW
- After feedback: export = 6A (charger draw revealed)
- Current engine: surplus = 6A (only sees charger's own reflection)
- With fix: surplus = 6A + 6.62kW/230V = 6A + 28.8A = 34.8A → correctly sees full surplus

### Proposed Changes

**Engine (`target_calculator.py`)** — In `_calculate_solar_surplus()` derived path:
```python
if site.solar_is_derived:
    surplus_per_phase = export_per_phase
    # Battery charge is solar power absorbed by self-consumption — add it back
    if site.battery_power is not None and site.battery_power < 0:
        battery_charge_current = abs(site.battery_power) / site.voltage / num_phases
        surplus_per_phase += battery_charge_current
```

**Engine (`_calculate_inverter_limit()`)** — Same principle: battery discharge in derived mode can be accounted for since we have the actual battery power reading.

**Production code** — No changes needed. `battery_power` is already read from the entity and passed to `SiteContext`.

**Test simulation** — Could revert to `solar_is_derived=True` for all scenarios since the engine would handle battery correctly in derived mode.

### Mode-Specific Behavior

The engine already handles mode-specific battery priority in the non-derived path. With this change, the derived path would also need to consider:
- **Solar/Excess mode**: Add full battery_charge back (charger should use all available solar)
- **Eco mode**: Add battery_charge back only if SOC >= target (battery charges first)
- **Standard mode**: Not surplus-based, uses `_calculate_inverter_limit()` instead

### Inverter Output Cap (TODO #39)

Battery discharge goes through the inverter. If solar already maxes out inverter capacity, battery can't physically discharge more. TODO #39 added:
- `household_consumption_total` field on SiteContext (true household from simulation, best-estimate from production)
- Engine limits `discharge_potential` by `inverter_headroom = inverter_max - estimated_solar`
- Engine caps final pool at `inverter_max - household` (not just `inverter_max`)
- Simulation limits battery discharge in `simulate_grid_ct()` by `inverter_max - solar_total`

**Known limitation**: In production (derived mode), when the site is exporting on all phases, the CT reads `consumption = 0`. The true household is hidden in the reduced export and cannot be determined from CT data alone. The engine uses `household_consumption_total` which is 0 in this case — slightly over-allocating by the household amount (typically 2-5A). Using a dedicated solar entity fully resolves this.

### Benefits
- Battery users don't need a dedicated solar entity (simpler setup)
- Uses data already available in the model (`site.battery_power`)
- Minimal code change (a few lines in the derived path)
- Backwards compatible (only activates when battery_power is available)
- Inverter output never exceeded in simulation (physical accuracy)

---

## ~~Per-phase inverter output entities + wiring topology~~
**Status:** Implemented (TODO #44)
**Complexity:** Medium–High
**Solves:** Asymmetric inverter over-allocation (2 failing test scenarios), per-phase household invisibility

### Problem

When the site is fully self-consuming (battery absorbs all surplus), the grid CT reads `consumption = 0` on all phases. The true per-phase household (e.g., 2A on A, 1A on B, 1A on C) is invisible. The engine's asymmetric inverter path sets `per_phase_limit = max_per_phase` without subtracting household, causing over-allocation of ~1-2A on the most loaded phase. This is documented as a "known limitation" in the battery awareness section above.

A single solar power entity gives us `household_consumption_total` (the total), but not the per-phase breakdown. To properly enforce `per_phase_limit = max_per_phase - household_per_phase`, we need per-phase data.

### Solution: 3 per-phase inverter output entities

Replace the single solar power entity with 3 inverter output entities (L1, L2, L3) as the primary data source. Keep the single solar entity as a fallback.

Two distinct wiring topologies determine what the inverter output measures:

#### Parallel wiring (AC-coupled, typically non-battery systems)
```
Grid CT ──── Main panel ──┬── House loads
                          ├── Chargers
                          └── Inverter (solar feeds INTO panel)
```
- Inverter output = **solar generation only**
- If solar = 0, inverter reads 0 regardless of loads
- Per-phase household derivation: `household_per_phase = consumption + inverter_output - export` (after feedback)
- Simplest case: inverter output directly gives per-phase solar

#### Series wiring (hybrid inverter, typically battery systems)
```
Grid CT ──── Inverter ──── Main panel ──┬── House loads
                                        └── Chargers
```
- Inverter output = **total load on house side** (household + chargers)
- All current flows through the inverter (solar + battery + grid import)
- Per-phase household derivation: `household_per_phase = inverter_output_per_phase - charger_draws_per_phase`
- Most direct: inverter tells us total load, we subtract what we already know (charger draws)

### Data hierarchy (best → worst)

| Level | Data source | Per-phase household | Accuracy |
|-------|-------------|-------------------|----------|
| 1 | 3 inverter output entities | Exact (parallel or series formula) | Perfect |
| 2 | Single solar entity | `total / num_phases` (uniform estimate) | Good (~1A error) |
| 3 | Derived from CT only | 0 when self-consuming | Poor (full household invisible) |

### Config flow changes

**Inverter step** — add 3 optional entity selectors + wiring mode:
```
Existing fields:
  - inverter_max_power (W)
  - inverter_max_power_per_phase (W)
  - inverter_supports_asymmetric (bool)

New fields:
  - inverter_l1_output_entity_id (optional, current or power sensor)
  - inverter_l2_output_entity_id (optional, current or power sensor)
  - inverter_l3_output_entity_id (optional, current or power sensor)
  - inverter_wiring_series (bool, default: False)
    → Only shown when inverter output entities are configured
    → Default could be auto-set: True when battery entities are configured, False otherwise
```

**Solar entity (battery step)** — kept as fallback. When 3 per-phase entities are configured, the single solar entity is redundant (solar total can be derived).

### Auto-detection patterns

```python
INVERTER_OUTPUT_PATTERNS = [
    {
        "name": "Fronius",
        "patterns": {
            "l1": r'sensor\..*inverter.*(?:ac_current|current).*(?:phase_1|_l1|_a).*',
            "l2": r'sensor\..*inverter.*(?:ac_current|current).*(?:phase_2|_l2|_b).*',
            "l3": r'sensor\..*inverter.*(?:ac_current|current).*(?:phase_3|_l3|_c).*',
        },
    },
    {
        "name": "SolarEdge inverter",
        "patterns": {
            "l1": r'sensor\..*(?:i1|inverter).*ac_current_a.*',
            "l2": r'sensor\..*(?:i1|inverter).*ac_current_b.*',
            "l3": r'sensor\..*(?:i1|inverter).*ac_current_c.*',
        },
    },
    {
        "name": "Huawei/FusionSolar",
        "patterns": {
            "l1": r'sensor\..*inverter.*phase_a.*(?:current|power).*',
            "l2": r'sensor\..*inverter.*phase_b.*(?:current|power).*',
            "l3": r'sensor\..*inverter.*phase_c.*(?:current|power).*',
        },
    },
    {
        "name": "Solarman/Deye output",
        "patterns": {
            "l1": r'sensor\..*(?:output|inverter).*(?:l1|_1).*(?:current|power).*',
            "l2": r'sensor\..*(?:output|inverter).*(?:l2|_2).*(?:current|power).*',
            "l3": r'sensor\..*(?:output|inverter).*(?:l3|_3).*(?:current|power).*',
        },
    },
    {
        "name": "SMA",
        "patterns": {
            "l1": r'sensor\.sma.*phase.*l1.*current.*',
            "l2": r'sensor\.sma.*phase.*l2.*current.*',
            "l3": r'sensor\.sma.*phase.*l3.*current.*',
        },
    },
    {
        "name": "GoodWe",
        "patterns": {
            "l1": r'sensor\..*(?:goodwe|gw).*output.*(?:current|power).*(?:l1|_1|_r).*',
            "l2": r'sensor\..*(?:goodwe|gw).*output.*(?:current|power).*(?:l2|_2|_s).*',
            "l3": r'sensor\..*(?:goodwe|gw).*output.*(?:current|power).*(?:l3|_3|_t).*',
        },
    },
]
```

Wiring mode auto-detection:
- If battery entities are configured (SOC, battery power) → suggest series (hybrid inverter likely)
- If no battery entities → suggest parallel (AC-coupled solar likely)

### Model changes

**SiteContext** — add new fields:
```python
# Per-phase inverter output (from dedicated entities)
inverter_l1_output: float | None = None  # Current (A) or power converted to current
inverter_l2_output: float | None = None
inverter_l3_output: float | None = None
inverter_wiring_series: bool = False  # True = series (hybrid), False = parallel (AC-coupled)
```

**Computed per-phase household** — new helper method or computed in `dynamic_ocpp_evse.py`:
```python
# Parallel: household = grid_consumption + inverter_output - grid_export (per phase, after feedback)
# Series:   household = inverter_output - charger_draws (per phase)
```

Store result as `PhaseValues` on SiteContext:
```python
household_consumption: PhaseValues | None = None  # Per-phase household (A)
```

This replaces the scalar `household_consumption_total` with per-phase granularity. The total can be derived as `.total`.

### Engine changes

**`_calculate_solar_surplus()` asymmetric path** — use per-phase household when available:
```python
if site.inverter_supports_asymmetric:
    total_pool = export_total + battery_adjustment_total
    if site.household_consumption is not None:
        # Exact per-phase household from inverter output entities
        h_a = site.household_consumption.a or 0
        h_b = site.household_consumption.b or 0
        h_c = site.household_consumption.c or 0
    elif site.household_consumption_total is not None:
        # Uniform estimate from single solar entity
        h_a = h_b = h_c = (site.household_consumption_total / site.voltage) / num_phases
    else:
        # CT only — household invisible when self-consuming
        h_a = site.consumption.a or 0
        h_b = site.consumption.b or 0
        h_c = site.consumption.c or 0

    phase_a_limit = min(total_pool, max(0, max_per_phase - h_a))
    phase_b_limit = min(total_pool, max(0, max_per_phase - h_b))
    phase_c_limit = min(total_pool, max(0, max_per_phase - h_c))
```

Same pattern applies to `_calculate_inverter_limit()` for the Standard mode path.

### Test simulation changes

**`run_tests.py`** — new YAML fields:
```yaml
site:
  inverter_l1_output: 18.0  # A (or W, auto-converted)
  inverter_l2_output: 18.0
  inverter_l3_output: 18.0
  inverter_wiring_series: false
```

Simulation computes per-phase household from these values using the parallel/series formula, sets `site.household_consumption = PhaseValues(...)`.

### Interaction with existing features

- **Single solar entity**: Still works as fallback (level 2 accuracy). When per-phase entities are configured, solar entity becomes redundant.
- **Derived mode** (no entities): Still works as before (level 3 accuracy). Battery awareness still helps recover total surplus.
- **Symmetric inverters**: Per-phase household is less critical (symmetric path uses per-phase export which already reflects household). Still beneficial for consistency.
- **Phase mapping**: Charger draws are already mapped to site phases. `get_site_phase_draw()` used in household computation for series mode.

---

## Automatic L1/L2/L3 → A/B/C phase mapping detection
**Status:** Not yet implemented (manual configuration available)
**Complexity:** Medium

### Current State
- Users manually configure L1→A, L2→B, L3→C mapping per charger in config flow
- Default mapping assumes L1=A, L2=B, L3=C

### Proposed Approach
Automatically detect the physical phase mapping at runtime by correlating charger current changes with grid CT readings:

1. When a charger starts drawing on L1, observe which site phase (A/B/C) CT reading increases
2. Repeat for L2 and L3 by modulating charger current
3. Build the mapping table from observed correlations

### Technical Considerations
- Requires the charger to be actively charging (can't detect mapping when idle)
- Grid CT readings include household loads — need to filter out noise
- Could run as a one-time calibration step or continuous background detection
- May need statistical confidence threshold before applying mapping
- Should only override manual config if user opts in

### Implementation Steps (for future)
1. Add "auto-detect" option to phase mapping config (alongside manual A/B/C dropdowns)
2. Create detection routine that commands small current changes on specific OCPP phases
3. Correlate grid CT delta with commanded phase to build mapping
4. Store detected mapping in config entry options
5. Add UI feedback showing detected vs configured mapping

---

## Automatic grid current inversion detection
**Status:** Not yet implemented (manual toggle available via `invert_phases`)
**Complexity:** Low–Medium

### Current State
- Users manually toggle `invert_phases` in the hub grid config step
- Some grid CTs measure import as positive, others as negative — depends on clamp orientation
- Getting this wrong inverts the entire system's understanding of import vs export, causing completely wrong behavior (e.g., charging at full power when exporting, pausing when importing)

### Proposed Approach
Detect the correct polarity automatically by observing grid CT behavior when a charger starts or stops:

1. **Charger start event**: When a charger begins drawing power, grid import should increase (CT reading goes more positive). If the CT reading goes more negative instead, the polarity is inverted.
2. **Charger stop event**: When a charger stops, grid import should decrease. Same logic in reverse.
3. **Correlation check**: Compare the sign of the CT delta with the expected direction based on the charger event.

### Technical Considerations
- Only needs one clear start/stop event to determine polarity with high confidence
- Household load fluctuations add noise — use the charger's known draw magnitude as a threshold (delta must be at least 50% of charger draw to be conclusive)
- Could run once on first charger connection and store the result
- Should warn the user if detected polarity differs from configured `invert_phases` setting rather than silently overriding
- Works best with larger charger draws (3-phase 16A = ~11kW is unmistakable vs household noise)

### Implementation Steps (for future)
1. Track grid CT readings before and after charger start/stop events in `dynamic_ocpp_evse.py`
2. Compare delta sign with expected direction
3. If mismatch detected, create a persistent notification suggesting the user toggle `invert_phases`
4. Optionally: auto-correct with a "trust auto-detection" config option

---

## ~~Adding an entity selection for actual solar power in the config_flow~~

**Status:** Implemented (v2.0)

Implemented as TODO item 12 ("Dedicated Solar Power Entity"). Key decisions made:
- Added `CONF_SOLAR_PRODUCTION_ENTITY_ID` as optional field in hub grid schema
- When configured, `dynamic_ocpp_evse.py` reads the solar entity directly instead of deriving from `consumption + export`
- Dropdown in config flow lists power-class sensor entities
- Falls back to the derived approach when the entity is not configured
- 3 test scenarios in `test_scenarios_solar_entity.yaml`, all passing
