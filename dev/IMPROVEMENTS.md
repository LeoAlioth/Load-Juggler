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


## ~~Relative time OCPP charging profiles~~ DONE
**Status:** Implemented — `CONF_PROFILE_VALIDITY_MODE` (Absolute/Relative) per-charger in config flow, with `duration`-based profiles in `sensor.py` line 453-470.
**Source:** GitHub issue #18 (Charge Offered instability)

---

## Conditional entity visibility based on configuration
**Status:** Partially implemented (battery entities already conditional)
**Complexity:** Low (remaining items)
**Source:** GitHub issue #13 (control panel and useful tips)

### Already Done
Battery entities are hidden when no battery is configured (`has_battery` check):
- Battery SOC Target slider, Battery SOC Min slider, Allow Grid Charging switch (number.py, switch.py)
- Battery SOC, Battery Power, Available Battery Power, Site Battery Available Power sensors (sensor.py `requires_battery` flag)
- Smart plug vs EVSE entities already branch correctly (Device Power slider vs Min/Max Current sliders)

### Remaining Candidates
- **Phase B/C available current sensors** — always created but useless on 1-phase sites (always 0). Could hide when only Phase A entity is configured in the hub.
- Solar surplus sensors are legitimately always-visible (derived from grid CT export even without dedicated solar entity).

---

## Expose HA service actions for automations
**Status:** Partially available (select entity workaround exists)
**Complexity:** Low
**Source:** GitHub discussion #8 (Time of day / free power)

### Current State
Users can change charging mode via HA automations by targeting the select entity directly (e.g., `select.set_option` on the charging mode entity). This works but isn't discoverable — users expect to find actions under the device/integration in the automation editor.

### Proposed Approach
Register HA services under the integration domain:
- `dynamic_ocpp_evse.set_charging_mode` (mode: Standard/Eco/Solar/Excess)
- `dynamic_ocpp_evse.set_max_current` (current: float)
- `dynamic_ocpp_evse.pause_charging` / `resume_charging`

### Technical Considerations
- Services are registered in `__init__.py` via `hass.services.async_register()`
- Each service needs a schema and handler function
- Low effort since the underlying entity operations already exist

---

## User documentation / setup guide
**Status:** Not yet created
**Complexity:** Low (writing, not code)
**Source:** GitHub issues #9, #11

### Content Needed
Several users have asked about:
- Initial setup workflow (install integration → configure hub → add charger → start charging)
- What each charging mode does in practice (especially with/without battery)
- What "Allow grid charging" does (only relevant for battery systems)
- How to automate charging modes (time-of-day, tariff-based)
- How min/max current settings interact with charging modes
- Troubleshooting: charger rejecting profiles, current not adjusting

### Proposed Approach
Create a `docs/` directory or a wiki page with:
1. Quick start guide
2. Configuration reference
3. Charging modes explained (with/without battery)
4. Common automations (time-based, tariff-based)
5. Troubleshooting FAQ

---

## GitHub issue triage (reviewed 2026-02-16)

### Can be closed (fixed in v2.0.0)
- **#3** — `UnboundLocalError: target_evse` + deprecated methods → v1.1.1 code, fully rewritten in v2.0.0
- **#7** — Single phase installation validation error → fixed since v1.2.1 (phases 2/3 optional)
- **#12** — Multi-charger support → implemented in v2.0.0
- **#14** — Huawei charger rejecting Amps profiles → charge rate unit auto-detection added (TODO #14)

### Need follow-up testing on v2.0.0
- **#18** — Charge Offered instability (clock drift) → needs relative time profile mode (see improvement above)
- **#19** — Solar mode not working → likely fixed by v2.0.0 solar refactoring; user testing v2.0.0-pre release

### Feature requests (tracked as improvements above)
- **#13** — Conditional entity visibility, control panel
- **#11** — User guide / documentation
- **#9** — "Allow grid charging" documentation

### Discussions
- **#8** — Time-of-day charging → resolved via select entity workaround; HA actions improvement noted above
- **#5** — HomeWizard P1 + WallBox setup → user testing v2.0.0
- **#4** — Helper setting clarification → old v1.x question, resolved
