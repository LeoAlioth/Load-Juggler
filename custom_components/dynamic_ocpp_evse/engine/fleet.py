"""Inverter fleet aggregation — many inverter entries, one set of scalars.

A hub may have several inverters (typical: an older AC-coupled string inverter
plus a hybrid with a battery), each an ``inverter`` config entry optionally
carrying its own battery. The distribution engine and ``SiteContext`` stay
single-inverter/single-battery on purpose — this module reduces the fleet to
those scalars, applying the per-member gating that the scalar form cannot
express:

- **Charge power** sums only members whose OWN battery is below its OWN
  full-SOC. The fleet passes ``battery_soc_full=None`` to SiteContext when
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
    charge_cap: Optional[float] = None  # W
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
    """Σ charge caps of members whose OWN battery is below its OWN full-SOC.

    A member with no SOC reading (or no full-SOC configured) counts as "not
    full" — the classic single-battery engine behaves the same way, gating
    only when both values are known.
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
        caps.append(m.charge_cap)
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
    battery flow, so production = output − its own battery power."""
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
    return sum(readings) if readings else None


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
