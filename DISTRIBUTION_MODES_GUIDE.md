# Load Juggler — Distribution Modes Guide

When multiple loads are connected to a single hub, the distribution mode determines how available current is allocated between them. All modes use a two-step approach: first ensure minimums, then distribute the remainder.

After distribution, [circuit group limits](#circuit-groups) are enforced as an additional constraint.

## Quick Comparison

| Mode | Strategy | When to use |
|------|----------|-------------|
| **Shared** | Equal split after minimums | Fair distribution, no priority differences |
| **Priority** | Higher priority gets remainder first | One vehicle needs faster charging |
| **Optimized** | Sequential but shares leftover | Priority with efficient power use |
| **Strict** | Fully satisfy highest priority first | Absolute priority enforcement |

---

## Shared Mode

**Algorithm:**

1. Allocate minimum current to each active charger
2. Distribute remaining current equally among all active chargers

**When to use:**

- Fair distribution among all chargers
- Multiple cars charging simultaneously
- No priority differences between vehicles

**Example:**

```text
Available: 32A total
Charger 1: min=6A, max=16A, priority=1
Charger 2: min=6A, max=16A, priority=2

Phase 1 - Allocate minimums:
  Charger 1: 6A
  Charger 2: 6A
  Remaining: 20A

Phase 2 - Distribute equally:
  Share: 20A / 2 = 10A each
  Charger 1: 6A + 10A = 16A (at max)
  Charger 2: 6A + 10A = 16A (at max)
  Final: Both at 16A
```

---

## Priority Mode

**Algorithm:**

1. Allocate minimum current to each active charger (in priority order)
2. Distribute remaining current by priority (fully satisfy higher priority first)

**When to use:**

- One vehicle needs faster charging
- Company car vs. visitor car
- Primary vehicle vs. secondary vehicle

**Example:**

```text
Available: 20A total
Charger 1: min=6A, max=16A, priority=1 (higher priority)
Charger 2: min=6A, max=16A, priority=2

Phase 1 - Allocate minimums:
  Charger 1: 6A
  Charger 2: 6A
  Remaining: 8A

Phase 2 - Distribute by priority:
  Charger 1 gets first: 6A + 8A = 14A
  Charger 2 stays at: 6A
  Final: 14A / 6A
```

---

## Optimized Mode (Sequential)

**Algorithm:**

- Process chargers in priority order
- Each charger gets up to its max (or remaining available)
- If can't reach minimum, skip and continue to next
- Allows "leftover" current from higher priority to flow to lower priority

**When to use:**

- Want priority but don't want to waste available current
- Higher priority charger has lower max than available
- Efficient use of all available power

**Example:**

```text
Available: 32A total
Charger 1: min=6A, max=10A, priority=1
Charger 2: min=6A, max=16A, priority=2

Processing:
  Charger 1: Can use up to 10A -> gets 10A
  Remaining: 22A
  Charger 2: Can use up to 16A -> gets 16A
  Final: 10A / 16A (total 26A used)
```

---

## Strict Mode (Sequential)

**Algorithm:**

- Process chargers in strict priority order
- Next charger only gets power if previous is fully satisfied (at max)
- Lower priority chargers may get nothing

**When to use:**

- Absolute priority enforcement
- One vehicle must be fully satisfied before others start
- Critical vehicle charging

**Example (constrained):**

```text
Available: 20A total
Charger 1: min=6A, max=16A, priority=1
Charger 2: min=6A, max=16A, priority=2

Processing:
  Charger 1: Gets 16A (at max) -> fully satisfied
  Remaining: 4A
  Charger 2: Needs min 6A, only 4A available -> gets 0A
  Final: 16A / 0A
```

**Example (generous):**

```text
Available: 32A total
Charger 1: min=6A, max=16A, priority=1
Charger 2: min=6A, max=16A, priority=2

Processing:
  Charger 1: Gets 16A (at max) -> fully satisfied
  Remaining: 16A
  Charger 2: Gets 16A (at max) -> fully satisfied
  Final: 16A / 16A
```

---

## Configuration

### Load-Level Parameters

| Parameter | Description | Default |
|-----------|-------------|---------|
| **Load Priority** | Priority for distribution (1-10, lower = higher) | 1 |
| **Min Current** | Minimum charge rate (A) — load gets this or 0 | 6A |
| **Max Current** | Maximum charge rate (A) | 16A |

### Key Rules

- Loads need >= min_current or they get 0A (can't operate below minimum)
- Distribution mode is set at the **hub level** (applies to all loads on that hub)
- Priority value 1 is highest, 10 is lowest
- Mode urgency takes precedence over priority number: Standard/Continuous loads are always allocated before Solar Priority, which comes before Solar Only, etc.
- Only active loads participate in distribution (EVSE must have car plugged in and ready, smart plugs must be connected)
- Circuit group limits are enforced **after** distribution — they can reduce allocations but never increase them

---

## Circuit Groups

Circuit groups add an intermediate breaker constraint between the site breaker and individual loads. Use them when multiple loads share a sub-breaker (e.g., two 16A EVSEs on a 20A circuit breaker).

### How It Works

1. Distribution mode allocates power as usual (Shared, Priority, etc.)
2. After distribution, circuit group limits are enforced per phase
3. If the combined allocation of group members exceeds the group limit on any phase, members are reduced in reverse priority order until the limit is satisfied
4. If reducing a load drops it below its min_current, it is set to 0A

### Configuration

Create a circuit group via **Settings > Devices & Services > Add Integration > Load Juggler > Circuit Group**:

| Field | Description |
|-------|-------------|
| **Name** | Display name for the group |
| **Current Limit** | Maximum current per phase (A) for all members combined |
| **Hub** | Which hub this group belongs to |
| **Members** | Select which loads belong to this group |

### Example

```text
Site breaker: 25A per phase
Circuit group "Garage": 20A limit
  - EVSE 1: 6-16A, priority 1
  - EVSE 2: 6-16A, priority 2

Distribution allocates: EVSE 1=12A, EVSE 2=12A (24A total)
Circuit group enforces: 24A > 20A limit
  → EVSE 2 reduced to 8A (lower priority)
  → Final: EVSE 1=12A, EVSE 2=8A (20A total)
```

### HA Entities

Each circuit group creates a sensor showing:
- **State**: Current allocation (sum of member draws on heaviest phase)
- **Attributes**: per-phase draw breakdown, headroom, member list
