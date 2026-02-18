# Improvements

This document is used for keeping notes of ideas for future implementations in no particular order. As long as the developer does not expicitly say to start implementing them, you can just use this as a reference for what might come, if that has any effect on current decisions. This will also keep any discussions about ideas. This way you can plan them easier with the developer.

## Supports for chargers with multiple plugs
**Status:** Not yet implemented
**Complexity:** Medium

### Current State
- Each charger is a single config entry with one OCPP connection
- One physical EVSE = one HA configuration entry

### Implementation Approach
Add `plug_id` field to `LoadContext`. Multiple plugs can:
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
1. Add `charging_mode` field to `LoadContext`
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
| `LoadContext` | `LoadContext` |
| `min_current` / `max_current` | `min_power` / `max_power` |
| Charging mode | Control strategy |
| Priority distribution | Load prioritization |

### Progress

**Phase 1: Smart Plug Support** — Done (TODO #6, #74).

**Phase 2: General Load Type** (higher effort, not started)
- Create separate `Load` entity type alongside `Charger`
- Each load has its own control entities
- Unified priority-based distribution across all loads

**Phase 3: Temperature-Based Control** (high effort, not started)
- Read temperature sensors
- Implement thermal models
- Schedule heating/cooling based on excess availability

---

---

## Auto-detect scoring: confidence-weighted points instead of flat sample counts

**Status:** Implemented (TODO #91)
**Complexity:** Medium

### Problem
The current auto-detect phase mapping uses flat sample counts (notify at 10, remap at 30). When a phase mapping is wrong, the engine allocates current to the wrong phase, causing start/stop oscillation. If the car experiences too many interruptions before we accumulate enough samples, it may refuse to charge entirely.

### Research: How EVs Handle Repeated Charging Interruptions

**No hard standard exists.** IEC 61851 defines CP pilot signal states (A/B/C/D/E/F) and PWM duty cycle communication, but does NOT specify a maximum number of start/stop cycles or mandatory cooldown between current changes. Behavior is entirely OEM-specific.

**Known behaviors (from forums, evcc community, real-world reports):**
- **Most EVs auto-retry** after an interruption — they re-enter state B (connected) and wait for state C (charging) again. There's no universal "3 strikes" rule.
- **Some cars fault after repeated rapid cycling.** Forum reports mention Kia EV6, Ford Mach-E, and some Renault ZOE models entering fault states after frequent start/stop. No consistent threshold found — ranges from ~5 to ~20+ cycles depending on the car, timing, and EVSE behavior.
- **Tesla** generally tolerant — retries indefinitely but may show "Charge interrupted" notifications. Some models add increasing delays between retries.
- **Hyundai/Kia ICCU** is known to be fragile — 2-10% of owners experienced ICCU-related failures in certain model years. Rapid cycling may exacerbate this.

**evcc's approach (industry reference):**
- **`guardduration`**: default **5 minutes** — minimum time between start→stop or stop→start transitions
- **`enable.delay`**: default **1 minute** — surplus must persist for 1 min before starting charge
- **`disable.delay`**: recommended **30 minutes** — deficit must persist 30 min before stopping
- **`Min+Solar` mode**: never stops charging, only adjusts current — specifically designed for "vehicles that do not like regular starting and stopping"
- evcc docs explicitly warn: *"Some vehicles refuse to charge if it is interrupted too often and must be made to charge again, for example by unlocking or plugging/unplugging the charging cable."*

**Key insight:** The danger isn't gradual current changes (PWM adjustments) — it's full **start/stop transitions** (CP state C→B→C). Adjusting current from 10A→6A is fine. Going to 0A (pause) and back is what stresses cars.

### Current System Context

Our system has two separate concerns:
1. **Grace period timer** (sensor.py) — already prevents rapid pause/resume in Solar Only and Excess modes. Configurable hold-at-min before pausing.
2. **Auto-detect phase mapping** — the oscillation happens because wrong phase mapping → engine allocates 0A on the wrong phase → pause → resume → repeat. This is the root cause, not a symptom we should mask with longer timers.

### Proposed: Confidence-Weighted Scoring

Replace flat `sample_count >= threshold` with a point-based system:

| Condition | Points |
|---|---|
| Correlating sample with confidence > 90% (best_phase matches >90% of votes) | +3 |
| Correlating sample with confidence > 80% | +2 |
| Correlating sample with confidence > 70% | +1 |
| Contradicting sample (different phase leads) | -1 |

**Thresholds:**
- **Notify** at score >= 10 (e.g., 4 strong samples or 10 weak ones)
- **Auto-remap** at score >= 25

**Benefits:**
- Strong signals (large delta_draw + clear single-phase correlation) trigger faster
- Noisy signals accumulate more slowly
- A single contradicting sample doesn't reset everything (unlike current "reset on inconclusive")

### Discussion Points
- Should we also weight by `|delta_draw|`? A 10A change is much more informative than a 1A change.
- The current "reset on inconclusive" approach is aggressive — might be better to decay points slowly instead of hard reset.
- The real fix for oscillation is the remap itself. Once applied, the oscillation stops. So speed of detection matters more than avoiding false positives (which can be corrected by re-detection after reset).

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
