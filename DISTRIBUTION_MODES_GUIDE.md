# Dynamic OCPP EVSE - Distribution Modes Guide

When multiple chargers are connected to a single hub, the distribution mode determines how available current is allocated between them. All modes use a two-step approach: first ensure minimums, then distribute the remainder.

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

### Charger-Level Parameters

| Parameter | Description | Default |
|-----------|-------------|---------|
| **Charger Priority** | Priority for distribution (1-10, lower = higher) | 1 |
| **Min Current** | Minimum charge rate (A) — charger gets this or 0 | 6A |
| **Max Current** | Maximum charge rate (A) | 16A |

### Key Rules

- Chargers need >= min_current or they get 0A (can't charge below minimum)
- Distribution mode is set at the **hub level** (applies to all chargers on that hub)
- Priority value 1 is highest, 10 is lowest
- Only chargers with connector_status = "Charging" (car plugged in and ready) participate in distribution
