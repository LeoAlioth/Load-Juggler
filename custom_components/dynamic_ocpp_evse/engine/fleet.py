"""Inverter fleet aggregation — many inverter entries, one set of scalars.

A hub may have several inverters (typical: an older AC-coupled string inverter
plus a hybrid with a battery), each an ``inverter`` config entry optionally
carrying its own battery. The distribution engine and ``SiteContext`` stay
single-inverter/single-battery on purpose — this module reduces the fleet to
those scalars, applying the per-member gating that the scalar form cannot
express:

- **Charge power** sums only members whose OWN battery is below its OWN
  full-SOC, and sums the rate each one is PERMITTED to take rather than its
  nameplate rate — our own charge control may be holding that member's register
  below it (see ``charge_power_total``). The fleet passes
  ``battery_soc_full=None`` to SiteContext when
  more than one battery exists, so the calculations-level full gate (which
  only knows the fleet SOC) can never falsely re-zero a partially-full fleet;
  with a single battery the member's real full-SOC is passed through and the
  behavior is exactly the classic single-battery one.
- **Discharge power** sums only members whose OWN battery is at/above the
  hub-level (hysteresis-adjusted) minimum SOC — a battery already below the
  floor cannot be counted dischargeable just because a big full sibling drags
  the weighted fleet SOC above it.
- **Fleet SOC** is capacity-weighted (Σ soc×kWh / Σ kWh); plain mean when no
  member has a known capacity. Hub-level policy (SOC target/min sliders,
  their hysteresis latches) applies to this one number.
- **Solar** sums per member: its own production sensor when configured,
  otherwise derived from its inverter output — a parallel member's output is
  production; a series member's output carries its battery flow, so its
  production is ``output − its battery power``. Applied to the summed outputs
  with the summed battery power, the series formula is algebraically exact
  for ANY mix, because parallel members contribute no battery term.
- **Inverter capacity**: total = Σ; the single per-phase scalar collapses
  conservatively to the minimum over phases of the per-phase sums of the
  members feeding that phase.

Pure functions — unit-testable. Reading HA entities into FleetMember objects
happens in hub_calculation.py, which owns the sensor helpers.
"""

import logging
from dataclasses import dataclass
from typing import Optional

from ..calculations import PhaseValues
from ..const import WIRING_TOPOLOGY_PARALLEL, WIRING_TOPOLOGY_SERIES

_LOGGER = logging.getLogger(__name__)

_PHASES = ("a", "b", "c")


@dataclass
class FleetMember:
    """One inverter's read state — config scalars plus smoothed live values."""

    entry_id: str
    name: str = ""
    max_power: Optional[float] = None
    max_power_per_phase: Optional[float] = None
    supports_asymmetric: bool = False
    topology: str = WIRING_TOPOLOGY_PARALLEL
    output: Optional[PhaseValues] = None  # smoothed amps per phase
    has_solar_entity: bool = False  # a solar production sensor is configured
    solar_measured: Optional[float] = None  # W, smoothed, when measured
    forecast_device_ids: tuple = ()  # this inverter's PV forecast sources
    has_battery: bool = False  # a battery SOC or power entity is configured
    has_battery_power_entity: bool = False
    battery_soc: Optional[float] = None
    battery_power: Optional[float] = None  # W, + discharging / − charging
    charge_cap: Optional[float] = None  # W — nameplate (configured) charge rate
    # W — the rate this member's battery is actually PERMITTED to take right
    # now, when our own Battery Charge Control is holding its charge register
    # down (INVERTER_RT_ENFORCED_CHARGE_W, read in engine/readers.py). None
    # whenever nothing is being held back, which includes an advice-only member
    # — see charge_power_total().
    enforced_charge_limit: Optional[float] = None
    discharge_cap: Optional[float] = None  # W
    soc_full: Optional[float] = None  # %
    capacity_kwh: Optional[float] = None

    def spans_phase(self, phase: str) -> bool:
        """Which site phases this inverter feeds — the phases its output
        entities cover, or all of them when no output entities are set."""
        if self.output is None:
            return True
        return getattr(self.output, phase) is not None


def weighted_soc(members) -> Optional[float]:
    """Capacity-weighted fleet SOC; plain mean when no capacities are known."""
    socs = [
        (m.battery_soc, m.capacity_kwh or 0)
        for m in members
        if m.battery_soc is not None
    ]
    if not socs:
        return None
    total_capacity = sum(c for _, c in socs)
    if total_capacity > 0:
        return sum(s * c for s, c in socs) / total_capacity
    return sum(s for s, _ in socs) / len(socs)


def battery_power_total(members) -> Optional[float]:
    """Summed battery power; None only when NO member has a power sensor —
    that is the signal the derived-solar discharge gate keys on."""
    values = [m.battery_power for m in members if m.has_battery_power_entity]
    readings = [v for v in values if v is not None]
    if not values:
        return None
    return sum(readings) if readings else None


def charge_power_total(members) -> Optional[float]:
    """Σ PERMITTED charge rates of members whose OWN battery is below its OWN
    full-SOC.

    A member with no SOC reading (or no full-SOC configured) counts as "not
    full" — the classic single-battery engine behaves the same way, gating
    only when both values are known.

    "Permitted" rather than "rated", per member:
    ``min(charge_cap, enforced_charge_limit)`` while our own Battery Charge
    Control is holding that member's charge register below its nameplate rate,
    and the plain ``charge_cap`` otherwise. This is the allowance the Excess
    verdict compares the site's placed power against
    (``calculations.excess_margin`` — the only consumer of
    ``SiteContext.battery_max_charge_power``), and the whole point of the
    distinction is the clipping window: while the forecast holds the battery at,
    say, 6.5 kW of a 10 kW rating, the missing 3.5 kW is not somewhere the site
    can put its production, so counting it would read the site as having room
    left exactly when it has surplus it cannot place, and Excess loads —
    which exist to soak that surplus up — could never engage.

    Only ENFORCEMENT narrows. An advice-only member (its switch off, so nothing
    is written to the inverter) really does still charge at its nameplate rate,
    so narrowing on the mere existence of an advice would under-report the
    allowance and over-trigger Excess. That distinction arrives already made:
    the control publishes a rate only while it is actually holding one, and
    None otherwise.

    One cycle behind by nature: the register write is performed by a site-cycle
    worker AFTER the result that carries the advice is published, so what any
    cycle can know is what the previous cycle's write enforced. At seconds-scale
    cycles against a forecast that moves over hours, that lag is invisible; and
    the value is the register's own read-back, so it reports what the battery is
    permitted rather than what we intend it to be permitted.
    """
    caps = []
    for m in members:
        if m.charge_cap is None:
            continue
        if (
            m.battery_soc is not None
            and m.soc_full is not None
            and m.battery_soc >= m.soc_full
        ):
            continue
        cap = m.charge_cap
        if m.enforced_charge_limit is not None:
            cap = min(cap, max(0.0, m.enforced_charge_limit))
        caps.append(cap)
    return sum(caps) if caps else None


def discharge_power_total(members, soc_min: Optional[float]) -> Optional[float]:
    """Σ discharge caps of members whose OWN battery is at/above the hub-level
    (hysteresis-adjusted) minimum SOC. Members without a SOC reading are
    included — the downstream fleet-level gates handle the unknown case."""
    caps = []
    for m in members:
        if m.discharge_cap is None:
            continue
        if (
            m.battery_soc is not None
            and soc_min is not None
            and m.battery_soc < soc_min
        ):
            continue
        caps.append(m.discharge_cap)
    return sum(caps) if caps else None


def soc_full_scalar(members) -> Optional[float]:
    """The full-SOC to pass into SiteContext: the member's own value when
    exactly one battery exists (classic behavior, including the plug-Excess
    gate), None for multi-battery fleets — their full gating already happened
    per member in charge_power_total()."""
    battery_members = [m for m in members if m.has_battery]
    if len(battery_members) == 1:
        return battery_members[0].soc_full
    return None


def capacity_total(members) -> float:
    return sum(m.capacity_kwh or 0 for m in members)


def sum_outputs(members, topology: Optional[str] = None) -> Optional[PhaseValues]:
    """Per-phase sum of member outputs (amps), optionally filtered by
    topology. A phase is non-None if any summed member covers it."""
    selected = [
        m.output
        for m in members
        if m.output is not None and (topology is None or m.topology == topology)
    ]
    if not selected:
        return None
    values = []
    for phase in _PHASES:
        readings = [
            getattr(o, phase) for o in selected if getattr(o, phase) is not None
        ]
        values.append(sum(readings) if readings else None)
    return PhaseValues(*values)


def fleet_topology(members) -> str:
    """The topology scalar for SiteContext: 'series' if any member is series.

    The series solar formula on the summed outputs with the summed battery
    power is exact for any mix — parallel members contribute no battery term —
    so 'series' is the correct site-wide setting whenever one exists.
    """
    if any(m.topology == WIRING_TOPOLOGY_SERIES for m in members):
        return WIRING_TOPOLOGY_SERIES
    return WIRING_TOPOLOGY_PARALLEL


def mixed_topologies(members) -> bool:
    """True when output-bearing members disagree on topology — the one case
    the per-phase household maths needs the two-formula composite."""
    topologies = {m.topology for m in members if m.output is not None}
    return len(topologies) > 1


def _battery_output_term(topology: str, battery_power: Optional[float]) -> float:
    """How much of a battery's flow shows up in its inverter's AC output.

    - **series** (DC-coupled hybrid): the battery hangs off the DC bus, in front
      of the inverter. Discharge adds to the AC output, and charging takes DC
      power that then never reaches the AC side — so the signed battery power
      applies as-is, and charging genuinely REDUCES the AC output.
    - **parallel** (AC-coupled battery/hybrid): the battery charges FROM the AC
      bus, so charging is a load on the bus, not a subtraction from the PV
      inverter's output — the inverter keeps putting out its full production.
      Only discharge adds to the output, hence max(0, ·).
    """
    bp = battery_power or 0.0
    if topology == WIRING_TOPOLOGY_SERIES:
        return bp
    return max(0.0, bp)


def output_power_measured(members, voltage: float) -> Optional[float]:
    """The fleet's measured AC output in watts — Σ per-phase member outputs ×
    voltage. None when no member has output entities.

    Signed on purpose (see hub_calculation._read_inverter_output): a cascaded
    child inverter on a hybrid's load port makes the parent's reading negative,
    and the signed sum nets that back-feed against the child's own positive
    reading — which is precisely the AC power the pair delivers to the site.
    Needs no topology assumption at all: a series member's reading already
    contains its battery flow and a parallel member's already excludes its
    charging, whatever the mix.
    """
    summed = sum_outputs([m for m in members if m.output is not None])
    if summed is None:
        return None
    return summed.total * voltage


def output_power_estimate(
    members, solar_w: Optional[float], battery_power_w: Optional[float] = None
) -> float:
    """Estimated fleet AC output in watts, for sites with NO output sensors.

    Solar production always reaches the AC bus, so it enters in full whatever
    the wiring; only the battery term is topology-dependent, and it is summed
    per member using that member's OWN topology (see _battery_output_term), so
    a mixed fleet is handled by construction. Members without a battery power
    sensor contribute no battery term.

    When no member has a battery power sensor at all, the fleet-level reading
    (usually None) is applied with the fleet topology instead — the same single
    formula the classic single-inverter site used.
    """
    base = solar_w or 0.0
    if any(m.has_battery_power_entity for m in members):
        return base + sum(
            _battery_output_term(m.topology, m.battery_power) for m in members
        )
    return base + _battery_output_term(fleet_topology(members), battery_power_w)


def output_power_total(
    members,
    voltage: float,
    solar_w: Optional[float] = None,
    battery_power_w: Optional[float] = None,
) -> float:
    """The fleet's current AC output in watts — measurement preferred, estimate
    as fallback. Used for display headroom (``rating − output``).

    1. **Measured**: whenever any member has inverter-output entities, its
       signed measured output is used. Members without output entities add
       their own topology-aware estimate from their own production sensor and
       battery flow — nothing else is attributable to them, since the site's
       export-derived solar cannot be split per member.
    2. **Estimated**: no output entities anywhere → output_power_estimate() on
       the site scalars, topology-aware per member.

    Never None: with no members at all this degenerates to the site scalars,
    which is what the pre-fleet code did. The result may be negative — a real
    state (net power flowing INTO the inverters); the caller decides what a
    negative output means for its own headroom maths.
    """
    measured = output_power_measured(members, voltage)
    if measured is None:
        return output_power_estimate(members, solar_w, battery_power_w)
    unmetered = [m for m in members if m.output is None]
    return measured + sum(
        (m.solar_measured or 0.0 if m.has_solar_entity else 0.0)
        + _battery_output_term(m.topology, m.battery_power)
        for m in unmetered
    )


def inverter_limits(members):
    """(max_power, max_power_per_phase, supports_asymmetric) for the fleet.

    Total is the sum. The single per-phase scalar collapses conservatively:
    for each phase, sum the per-phase caps of the members feeding it (a phase
    fed by any uncapped member is unlimited), then take the minimum over the
    limited phases. Asymmetric only when every capacity-configured member
    supports it — a symmetric member cannot shift its share between phases.
    """
    configured = [
        m for m in members if m.max_power is not None or m.max_power_per_phase is not None
    ]
    max_power = None
    totals = [m.max_power for m in configured if m.max_power is not None]
    if totals:
        max_power = sum(totals)

    per_phase_sums = []
    for phase in _PHASES:
        feeders = [m for m in members if m.spans_phase(phase)]
        if not feeders:
            continue
        if any(m.max_power_per_phase is None for m in feeders):
            continue  # an uncapped member makes this phase unlimited
        per_phase_sums.append(sum(m.max_power_per_phase for m in feeders))
    max_power_per_phase = min(per_phase_sums) if per_phase_sums else None

    supports_asymmetric = bool(configured) and all(
        m.supports_asymmetric for m in configured
    )
    return max_power, max_power_per_phase, supports_asymmetric


def member_solar(member, voltage: float) -> Optional[float]:
    """One member's solar production in watts, or None without output
    entities: a parallel output IS production; a series output carries the
    battery flow, so production = output − its own battery power.

    The max(0, ·) is a physical clamp, and it matters now that outputs are
    signed (see hub_calculation._read_inverter_output): a negative result means
    power is flowing INTO this inverter — a cascaded child inverter back-feeding
    its parent's load port, or the grid charging its battery — and neither is
    production of its own. The child's production is counted on the child's own
    member, so clamping here cannot lose it.
    """
    if member.output is None:
        return None
    out_watts = (member.output.total or 0) * voltage
    if member.topology == WIRING_TOPOLOGY_SERIES:
        out_watts -= member.battery_power or 0
    return max(0.0, out_watts)


def member_solar_production(member, voltage: float) -> Optional[float]:
    """One member's solar production: its own production sensor when it has
    one, else derived from its inverter output (None without either)."""
    if member.has_solar_entity:
        return member.solar_measured
    return member_solar(member, voltage)


def solar_total(members, voltage: float) -> Optional[float]:
    """Fleet solar production in watts — each member measured or derived,
    summed. None when no member knows its production at all, which is the
    caller's cue to fall back to grid export + the fleet's charging draw.

    A mixed fleet (one inverter with a production sensor, one without) sums
    the measured and the output-derived halves; that is why the derivation is
    per member rather than one formula over the fleet scalars.
    """
    readings = [
        s
        for s in (member_solar_production(m, voltage) for m in members)
        if s is not None
    ]
    if not readings:
        return None
    # Aggregate clamp: a site cannot produce negative solar power. Every derived
    # term is already non-negative (member_solar clamps), but a MEASURED
    # production sensor can read slightly negative (night-time offset, inverter
    # self-consumption), and a negative site production would poison every
    # downstream pool that multiplies or subtracts it.
    return max(0.0, sum(readings))


def solar_is_measured(members) -> bool:
    """True only when EVERY member reports production from its own sensor —
    the one case with nothing left to re-derive after the feedback loop."""
    return bool(members) and all(m.has_solar_entity for m in members)


def forecast_device_ids(members) -> list:
    """Every PV forecast device configured across the fleet, de-duplicated.

    Clipping is a site-level question — all arrays compete for the same export
    headroom — so the per-inverter sources are merged into one site forecast.
    """
    seen = []
    for m in members:
        for device_id in m.forecast_device_ids or ():
            if device_id not in seen:
                seen.append(device_id)
    return seen


def charging_power_total(members) -> float:
    """Σ of the fleet's current battery-charging draw (positive watts) — the
    no-output-entities solar fallback adds this to grid export."""
    return sum(
        -m.battery_power
        for m in members
        if m.battery_power is not None and m.battery_power < 0
    )
