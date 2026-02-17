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

**Phase 1: Smart Plug Support** — Done (TODO #13).

**Phase 2: General Load Type** (higher effort, not started)
- Create separate `Load` entity type alongside `Charger`
- Each load has its own control entities
- Unified priority-based distribution across all loads

**Phase 3: Temperature-Based Control** (high effort, not started)
- Read temperature sensors
- Implement thermal models
- Schedule heating/cooling based on excess availability

---

## Minimum run timer for Solar/Excess modes (anti-flicker)
**Status:** Not yet implemented (Excess mode had `_excess_charge_start_time` but it was removed)
**Complexity:** Low

### Problem
In Solar and Excess modes, short transient events — a cloud passing for a few seconds, a startup surge from a large appliance — can cause the charger to pause immediately. This creates unnecessary charge/pause cycling that's bad for the charger and the car.

**Excess mode** previously had `_excess_charge_start_time` as a minimum run timer, ensuring that once excess charging started it wouldn't stop immediately. This was removed but the concept is still valid.

**Solar mode** has no such protection at all — a momentary dip below minimum current causes an immediate pause.

### Proposed Approach
Add a configurable minimum run timer that prevents a charging session from pausing until the timer expires. Once charging starts (or resumes from pause), it must run for at least N seconds before the system is allowed to pause again.

### Technical Considerations
- Default: 30–60 seconds (configurable via number entity or config flow)
- Applies to Solar and Excess modes (and potentially Eco mode)
- Should NOT prevent pausing when the user changes mode or an error occurs
- Only applies to automatic solar-driven pauses, not manual control
- Reuse the same mechanism for both modes (single `_charge_started_at` timestamp)

---

## Excess mode: battery site behavior & "truly excess" definition
**Status:** Needs design refinement and test scenario updates
**Complexity:** Medium

### Current State

**No-battery sites**: Apart from needing the minimum run timer (see above), these are straightforward — excess is whatever is being exported to the grid above the threshold.

**Battery sites**: The current logic and test scenarios have gaps in how they define "excess" when a battery is present.

### What "truly excess" means

Solar power is only truly excess when it has **nowhere else to go**. On a battery site, solar first charges the battery. It only becomes excess when:

1. **Battery SOC hits 100%** — battery is full, can't absorb any more. Should use 3% hysteresis (same as target/min SOC), so charging starts at 100% and the battery is allowed to drop to 97% before EV charging pauses. This prevents the EV from draining the battery — it lets the battery fill up completely first.
2. **Battery max charge rate is exceeded** — solar production is higher than what the battery can absorb (e.g., 8kW solar but battery can only charge at 5kW). The 3kW overflow is excess.

### Test scenario issue

Current tests assume all solar gets exported when battery is at 97%. In reality, the inverter would still be charging the battery at that SOC (possibly at a reduced rate), exporting only part of the solar production. The test scenarios need updating to reflect this — at 97% SOC, most solar still goes to the battery.

### Export threshold on battery vs non-battery sites

The `excess_export_threshold` must be respected on **all** site types:

- **Export allowed + threshold set**: Works naturally — export exceeds threshold → charge.
- **No export allowed (threshold = 0)**: The inverter will never push power to the grid (except transients). In this case, excess must be detected differently — by looking at whether the battery charge rate is being capped, or battery SOC = 100%. The export threshold of 0 effectively means "any energy that the site physically can't use or store".

### Open questions
- How to detect excess on a no-export site? Battery charge rate saturation? SOC = 100%?
- Should the threshold be applied differently when `allow_export = false`?
- Do we need a separate config flag for "site allows grid export"?

---

## Charging profile IDs as config flow options
**Status:** Not yet implemented
**Complexity:** Low

### Current State
Charging profile IDs are hardcoded (`10` for reset, `11` for active profiles) to avoid conflicts with device-specific profiles. These values work for most chargers but some devices may use overlapping IDs.

### Proposed Approach
Expose `chargingProfileId` as an advanced config flow option with the current defaults (10/11). This allows users with non-standard chargers to pick IDs that don't conflict.

---

## Automatic L1/L2/L3 → A/B/C phase mapping detection
**Status:** Not yet implemented (manual configuration available via TODO #43)
**Complexity:** Medium

### Proposed Approach
Automatically detect the physical phase mapping at runtime by correlating charger current changes with grid CT readings:

1. When a charger starts drawing on L1, observe which site phase (A/B/C) CT reading increases
2. Repeat for L2 and L3 by modulating charger current
3. Build the mapping table from observed correlations

### Technical Considerations
- Requires the charger to be actively charging (can't detect mapping when idle)
- Grid CT readings include household loads — need to filter out noise
- Could run as a one-time calibration step or continuous background detection
- Should only override manual config if user opts in

---

## Automatic grid current inversion detection
**Status:** Not yet implemented (manual toggle available via `invert_phases`)
**Complexity:** Low–Medium

### Proposed Approach
Detect the correct polarity automatically by observing grid CT behavior when a charger starts or stops:

1. **Charger start event**: When a charger begins drawing power, grid import should increase. If the CT reading goes more negative instead, the polarity is inverted.
2. **Charger stop event**: Same logic in reverse.
3. **Correlation check**: Compare the sign of the CT delta with the expected direction based on the charger event.

### Technical Considerations
- Only needs one clear start/stop event to determine polarity with high confidence
- Household load fluctuations add noise — use the charger's known draw magnitude as a threshold
- Should warn the user if detected polarity differs from configured setting rather than silently overriding

---

## GitHub issue triage (reviewed 2026-02-17)

### Can be closed (fixed in v2.0.0)
- **#3** — `UnboundLocalError: target_evse` + deprecated methods → v1.1.1 code, fully rewritten in v2.0.0
- **#7** — Single phase installation validation error → fixed since v1.2.1 (phases 2/3 optional)
- **#12** — Multi-charger support → implemented in v2.0.0
- **#14** — Huawei charger rejecting Amps profiles → charge rate unit auto-detection added (TODO #14)
- **#13** — Conditional entity visibility → implemented (TODO #53, Phase B/C hiding + battery entities)
- **#11** — User guide / documentation → README.md rewritten (TODO #55)
- **#9** — "Allow grid charging" documentation → covered in README.md
- **#8** — Time-of-day charging → HA service actions implemented (TODO #54)
- **#4** — Helper setting clarification → old v1.x question, resolved

### Need follow-up testing on v2.0.0
- **#18** — Charge Offered instability (clock drift) → relative time profile mode implemented (CONF_PROFILE_VALIDITY_MODE)
- **#19** — Solar mode not working → likely fixed by v2.0.0 solar refactoring; user testing v2.0.0-pre release
- **#5** — HomeWizard P1 + WallBox setup → user testing v2.0.0
