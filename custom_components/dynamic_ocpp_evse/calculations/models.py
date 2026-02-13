"""
Data models for EVSE calculations - NO Home Assistant dependencies.
Pure Python dataclasses that can be used in tests.
"""
from dataclasses import dataclass, field


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
    
    # Active car connection (detected from OCPP or configured)
    car_phases: int = None  # 1, 2, or 3 (actual car OBC phases detected)
    active_phases_mask: str = None  # "A", "AB", "ABC", "B", "BC", "C", "AC"
    connector_status: str = "Charging"  # OCPP status: Default to active for backward compatibility
    
    def __post_init__(self):
        """Set default phase mask based on charger phases if not explicitly provided.
        
        Defaults:
        - 3-phase: 'ABC' (all three phases)
        - 2-phase: 'AB' (phases A and B)
        - 1-phase: 'A' (phase A)
        
        These can be overridden by explicitly setting connected_to_phase in config.
        """
        if self.active_phases_mask is None:
            if self.phases == 3:
                self.active_phases_mask = 'ABC'
            elif self.phases == 2:
                self.active_phases_mask = 'AB'
            elif self.phases == 1:
                self.active_phases_mask = 'A'
    
    # Per-phase current readings (from OCPP L1/L2/L3 attributes)
    l1_current: float = 0  # Phase A current (A)
    l2_current: float = 0  # Phase B current (A)
    l3_current: float = 0  # Phase C current (A)
    
    # Current state
    current_import: float = 0  # What charger is currently drawing (A)
    current_offered: float = 0  # What was last offered (A)
    
    # Calculated values (populated during calculation)
    target_current: float = 0  # What mode calculation determines (A)
    allocated_current: float = 0  # What distribution allocates (A)
    max_available: float = 0  # Maximum available from site for this charger (A)
    
    # OCPP settings
    ocpp_device_id: str = None
    stack_level: int = 2
    charge_rate_unit: str = "auto"  # "amps", "watts", or "auto"
    
    # Legacy sensor reference (for backward compatibility)
    sensor: object = None


@dataclass
class SiteContext:
    """Site-wide electrical system state and configuration."""
    # Grid/Power configuration
    voltage: float = 230
    main_breaker_rating: float = 63
    max_import_power: float = 0
    num_phases: int = 3
    invert_phases: bool = False
    
    # Grid currents (site meter readings) - Positive = import, Negative = export
    grid_phase_a_current: float = 0
    grid_phase_b_current: float = 0
    grid_phase_c_current: float = 0
    total_import_current: float = 0
    
    # Per-phase consumption (home load before EV)
    phase_a_consumption: float = 0
    phase_b_consumption: float = 0
    phase_c_consumption: float = 0
    
    # Export (solar surplus) - Positive values
    phase_a_export_current: float = 0
    phase_b_export_current: float = 0
    phase_c_export_current: float = 0
    phase_a_export: float = 0  # Alias for compatibility
    phase_b_export: float = 0  # Alias for compatibility
    phase_c_export: float = 0  # Alias for compatibility
    total_export_current: float = 0
    total_export_power: float = 0
    solar_production_total: float = 0  # For tests
    
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
    
    # Site available power
    site_available_current_phase_a: float = 0
    site_available_current_phase_b: float = 0
    site_available_current_phase_c: float = 0
    site_battery_available_power: float = 0
    site_grid_available_power: float = 0
    total_site_available_power: float = 0
    
    # Site power balance
    net_site_consumption: float = 0  # Watts (+ = importing, - = exporting)
    solar_surplus_current: float = 0  # Amps (+ = excess available)
    solar_surplus_power: float = 0  # Watts (+ = excess available)
    
    # Settings
    allow_grid_charging: bool = True
    power_buffer: float = 0
    excess_export_threshold: float = 13000
    charging_mode: str = "Standard"  # "Standard", "Eco", "Solar", "Excess"
    distribution_mode: str = "priority"  # "priority", "shared", "strict", "optimized"
    
    # Chargers at this site
    chargers: list[ChargerContext] = field(default_factory=list)
    
    # Legacy state dict (for backward compatibility during migration)
    state: dict = field(default_factory=dict)
