# Load Juggler — Improvements

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

## Circuit Groups — shared breaker limit for co-located loads
**Status:** Implemented (TODO #96)
**Complexity:** Medium

### Problem
Two 16A EVSEs on the same 20A circuit breaker. The engine knows per-load limits (6-16A each) and the site breaker (25A), but not the intermediate 20A circuit breaker. Without a circuit-level constraint, it could allocate 12A + 12A = 24A to the circuit, tripping the 20A breaker.

### Design

**New device type: Circuit Group** — a config entry linked to a hub, like chargers/plugs.

**Config flow:**
1. Select type "Circuit Group"
2. Set name + current limit (A) + linked hub
3. Multi-select member loads from existing loads on that hub

**Data model:**
```python
@dataclass
class CircuitGroup:
    group_id: str
    name: str
    current_limit: float  # per-phase limit (A)
    member_ids: list[str]  # charger_ids of member loads
```

No separate phase setting needed — the limit applies per-phase universally. A 20A breaker on a 3ph site = 20A on each phase. Works with PhaseConstraints naturally.

**Engine: post-distribution capping (new Step 6)**
After `_distribute_power()`, add `_enforce_circuit_groups()`:
1. For each circuit group, sum member allocations per phase (using each load's phase mapping)
2. If any phase sum > group limit, walk members in reverse priority order (lowest urgency + lowest priority first) and reduce allocations until within limit
3. If reducing drops a load below min_current → set to 0

**HA entities on the group device:**
- Circuit Allocation sensor (sum of member allocations)
- Circuit Headroom sensor (limit - allocation)

### Assumptions
- Only managed loads on the circuit (no background consumption from unmanaged loads)
- Each load belongs to at most one circuit group
- Loads not in any group are unconstrained (only site breaker applies)

### Future upgrade path
Post-distribution capping is simple but can "waste" headroom — the engine might over-allocate to a group then slash, while non-grouped loads could have used that capacity. If this matters in practice, upgrade to group-aware distribution where `_distribute_power()` deducts from both site pool and group budget simultaneously.


## Device-based OCPP charger discovery

**Problem:** Some OCPP chargers (e.g. Alfen) expose per-phase current as separate entities (`sensor.<charger>_current_import_l1`, `_l2`, `_l3`) instead of a single entity with L1/L2/L3 attributes. Our discovery only finds `sensor.<charger>_current_import` and reads phase data from its attributes.

**Solution:** Rewrite charger discovery to be device-based. User selects an OCPP **device** from the device registry, and we auto-discover all relevant entities from it (current_import, current_offered, status, per-phase current if separate). This also simplifies UX — one device pick instead of hoping entity naming matches.

**Scope:**
- Rework `_discover_ocpp_chargers()` in config_flow.py to scan by device, not entity suffix
- In `_build_evse_charger()`, support reading per-phase current from separate entities (store entity IDs during discovery)
- Fallback: keep attribute-based reading for chargers that use single-entity + attributes pattern
- Store discovered entity IDs in config entry data (current_import, current_import_l1/l2/l3, current_offered, status)

## Failure modes — DONE


### Issue 1: Grid CT sensor unavailability — IMPLEMENTED

When a configured grid CT sensor becomes `unavailable`/`unknown`, `_read_entity()` returns default 0, making the engine think there's zero site load — dangerous over-allocation.

**Solution** (in `dynamic_ocpp_evse.py`):
- After reading raw_phases, check each configured CT entity via `hass.states.get()`
- If unavailable: inject the last known EMA smoothed value (prevents EMA decay toward 0)
- If no prior EMA value exists: assume `main_breaker_rating` (worst-case safe)
- Track staleness via `hub_runtime["grid_stale_since"]` (monotonic clock)
- After `GRID_STALE_TIMEOUT` (60s): override all chargers to `min_current` (charging) or 0 (not charging)
- Recovery: when sensors return, resume normal operation with log message
- Hub sensor exposes `grid_stale` attribute (only shown when `True`)

### Issue 2: Site available power ignores max_grid_import_power — IMPLEMENTED

`total_site_available_power` and `available_grid_power` (hub sensors) only accounted for breaker headroom, ignoring the configured max grid import power entity/slider.

**Solution** (in `_build_hub_result()`):
- After computing `net_consumption`, cap `total_site_available` by `max_grid_import_power - net_consumption`
- Cap `grid_headroom` by `max_grid_import_power - post_feedback_consumption`
- These sensors now reflect the actual grid limit used by the calculation engine

### Issue 3: Broader sensor resilience — IMPLEMENTED

Extended failure handling beyond grid CT to all sensor types:

- `_read_entity()` now returns `_UNAVAILABLE` sentinel when a configured sensor is `unknown`/`unavailable`
- `_smooth()` holds last known EMA value when receiving `_UNAVAILABLE` (prevents decay to 0)
- `_smooth()` rejects `NaN`/`Inf` values (returns last EMA instead)
- Non-smoothed call sites use `_coerce()` helper to safely convert `_UNAVAILABLE` to appropriate defaults
- **Effect**: solar production, battery power, and inverter output sensors automatically hold their last value during brief unavailability

### Issue 4: OCPP/switch service call resilience — IMPLEMENTED

- `set_charge_rate` wrapped in try-except; `_last_commanded_limit` only updated on success
- Plug switch `turn_on`/`turn_off` wrapped in try-except with warning log
- Prevents entire update cycle crash if OCPP integration restarts

### Issue 5: Miscellaneous hardening — IMPLEMENTED

- Plug charger `connected_to_phase` fallback to "A" if empty (prevents division by zero)
- Voltage validation: `<= 0` falls back to `DEFAULT_PHASE_VOLTAGE` (230V)
- Circuit group stale member filtering: deleted charger entry_ids silently dropped with warning log

## Fallback to Power Offered if Current offered (total or per phase) is not available