"""
Data models for EVSE calculations - NO Home Assistant dependencies.
Pure Python dataclasses that can be used in tests.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace

_LOGGER = logging.getLogger(__name__)


@dataclass
class ChargerContext:
    """Individual EVSE/charger state and configuration."""
    # Identity
    charger_id: str  # Config entry ID
    entity_id: str   # Entity ID (e.g., "my_charger")

    # Configuration
    min_current: float
    max_current: float
    phases: int  # 1 or 3 (EVSE hardware capability)
    priority: int = 1  # For distribution (lower = higher priority)
    device_type: str = "evse"  # "evse" (OCPP) or "plug" (smart plug/relay)
    
    # Active car connection (detected from OCPP or configured)
    car_phases: int = None  # 1, 2, or 3 (actual car OBC phases detected)
    active_phases_mask: str = None  # "A", "AB", "ABC", "B", "BC", "C", "AC"
    connector_status: str = "Charging"  # OCPP status: Default to active for backward compatibility
    
    def __post_init__(self):
        """Set default phase mask from L1/L2/L3 → site phase mapping.

        For OCPP chargers, the mapping determines which site phases the charger
        occupies. For smart plugs, active_phases_mask is set explicitly via
        connected_to_phase in config and this default is skipped.
        """
        if self.active_phases_mask is None:
            if self.phases == 3:
                self.active_phases_mask = "".join(sorted({self.l1_phase, self.l2_phase, self.l3_phase}))
            elif self.phases == 2:
                self.active_phases_mask = "".join(sorted({self.l1_phase, self.l2_phase}))
            elif self.phases == 1:
                self.active_phases_mask = self.l1_phase
    
    # L1/L2/L3 → site phase mapping (configurable, default L1=A, L2=B, L3=C)
    l1_phase: str = "A"
    l2_phase: str = "B"
    l3_phase: str = "C"

    # Per-phase current readings (from OCPP L1/L2/L3 attributes)
    l1_current: float = 0  # L1 current (A) — maps to l1_phase
    l2_current: float = 0  # L2 current (A) — maps to l2_phase
    l3_current: float = 0  # L3 current (A) — maps to l3_phase
    
    # Calculated values (populated during calculation)
    allocated_current: float = 0   # What the charger actually gets (sent via OCPP)
    available_current: float = 0   # What the charger could get if a car were plugged in

    # OCPP settings
    ocpp_device_id: str = None
    stack_level: int = 2
    charge_rate_unit: str = "auto"  # "amps", "watts", or "auto"

    def get_site_phase_draw(self) -> tuple[float, float, float]:
        """Map L1/L2/L3 current to site phases A/B/C using phase mapping."""
        draw = {"A": 0.0, "B": 0.0, "C": 0.0}
        draw[self.l1_phase] += self.l1_current
        draw[self.l2_phase] += self.l2_current
        draw[self.l3_phase] += self.l3_current
        return draw["A"], draw["B"], draw["C"]


@dataclass
class PhaseValues:
    """Per-phase values (A, B, C) with convenience properties.

    None means the phase does not physically exist on the site.
    0.0 means the phase exists but has no load.
    """
    a: float | None = None
    b: float | None = None
    c: float | None = None

    @property
    def total(self) -> float:
        return sum(v for v in (self.a, self.b, self.c) if v is not None)

    @property
    def active_count(self) -> int:
        """Number of phases that physically exist (non-None)."""
        return sum(1 for v in (self.a, self.b, self.c) if v is not None)

    @property
    def active_mask(self) -> str:
        """Phase mask string for existing phases, e.g. 'A', 'AB', 'ABC'."""
        return ''.join(
            p for p, v in [('A', self.a), ('B', self.b), ('C', self.c)]
            if v is not None
        )

    def __neg__(self) -> PhaseValues:
        return PhaseValues(
            -self.a if self.a is not None else None,
            -self.b if self.b is not None else None,
            -self.c if self.c is not None else None,
        )

    def clamp_min(self, v: float = 0.0) -> PhaseValues:
        return PhaseValues(
            max(v, self.a) if self.a is not None else None,
            max(v, self.b) if self.b is not None else None,
            max(v, self.c) if self.c is not None else None,
        )

    def __repr__(self) -> str:
        parts = []
        for name, val in [('a', self.a), ('b', self.b), ('c', self.c)]:
            if val is not None:
                parts.append(f"{name}={val:.1f}")
        return f"PV({', '.join(parts)})"


@dataclass
class SiteContext:
    """Site-wide electrical system state and configuration."""
    # Grid/Power configuration
    voltage: float = 230
    main_breaker_rating: float = 63

    # Per-phase readings from site meter (Amps)
    grid_current: PhaseValues = field(default_factory=PhaseValues)     # raw meter (+ import, - export)
    consumption: PhaseValues = field(default_factory=PhaseValues)      # max(0, grid_current) per phase
    export_current: PhaseValues = field(default_factory=PhaseValues)   # max(0, -grid_current) per phase

    # Solar
    solar_production_total: float = 0
    solar_is_derived: bool = True  # True = derived from grid meter, False = dedicated entity
    household_consumption_total: float | None = None  # Computed when solar entity available (W)
    household_consumption: PhaseValues | None = None  # Per-phase household (A), from inverter entities

    # Wiring topology + per-phase inverter output
    wiring_topology: str = "parallel"  # "parallel" or "series"
    inverter_output_per_phase: PhaseValues | None = None  # Raw inverter output readings (A)

    # Battery
    battery_soc: float | None = None
    battery_power: float | None = None  # Positive = discharging, Negative = charging
    battery_soc_target: float | None = None
    battery_soc_min: float | None = None
    battery_soc_hysteresis: float = 5
    battery_max_charge_power: float | None = None
    battery_max_discharge_power: float | None = None
    
    # Grid import limit (from smart meter / grid operator)
    max_grid_import_power: float | None = None  # Max total power allowed from grid (W)

    # Inverter specifications (for sites with battery/solar inverter)
    inverter_max_power: float | None = None  # Total inverter power capacity (W)
    inverter_max_power_per_phase: float | None = None  # Max power per phase (W)
    inverter_supports_asymmetric: bool = False  # Can inverter balance power across phases

    # Settings
    allow_grid_charging: bool = True
    power_buffer: float = 0
    excess_export_threshold: float = 13000
    charging_mode: str = "Standard"  # "Standard", "Eco", "Solar", "Excess"
    distribution_mode: str = "priority"  # "priority", "shared", "strict", "optimized"

    # Chargers at this site
    chargers: list[ChargerContext] = field(default_factory=list)

    @property
    def num_phases(self) -> int:
        """Number of phases at this site, derived from consumption data."""
        count = self.consumption.active_count
        return count if count > 0 else 1

    @property
    def total_export_current(self) -> float:
        return self.export_current.total

    @property
    def total_export_power(self) -> float:
        return self.export_current.total * self.voltage


@dataclass
class PhaseConstraints:
    """Per-phase and combination power constraints (in Amps).

    Keys represent physical phase combinations:
    - A, B, C: single-phase limits
    - AB, AC, BC: two-phase combination limits
    - ABC: three-phase (total) limit
    """
    A: float = 0.0
    B: float = 0.0
    C: float = 0.0
    AB: float = 0.0
    AC: float = 0.0
    BC: float = 0.0
    ABC: float = 0.0

    @classmethod
    def zeros(cls) -> PhaseConstraints:
        return cls()

    @classmethod
    def from_per_phase(cls, a: float, b: float, c: float) -> PhaseConstraints:
        """Build constraints from per-phase values (symmetric inverter pattern).

        Multi-phase combos are the sum of their components.
        """
        return cls(
            A=a, B=b, C=c,
            AB=a + b, AC=a + c, BC=b + c,
            ABC=a + b + c,
        )

    @classmethod
    def from_pool(cls, a: float, b: float, c: float, total: float) -> PhaseConstraints:
        """Build constraints for asymmetric inverter pattern.

        Per-phase limits may be less than total (due to per-phase inverter cap),
        but multi-phase combos can access the full pool.
        """
        return cls(
            A=a, B=b, C=c,
            AB=total, AC=total, BC=total,
            ABC=total,
        )

    def __add__(self, other: PhaseConstraints) -> PhaseConstraints:
        return PhaseConstraints(
            A=self.A + other.A, B=self.B + other.B, C=self.C + other.C,
            AB=self.AB + other.AB, AC=self.AC + other.AC, BC=self.BC + other.BC,
            ABC=self.ABC + other.ABC,
        )

    def __sub__(self, other: PhaseConstraints) -> PhaseConstraints:
        return PhaseConstraints(
            A=self.A - other.A, B=self.B - other.B, C=self.C - other.C,
            AB=self.AB - other.AB, AC=self.AC - other.AC, BC=self.BC - other.BC,
            ABC=self.ABC - other.ABC,
        )

    def scale(self, factor: float) -> PhaseConstraints:
        return PhaseConstraints(
            A=self.A * factor, B=self.B * factor, C=self.C * factor,
            AB=self.AB * factor, AC=self.AC * factor, BC=self.BC * factor,
            ABC=self.ABC * factor,
        )

    def element_min(self, other: PhaseConstraints) -> PhaseConstraints:
        return PhaseConstraints(
            A=min(self.A, other.A), B=min(self.B, other.B), C=min(self.C, other.C),
            AB=min(self.AB, other.AB), AC=min(self.AC, other.AC), BC=min(self.BC, other.BC),
            ABC=min(self.ABC, other.ABC),
        )

    def element_max(self, other: PhaseConstraints) -> PhaseConstraints:
        return PhaseConstraints(
            A=max(self.A, other.A), B=max(self.B, other.B), C=max(self.C, other.C),
            AB=max(self.AB, other.AB), AC=max(self.AC, other.AC), BC=max(self.BC, other.BC),
            ABC=max(self.ABC, other.ABC),
        )

    def get_available(self, mask: str) -> float:
        """Get per-phase current available for a charger with given phase mask.

        Implements Multi-Phase Constraint Principle:
        - 1-phase on A: min(A, any 2-phase combo containing A, ABC)
        - 2-phase on AB: min(A, B, AB/2, ABC/2)
        - 3-phase: min(A, B, C, AB/2, AC/2, BC/2, ABC/3)
        """
        if len(mask) == 1:
            phase = mask
            two_phase_limits = []
            for combo in ('AB', 'AC', 'BC'):
                if phase in combo:
                    two_phase_limits.append(getattr(self, combo))
            return min(getattr(self, phase), *two_phase_limits, self.ABC)

        elif len(mask) == 2:
            return min(
                getattr(self, mask[0]),
                getattr(self, mask[1]),
                getattr(self, mask) / 2,
                self.ABC / 2,
            )

        elif mask == 'ABC':
            return min(
                self.A, self.B, self.C,
                self.AB / 2, self.AC / 2, self.BC / 2,
                self.ABC / 3,
            )

        _LOGGER.warning("Unknown phase mask '%s', returning 0", mask)
        return 0

    def deduct(self, current: float, mask: str) -> PhaseConstraints:
        """Deduct current from all affected phase combinations. Returns new instance."""
        result = self.copy()

        # Deduct from individual phases
        for phase in mask:
            setattr(result, phase, getattr(result, phase) - current)

        # Deduct from affected 2-phase combinations
        for combo in ('AB', 'AC', 'BC'):
            overlap = sum(1 for p in mask if p in combo)
            if overlap > 0:
                setattr(result, combo, getattr(result, combo) - current * overlap)

        # Deduct total from ABC
        result.ABC -= current * len(mask)

        return result.normalize()

    def normalize(self) -> PhaseConstraints:
        """Apply cascading limits to ensure constraint consistency. Returns new instance."""
        r = self.copy()

        for _ in range(2):
            # DOWNWARD: larger combos limited by smaller components
            r.AB = min(r.AB, r.A + r.B, r.ABC)
            r.AC = min(r.AC, r.A + r.C, r.ABC)
            r.BC = min(r.BC, r.B + r.C, r.ABC)

            r.ABC = min(
                r.ABC,
                r.A + r.B + r.C,
                r.AB + r.C,
                r.AC + r.B,
                r.BC + r.A,
            )

            # UPWARD: smaller components limited by larger combos
            r.A = min(r.A, r.AB, r.AC, r.ABC)
            r.B = min(r.B, r.AB, r.BC, r.ABC)
            r.C = min(r.C, r.AC, r.BC, r.ABC)

        # Clamp non-negative
        r.A = max(0, r.A)
        r.B = max(0, r.B)
        r.C = max(0, r.C)
        r.AB = max(0, r.AB)
        r.AC = max(0, r.AC)
        r.BC = max(0, r.BC)
        r.ABC = max(0, r.ABC)

        return r

    def copy(self) -> PhaseConstraints:
        return replace(self)

    def __repr__(self) -> str:
        return (f"PC(A={self.A:.1f}, B={self.B:.1f}, C={self.C:.1f}, "
                f"AB={self.AB:.1f}, AC={self.AC:.1f}, BC={self.BC:.1f}, "
                f"ABC={self.ABC:.1f})")
