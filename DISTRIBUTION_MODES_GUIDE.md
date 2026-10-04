# Load Juggler - Distribution Modes Guide

When multiple loads are connected to a single hub, the distribution mode determines how available current is allocated between them. Shared and Priority first give every load its minimum and then distribute the rest; Optimized and Strict serve the loads one after another, in priority order.

After distribution, [circuit group limits](#circuit-groups) are enforced as an additional constraint.

## Quick Comparison

| Mode | Strategy | When to use |
|------|----------|-------------|
| **Shared** | Every minimum, then an equal split | Fair distribution, no priority differences |
| **Priority** | Every minimum, then the higher priority first | One vehicle needs faster charging, both should charge |
| **Optimized** | Higher priority up to its maximum; only the current left beyond that is shared | One vehicle charges at full speed, the spare runs a second |
| **Strict** | Higher priority up to its maximum; the next gets only what is left | Absolute priority enforcement |

Two three-phase chargers, both 6-16 A, charger 1 at the higher priority, both cars taking all they are offered. Each row is the current available to the two of them, per phase; each cell is charger 1 / charger 2:

| Available | Shared | Priority | Optimized | Strict |
|-----------|--------|----------|-----------|--------|
| 26 A | 13 / 13 | 16 / 10 | 16 / 10 | 16 / 10 |
| 24 A | 12 / 12 | 16 / 8 | 16 / 8 | 16 / 8 |
| 22 A | 11 / 11 | 16 / 6 | 16 / 6 | 16 / 6 |
| 20 A | 10 / 10 | 14 / 6 | 14 / 6 | 16 / 0 |
| 18 A | 9 / 9 | 12 / 6 | 12 / 6 | 16 / 0 |
| 16 A | 8 / 8 | 10 / 6 | 16 / 0 | 16 / 0 |
| 12 A | 6 / 6 | 6 / 6 | 12 / 0 | 12 / 0 |
| 11 A | 11 / 0 | 11 / 0 | 11 / 0 | 11 / 0 |

---

## Shared Mode

**Algorithm:**

1. Allocate minimum current to each active charger, in priority order
2. Distribute the remaining current equally among the chargers that got their minimum

Below both minimums the first charger takes it all.

**When to use:**

- Fair distribution among all chargers
- Multiple cars charging simultaneously
- No priority differences between vehicles

**Example:**

```text
Available: 20A per phase
Charger 1: min=6A, max=16A, priority=1
Charger 2: min=6A, max=16A, priority=2

Step 1 - Allocate minimums:
  Charger 1: 6A
  Charger 2: 6A
  Remaining: 8A

Step 2 - Distribute equally:
  Share: 8A / 2 = 4A each
  Final: 10A / 10A

With 11A: only one minimum fits -> Charger 1 takes it all, 11A / 0A
```

---

## Priority Mode

**Algorithm:**

1. Allocate minimum current to each active charger, in priority order
2. Give the remaining current to the highest priority charger up to its maximum, then to the next

Below both minimums the first charger takes it all.

**When to use:**

- One vehicle needs faster charging, but both should charge
- Company car vs. visitor car
- Primary vehicle vs. secondary vehicle

**Example:**

```text
Available: 20A per phase
Charger 1: min=6A, max=16A, priority=1 (higher priority)
Charger 2: min=6A, max=16A, priority=2

Step 1 - Allocate minimums:
  Charger 1: 6A
  Charger 2: 6A
  Remaining: 8A

Step 2 - Distribute by priority:
  Charger 1 gets first: 6A + 8A = 14A
  Charger 2 stays at: 6A
  Final: 14A / 6A

With 12A: both minimums, nothing left -> 6A / 6A
With 11A: only one minimum fits -> Charger 1 takes it all, 11A / 0A
```

---

## Optimized Mode (Sequential)

**Algorithm:**

- Process chargers in priority order; each gets up to its maximum
- When current is left over beyond a charger's maximum, but less than the next charger's minimum, the charger is trimmed - never below its own minimum - so the next one reaches its minimum
- With nothing left over beyond its maximum, a charger takes it all and the next gets nothing; it is never trimmed for a charger that still could not start
- To start the next charger, at least 1 A must be left over beyond the higher priority charger's maximum; once it runs, it keeps its minimum until nothing is left over. A supply hovering just above the first charger's maximum therefore does not switch the second one on and off

**When to use:**

- The higher priority vehicle should charge at full speed whenever the supply is no more than it can take
- Spare current above that should still run a second vehicle rather than go unused

**Example:**

```text
Available: 18A per phase
Charger 1: min=6A, max=16A, priority=1
Charger 2: min=6A, max=16A, priority=2

Processing:
  Charger 1: Can use up to 16A -> 2A left over
  Charger 2: Needs 6A, 2A left -> Charger 1 trimmed by 4A to 12A
  Final: 12A / 6A

With 17A: 1A left over -> 11A / 6A (Charger 2 starts here)
With 16.5A: 10.5A / 6A if Charger 2 is running, 16.5A / 0A if it is not
With 16A: nothing left beyond Charger 1's 16A -> 16A / 0A
With 12A: nothing left over -> 12A / 0A (Priority would give 6A / 6A)
```

---

## Strict Mode (Sequential)

**Algorithm:**

- Process chargers in strict priority order
- The next charger gets only what is left once the previous one has all it can take, up to its maximum, and only if that reaches its minimum
- A higher priority charger is never trimmed for a lower one

**When to use:**

- Absolute priority enforcement
- One vehicle must be fully satisfied before others start
- Critical vehicle charging

**Example (constrained):**

```text
Available: 20A per phase
Charger 1: min=6A, max=16A, priority=1
Charger 2: min=6A, max=16A, priority=2

Processing:
  Charger 1: Gets 16A (at max) -> fully satisfied
  Remaining: 4A
  Charger 2: Needs min 6A, only 4A available -> gets 0A
  Final: 16A / 0A
```

**Example (room for both):**

```text
Available: 26A per phase
Charger 1: min=6A, max=16A, priority=1
Charger 2: min=6A, max=16A, priority=2

Processing:
  Charger 1: Gets 16A (at max) -> fully satisfied
  Remaining: 10A
  Charger 2: Gets 10A (at least its 6A minimum)
  Final: 16A / 10A
```

---

## Configuration

### Load-Level Parameters

| Parameter | Description | Default |
|-----------|-------------|---------|
| **Load Priority** | Priority for distribution (1-10, lower = higher) | 1 |
| **Min Current** | Minimum charge rate (A) - load gets this or 0 | 6A |
| **Max Current** | Maximum charge rate (A) | 16A |

### Key Rules

- Loads need >= min_current or they get 0A (can't operate below minimum)
- Distribution mode is set at the **hub level** (applies to all loads on that hub)
- Priority value 1 is highest, 10 is lowest
- Mode urgency takes precedence over priority number: Standard/Continuous loads are always allocated before Solar Priority, which comes before Solar Only, etc.
- Only active loads participate in distribution (EVSE must have car plugged in and ready, smart plugs must be connected)
- Circuit group limits are enforced **after** distribution - they can reduce allocations but never increase them

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
