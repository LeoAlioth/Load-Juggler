# Improvements

This document is used for keeping notes of ideas for future implementations in no particular order. As long as the developer does not explicitly say to start implementing them, you can just use this as a reference for what might come, if that has any effect on current decisions. This will also keep any discussions about ideas. This way you can plan them easier with the developer.

## Support for chargers with multiple plugs
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

## Making this a general load management project
**Status:** Phase 1 complete (smart plug + per-load operating modes), Phases 2-3 not started
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
| Operating mode | Control strategy |
| Priority distribution | Load prioritization |

### Progress

**Phase 1: Smart Plug + Per-Load Operating Modes** — Done (TODO #6, #74, #79-85).
- Smart plug device type with power monitoring
- Per-load operating mode selection (Continuous, Solar Priority, Solar Only, Excess)
- Dual-pool distribution engine (physical pool + mode-aware ceilings)
- Mode urgency sorting (Continuous > Solar Priority > Solar Only > Excess)
- Mixed-mode scenarios across chargers and plugs

**Phase 2: General Load Type** (higher effort, not started)
- Create separate `Load` entity type alongside `Charger`
- Each load has its own control entities
- Unified priority-based distribution across all loads

**Phase 3: Temperature-Based Control** (high effort, not started)
- Read temperature sensors
- Implement thermal models
- Schedule heating/cooling based on excess availability

---

## Auto-detect scoring: confidence-weighted points instead of flat sample counts

**Status:** Implemented (TODO #91)
**Complexity:** Medium

### Problem
Flat sample counts (notify at 10, remap at 30) were too slow when phase mapping was wrong — the engine would oscillate (allocate wrong phase → pause → resume → repeat) for 30+ cycles before correcting. Some cars refuse to charge after too many interruptions.

### Implementation
Replaced flat counts with weighted scoring:
- `weight = min(abs(delta_draw), 15) / 5` — strong signals (large current swings) score higher
- Notify at score >= 6.0, auto-remap at score >= 15.0
- Soft decay (×0.5) on inconclusive data instead of hard reset — preserves partial progress
- Strong oscillation signals (20A swings) trigger remap in ~5 samples vs 30 flat samples

### EV Charging Interruption Research (reference)

**No hard standard exists.** IEC 61851 defines CP pilot signal states but does NOT specify max start/stop cycles. Behavior is OEM-specific:
- **Most EVs auto-retry** — no universal "3 strikes" rule
- **Some cars fault after rapid cycling** — Kia EV6, Ford Mach-E, Renault ZOE reported, ~5 to ~20+ cycles
- **Tesla** generally tolerant, retries indefinitely
- **Hyundai/Kia ICCU** known to be fragile

**evcc's approach:** `guardduration` 5 min between start/stop, `disable.delay` 30 min before pausing. Their `Min+Solar` mode never stops charging — designed for sensitive cars.

**Key insight:** The danger is full start/stop transitions (CP state C→B→C), not gradual current changes. The auto-detect remap eliminates the root cause of oscillation.

---

## GitHub issue triage (reviewed 2026-02-17)

### Can be closed (fixed in v2.0.0)
- **#3** — `UnboundLocalError: target_evse` + deprecated methods → v1.1.1 code, fully rewritten in v2.0.0
- **#7** — Single phase installation validation error → fixed since v1.2.1 (phases 2/3 optional)
- **#12** — Multi-charger support → implemented in v2.0.0
- **#14** — Huawei charger rejecting Amps profiles → charge rate unit auto-detection added (TODO #7)
- **#13** — Conditional entity visibility → implemented (TODO #29, Phase B/C hiding + battery entities)
- **#11** — User guide / documentation → README.md rewritten (TODO #31)
- **#9** — "Allow grid charging" documentation → covered in README.md
- **#8** — Time-of-day charging → HA service actions implemented (TODO #30)
- **#4** — Helper setting clarification → old v1.x question, resolved

### Need follow-up testing on v2.0.0
- **#18** — Charge Offered instability (clock drift) → relative time profile mode implemented (CONF_PROFILE_VALIDITY_MODE)
- **#19** — Solar mode not working → likely fixed by v2.0.0 solar refactoring; user testing v2.0.0-pre release
- **#5** — HomeWizard P1 + WallBox setup → user testing v2.0.0
