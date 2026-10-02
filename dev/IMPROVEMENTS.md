# Load Juggler - Improvements

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
**Status:** EVSEs, smart plugs, hot water tanks and power stations are all managed loads under the one distribution; HVAC and thermal models are not started
**Complexity:** High (but incremental path possible)

### Vision
Extend beyond EV charging and the loads above to any controllable load:
- HVAC systems (space heating/cooling)
- Other flexible appliances

### Why It Fits Well

The current architecture is already quite general:

| Current EVSE Concept | General Load Equivalent |
|---------------------|------------------------|
| `LoadContext` | `LoadContext` |
| `min_current` / `max_current` | `min_power` / `max_power` |
| Operating mode | Control strategy |
| Priority distribution | Load prioritization |

### Next: Temperature-Based Control beyond the tank (high effort, not started)
The hot water tank already reads its temperature against its setpoints. Still open:
- Implement thermal models
- Schedule heating/cooling based on excess availability

---

## EV charging interruption research (reference)

**No hard standard exists.** IEC 61851 defines CP pilot signal states but does NOT specify max start/stop cycles. Behavior is OEM-specific:
- **Most EVs auto-retry** - no universal "3 strikes" rule
- **Some cars fault after rapid cycling** - Kia EV6, Ford Mach-E, Renault ZOE reported, ~5 to ~20+ cycles
- **Tesla** generally tolerant, retries indefinitely
- **Hyundai/Kia ICCU** known to be fragile

**evcc's approach:** `guardduration` 5 min between start/stop, `disable.delay` 30 min before pausing. Their `Min+Solar` mode never stops charging - designed for sensitive cars.

**Key insight:** The danger is full start/stop transitions (CP state C→B→C), not gradual current changes. The auto-detect remap eliminates the root cause of oscillation.

---

## Circuit groups: group-aware distribution
**Status:** Not yet implemented - groups are enforced by post-distribution capping (`target_calculator._enforce_circuit_groups`)

Post-distribution capping is simple but can "waste" headroom - the engine might over-allocate to a group then slash, while non-grouped loads could have used that capacity. If this matters in practice, upgrade to group-aware distribution where `_distribute_power()` deducts from both site pool and group budget simultaneously.

## Cascaded inverters - child on the parent's load/backup port
**Status:** Not yet implemented (requested 2026-08-17; real site: SolarEdge AC-coupled on the Deye's load port)
**Complexity:** Medium-high (fleet maths)

### Problem
The fleet model treats all inverters as parallel peers on the site bus. A cascaded setup - an AC-coupled inverter wired to a hybrid's load/backup port - breaks that: the child's output flows THROUGH the parent, so the fleet currently over-counts capacity and may double-count production.

### Idea
Optional field on the inverter entry: **"Output feeds"** - a selector of the other inverters on the same hub (default: the grid/site bus, today's behavior). Validation: same hub only, no cycles.

Engine implications to work through:
1. **Throughput capping** - the parent's `inverter_max_power`(/per-phase) must cap its own output PLUS the child's passthrough; `fleet.inverter_limits` / `sum_outputs` need a nested (tree) model instead of a flat sum.
2. **Double counting** - establish whether the parent's output sensors already include the child's passthrough power (measurement point question - on a Deye the load-port input likely does NOT appear on its grid-side output sensors, but must be verified on the real site). `solar_total` must count the child's production exactly once.
3. **Behavioral gains** - child production can charge the parent's battery (the point of this wiring); off-grid, the child is only alive while the parent is up; the child is effectively "series behind the parent" regardless of its own topology field.
4. **Display** - Overview/Summary pages render the relationship, e.g. "Solaredge Inverter · 10000 W · symmetric · behind DEYE Inverter (load port)".

First step when picked up: measure on the real site (child exporting hard, parent idle/charging/discharging) to pin down what each Deye sensor actually includes before touching the fleet maths.

## Dry-run mode + Debug options page
**Status:** Not yet implemented - idea from the Adaptive Cover Pro discussion (2026-08-17). The companion Overview and "How it decides" summary pages are NOT part of this item; they are being built directly into the options flow alongside the reconfigure→options collapse.
**Complexity:** Medium

### Idea
A third read-only page in the hub's options ("Configure") menu - **Debug** - next to Overview and Summary, mirroring Adaptive Cover Pro's Debug & Diagnostics screen:

- **Dry Run switch (hub-level)** - the engine runs its full cycle every interval, but ALL actuation is suppressed: OCPP profiles (`control/ocpp.py`), plug switches (`control/plug.py`), inverter register writes (`control/inverter.py`), tank climate calls (`control/hot_water_tank.py`), power-station writes (`control/power_station.py`). Instead, log at INFO and publish a per-load "last decision" attribute - what *would* have been sent and why (AC Pro's "Decision Trace" / "Last Skipped Action" pattern). Gate it at the single dispatch choke point in `entities/load.py`, not per control module, so future device types inherit it automatically.
- **Debug log promotion** - multi-select of log areas (engine, distribution, OCPP, compliance, auto-detect) promoted from DEBUG to INFO without touching YAML/logger config.
- **`diagnostics.py` platform** - standard HA diagnostics: downloadable JSON of entry config (redacted entity IDs optional) + the latest `hub_data` snapshot, for attaching to bug reports. ~50 lines, independent of the rest - could land first.

### Why
Config validation without moving real loads (new installs, phase-mapping experiments); dramatically better support/bug-report loops.