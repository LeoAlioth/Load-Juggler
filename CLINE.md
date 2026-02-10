# CLINE.md - Dynamic OCPP EVSE Development Guide

## Project Overview

**Dynamic OCPP EVSE** is a Home Assistant custom integration that provides intelligent, dynamic charging control for Electric Vehicle Supply Equipment (EVSE) that supports the Open Charge Point Protocol (OCPP). The integration calculates and automatically adjusts charging current based on available power, solar generation, battery state, and user preferences.

**Repository:** https://gitea.alpacasbarn.com/LeoAlioth/Dynamic_OCPP_EVSE  
**Version:** 2.0.0  
**Author:** @LeoAlioth  
**License:** See LICENSE file

---

## Architecture Overview

### Hub-Charger Architecture (v2.0+)

The integration uses a **hub-charger architecture** introduced in v2.0:

- **Hub**: Represents the home's electrical system (solar, grid, battery, power limits)
  - Stores system-wide configuration (power limits, battery settings, phase voltage)
  - Manages global entities (charging mode, battery SOC targets, power buffer)
  - Coordinates current distribution across multiple chargers
  
- **Charger**: Represents individual OCPP-compatible EV chargers
  - Links to a hub
  - Has charger-specific configuration (min/max current, priority, OCPP device)
  - Creates charger-specific entities (current sensors, buttons)
  - Receives allocated current from hub's distribution algorithm

**Migration Path:**  
Legacy v1.x entries (single config) are automatically migrated to hub entries in v2.0+. Users must then add chargers separately.

---

## Directory Structure

```
Dynamic_OCPP_EVSE/
├── custom_components/
│   └── dynamic_ocpp_evse/          # Main integration code
│       ├── __init__.py              # Integration setup, hub/charger management, distribution
│       ├── manifest.json            # Integration metadata
│       ├── const.py                 # Constants and configuration keys
│       ├── config_flow.py           # Configuration UI flows
│       ├── services.yaml            # Service definitions
│       ├── strings.json             # UI strings (English)
│       │
│       ├── button.py                # Button entities (Reset OCPP)
│       ├── number.py                # Number entities (SOC targets, buffer, current limits)
│       ├── select.py                # Select entities (modes, distribution)
│       ├── sensor.py                # Sensor entities (current calculations, allocation)
│       ├── switch.py                # Switch entities (grid charging)
│       │
│       ├── calculations/            # Current calculation engine
│       │   ├── __init__.py
│       │   ├── context.py           # CalculationContext data class
│       │   ├── max_available.py     # Max available current calculation
│       │   ├── utils.py             # Utility functions (power/current conversion)
│       │   └── modes/               # Charging mode implementations
│       │       ├── __init__.py
│       │       ├── base.py          # BaseChargeMode abstract class
│       │       ├── standard.py      # Standard mode (max speed)
│       │       ├── eco.py           # Eco mode (solar + minimum)
│       │       ├── solar.py         # Solar mode (solar only)
│       │       └── excess.py        # Excess mode (threshold-based)
│       │
│       └── translations/            # Localization files
│           ├── en.json              # English translations
│           └── sl.json              # Slovenian translations
│
├── tests/                           # Test suite and results
│   ├── test_current_calculation.py  # Current calculation tests
│   ├── test_entity_migration.py    # Entity migration tests
│   ├── current_calculation_results.csv
│   ├── entity_migration_results.csv
│   ├── entity_unique_ids.csv
│   ├── README.md                    # Test documentation
│   └── CSV_GRAPHING_GUIDE.md       # Guide for analyzing test results
│
├── README.md                        # User-facing documentation
├── CHARGE_MODES_GUIDE.md           # Comprehensive charging modes guide
├── CLINE.md                         # This file - developer guide
├── hacs.json                        # HACS integration metadata
├── requirements.txt                 # Runtime dependencies
├── requirements_dev.txt             # Development dependencies
├── mypy.py                          # MyPy type checking script
├── .gitignore
├── icon.png
└── LICENSE
```

---

## Key Components

### 1. Core Integration (`__init__.py`)

**Responsibilities:**
- Config entry setup and migration (v1 → v2)
- Hub and charger entry management
- Current distribution algorithms (4 modes)
- Service registration (`reset_ocpp_evse`)
- Entity discovery and automatic configuration

**Key Functions:**
- `async_migrate_entry()` - Handles version migrations
- `async_setup_entry()` - Routes hub vs charger setup
- `distribute_current_to_chargers()` - Main distribution logic
- `_distribute_shared()` - Equal distribution after minimums
- `_distribute_priority()` - Priority-based distribution
- `_distribute_sequential_optimized()` - Sequential with leftover
- `_distribute_sequential_strict()` - Strict priority enforcement

**Distribution Modes:**
1. **Shared**: Minimums first, then equal distribution
2. **Priority**: Minimums first, then priority-based distribution
3. **Sequential - Optimized**: Priority order, leftover flows to lower priority
4. **Sequential - Strict**: Fully satisfy each priority before next

### 2. Configuration Flow (`config_flow.py`)

**Flows:**
- Hub configuration (system-wide settings)
- Charger configuration (per-charger settings)
- Discovery flow (automatic OCPP charger detection)
- Options flow (reconfiguration)

**Auto-Detection:**
- Detects SolarEdge, Deye, Solar Assistant sensors
- Finds OCPP charger entities by suffix patterns
- Suggests appropriate sensors during setup

### 3. Constants (`const.py`)

**Defines:**
- Domain name and version
- All configuration keys (CONF_*)
- Entity suffixes (for OCPP integration)
- Default values for all settings
- Charging modes and distribution modes
- Charge rate units (Amps/Watts/Auto)

### 4. Calculation Engine (`calculations/`)

**Architecture:**
- `CalculationContext` - Data class holding all input data (grid, solar, battery, limits)
- `BaseChargeMode` - Abstract base class for all modes
- Mode implementations - Each mode calculates target current

**Flow:**
1. Coordinator reads all sensor values
2. Creates `CalculationContext` with current state
3. Calls active mode's `calculate()` method
4. Mode returns target current
5. Hub distributes current to chargers
6. Chargers send OCPP commands to set charge rate

**Modes:**
- **Standard** (`standard.py`): Maximum speed charging
  - Uses grid + solar + battery (if SOC > min)
  - Respects power buffer for safety
  
- **Eco** (`eco.py`): Economical solar + minimum rate
  - Without battery: max(solar, min_current)
  - With battery: Graduated based on SOC thresholds
  
- **Solar** (`solar.py`): Pure solar charging
  - Without battery: Solar export only
  - With battery: Requires battery at target SOC
  
- **Excess** (`excess.py`): Threshold-based charging
  - Without battery: Charge when export > threshold
  - With battery: Dynamic threshold based on battery charge needs

### 5. Entities

**Hub Entities:**
- `select.{hub}_charging_mode` - Active charging mode
- `select.{hub}_distribution_mode` - Current distribution algorithm
- `number.{hub}_home_battery_soc_target` - Target battery SOC
- `number.{hub}_home_battery_soc_min` - Minimum battery SOC
- `number.{hub}_power_buffer` - Safety buffer for Standard mode
- `switch.{hub}_allow_grid_charging` - Enable/disable grid import
- `sensor.{hub}_max_available_current` - Total available current

**Charger Entities:**
- `sensor.{charger}_target_charge_current` - Mode-calculated target
- `sensor.{charger}_allocated_charge_current` - Distribution-allocated current
- `number.{charger}_min_charge_current` - Minimum charge rate
- `number.{charger}_max_charge_current` - Maximum charge rate
- `number.{charger}_priority` - Distribution priority
- `button.{charger}_reset_ocpp_evse` - Reset charger profile

---

## Configuration Structure

### Hub Configuration Data
```python
{
    ENTRY_TYPE: "hub",
    CONF_ENTITY_ID: "my_evse_hub",
    
    # Grid & Power
    CONF_MAIN_BREAKER_RATING: 25,  # Amps per phase
    CONF_MAX_IMPORT_POWER_ENTITY_ID: "sensor.max_import_power",
    CONF_PHASE_VOLTAGE: 230,  # Volts
    CONF_NUM_PHASES: 3,
    
    # Phase Current/Power Sensors
    CONF_L1_CURRENT_ENTITY_ID: "sensor.l1_current",
    CONF_L2_CURRENT_ENTITY_ID: "sensor.l2_current",
    CONF_L3_CURRENT_ENTITY_ID: "sensor.l3_current",
    # OR
    CONF_L1_POWER_ENTITY_ID: "sensor.l1_power",
    # ...
    
    # Battery (optional)
    CONF_BATTERY_SOC_ENTITY_ID: "sensor.battery_soc",
    CONF_BATTERY_POWER_ENTITY_ID: "sensor.battery_power",
    CONF_BATTERY_MAX_DISCHARGE_POWER: 5000,  # Watts
    CONF_BATTERY_MAX_CHARGE_POWER: 5000,  # Watts
    CONF_BATTERY_SOC_TARGET: 80,  # Percent
    CONF_BATTERY_SOC_MIN: 20,  # Percent
    
    # Mode Settings
    CONF_CHARGING_MODE: "eco",
    CONF_EXCESS_EXPORT_THRESHOLD: 13000,  # Watts
    CONF_POWER_BUFFER: 0,  # Watts
    CONF_ALLOW_GRID_CHARGING: True,
}
```

### Charger Configuration Data
```python
{
    ENTRY_TYPE: "charger",
    CONF_HUB_ENTRY_ID: "hub_entry_id_here",
    CONF_ENTITY_ID: "my_charger",
    
    # OCPP Integration
    CONF_OCPP_DEVICE_ID: "charger_device_id",
    CONF_EVSE_CURRENT_IMPORT_ENTITY_ID: "sensor.charger_current_import",
    CONF_EVSE_CURRENT_OFFERED_ENTITY_ID: "sensor.charger_current_offered",
    
    # Charger Limits
    CONF_EVSE_MINIMUM_CHARGE_CURRENT: 6,  # Amps
    CONF_EVSE_MAXIMUM_CHARGE_CURRENT: 16,  # Amps
    
    # Distribution
    CONF_CHARGER_PRIORITY: 1,  # 1-10, lower = higher priority
    
    # OCPP Profile
    CONF_STACK_LEVEL: 2,  # Stack level for charge profiles
    CONF_CHARGE_RATE_UNIT: "auto",  # "amps", "watts", or "auto"
    
    # Update Control
    CONF_UPDATE_FREQUENCY: 15,  # Seconds
    CONF_CHARGE_PAUSE_DURATION: 180,  # Seconds
}
```

---

## Development Workflow

### Adding a New Charging Mode

1. **Create mode file** in `calculations/modes/`
   ```python
   from .base import BaseChargeMode
   from ..context import CalculationContext
   
   class MyMode(BaseChargeMode):
       def calculate(self, context: CalculationContext) -> float:
           # Implement calculation logic
           return target_current
   ```

2. **Register mode** in `calculations/modes/__init__.py`
   ```python
   from .mymode import MyMode
   
   MODES = {
       "standard": StandardMode,
       "eco": EcoMode,
       "solar": SolarMode,
       "excess": ExcessMode,
       "mymode": MyMode,  # Add here
   }
   ```

3. **Add constant** in `const.py`
   ```python
   CHARGING_MODE_MYMODE = "mymode"
   CHARGING_MODES = [..., CHARGING_MODE_MYMODE]
   ```

4. **Update strings** in `strings.json` and translations
   ```json
   {
       "options": {
           "charging_mode": {
               "mymode": "My Mode Name"
           }
       }
   }
   ```

5. **Document mode** in `CHARGE_MODES_GUIDE.md`

6. **Add tests** in `tests/test_current_calculation.py`

### Testing Changes

```bash
# Run current calculation tests
python tests/test_current_calculation.py

# Run entity migration tests
python tests/test_entity_migration.py

# Type checking
python mypy.py

# View test results
# - Open tests/current_calculation_results.csv
# - Follow tests/CSV_GRAPHING_GUIDE.md for analysis
```

### Debugging Tips

1. **Enable debug logging** in Home Assistant's `configuration.yaml`:
   ```yaml
   logger:
     default: info
     logs:
       custom_components.dynamic_ocpp_evse: debug
   ```

2. **Check entity states** in Developer Tools → States
   - Look for hub entities: `select.{hub}_charging_mode`
   - Look for charger entities: `sensor.{charger}_target_charge_current`
   - Check allocated current: `sensor.{charger}_allocated_charge_current`

3. **Monitor OCPP calls** - Enable OCPP integration debug logging:
   ```yaml
   logger:
     logs:
       custom_components.ocpp: debug
   ```

4. **Verify distribution** - Check logs for distribution calculations:
   ```
   Current distribution (priority) - Total: 32.0A, Allocations: 1: 16.0A, 2: 16.0A
   ```

---

## Key Concepts

### Battery SOC Management

**Thresholds:**
- `min_soc` - Minimum battery level for operation (default 20%)
- `target_soc` - Target battery level for mode behavior changes (default 80%)
- `hysteresis` - Prevents oscillation around thresholds (default 3%)

**Behavior by Mode:**
- **Standard**: Battery provides power when SOC > min_soc
- **Eco**: Graduated charging based on SOC ranges
- **Solar**: Requires battery at target_soc before EV charging
- **Excess**: Dynamic threshold adjustment based on battery needs

### Power vs Current Sensors

The integration supports both:
- **Current sensors** (A) - Direct phase current measurements
- **Power sensors** (W) - Converted to current using configured voltage

**Auto-detection:**
- Reads sensor's `unit_of_measurement` attribute
- Automatically applies conversion when needed
- Formula: `current = power / voltage`

### OCPP Charge Profiles

**Stack Levels:**
- Integration uses configurable stack level (default 2)
- Reset operation uses stack_level - 1
- Higher stack levels override lower ones
- Profile purpose: `TxDefaultProfile`
- Profile kind: `Relative`

**Rate Units:**
- `amps` (A) - Standard OCPP current limit
- `watts` (W) - Power-based limit (3-phase: I * V * 3)
- `auto` - Detect from offered current sensor unit

### Update Timing

**Coordinator Pattern:**
- Each charger has a `DataUpdateCoordinator`
- Update frequency configurable (default 15s)
- Coordinator fetches all sensor values
- Builds `CalculationContext`
- Calls mode calculation
- Updates target current sensor
- Triggers OCPP set_charge_rate if changed

**Pause Duration:**
- Minimum time before restarting stopped charging (default 180s)
- Prevents rapid on/off cycling
- Applies when mode says "stop charging"

---

## Common Modifications

### Change Default Values

Edit `const.py`:
```python
DEFAULT_MIN_CHARGE_CURRENT = 6  # Amps
DEFAULT_MAX_CHARGE_CURRENT = 16  # Amps
DEFAULT_UPDATE_FREQUENCY = 15  # Seconds
```

### Add New Distribution Mode

1. Add constant in `const.py`
2. Implement distribution function in `__init__.py` (e.g., `_distribute_mymode()`)
3. Add case in `distribute_current_to_chargers()`
4. Update strings and translations

### Support New Inverter Brand

Edit `config_flow.py`:
```python
def _get_phase_current_sensors(hass):
    # Add detection logic for new brand
    if "mybrand" in entity_id:
        suggested_sensors[f"L{phase}"] = entity_id
```

### Change Excess Mode Threshold Logic

Edit `calculations/modes/excess.py`:
```python
def calculate(self, context: CalculationContext) -> float:
    # Modify threshold calculation
    effective_threshold = self._calculate_threshold(context)
    # ... rest of logic
```

---

## Integration Points

### OCPP Integration Dependency

**After Dependency:**
```json
"after_dependencies": ["ocpp"]
```

**Expected Entities:**
- `sensor.{charger}_current_import` - Current grid import
- `sensor.{charger}_current_offered` - Current offered to vehicle

**Service Calls:**
- `ocpp.clear_profile` - Clear all charge profiles
- `ocpp.set_charge_rate` - Set charging profile with custom limits

### Home Assistant Platforms Used

- **Config Flow** - Configuration UI
- **Sensor** - Current calculations and monitoring
- **Number** - Adjustable numeric settings
- **Select** - Mode and distribution selection
- **Switch** - Binary settings (grid charging)
- **Button** - Action triggers (reset)

---

## Migration Notes

### v1.x to v2.0 Migration

**Automatic:**
- Entry type set to `hub`
- Entity IDs preserved
- Legacy config converted to hub structure

**Manual Steps Required:**
1. Restart Home Assistant after migration
2. Go to Settings → Devices & Services → Dynamic OCPP EVSE
3. Add charger(s) via "Add Charger" button or discovery
4. Configure charger settings (OCPP device, limits, priority)
5. Reset OCPP EVSE for each charger (clears old profiles)

**What Changed:**
- Single config → Hub + Charger(s)
- Current calculation → Per-charger calculation + distribution
- New distribution modes for multi-charger setups
- Charger priority setting
- Allocated current sensor (shows distribution result)

---

## Troubleshooting Guide

### Integration Won't Load

1. Check Home Assistant logs for errors
2. Verify OCPP integration is loaded first
3. Check all required sensor entities exist
4. Verify config entry data structure

### Charger Not Responding

1. Check OCPP device ID is correct
2. Verify charger is online in OCPP integration
3. Check charge profile stack level compatibility
4. Test manual `ocpp.set_charge_rate` call
5. Reset OCPP EVSE via button entity

### Unexpected Current Allocation

1. Check distribution mode setting
2. Verify charger priorities
3. Check hub's max available current sensor
4. Review mode calculation in logs (debug level)
5. Check charger min/max limits
6. Verify target vs allocated current sensors

### Battery Not Affecting Charging

1. Check battery SOC sensor value
2. Verify battery power sensor (positive = charging, negative = discharging)
3. Check battery SOC thresholds (min, target)
4. Verify mode supports battery integration
5. Check "Allow Grid Charging" switch state

### Solar Not Being Used

1. Verify phase power/current sensors are correct
2. Check if grid is importing (should be exporting for solar modes)
3. Verify battery SOC meets mode requirements
4. Check charging mode is solar-aware (Eco, Solar, Excess)
5. Review calculation context in debug logs

---

## API Reference

### Key Classes

**CalculationContext** (`calculations/context.py`)
```python
@dataclass
class CalculationContext:
    grid_current_l1: float  # Grid import current per phase (A)
    grid_current_l2: float
    grid_current_l3: float
    evse_current_offered: float  # Current offered by EVSE (A)
    max_import_power: float  # Maximum import power limit (W)
    main_breaker_rating: float  # Main breaker rating per phase (A)
    battery_soc: float | None  # Battery state of charge (%)
    battery_power: float | None  # Battery power (W, + charging, - discharging)
    # ... plus battery thresholds, limits, etc.
```

**BaseChargeMode** (`calculations/modes/base.py`)
```python
class BaseChargeMode(ABC):
    @abstractmethod
    def calculate(self, context: CalculationContext) -> float:
        """Calculate target charge current based on context."""
        pass
```

### Important Functions

**distribute_current_to_chargers()**
```python
def distribute_current_to_chargers(
    hass: HomeAssistant, 
    hub_entry_id: str, 
    total_available_current: float,
    charger_targets: dict = None
) -> dict:
    """Returns {charger_entry_id: allocated_current}"""
```

**get_hub_for_charger()**
```python
def get_hub_for_charger(
    hass: HomeAssistant, 
    charger_entry_id: str
) -> ConfigEntry | None:
    """Get hub config entry for a charger."""
```

**get_chargers_for_hub()**
```python
def get_chargers_for_hub(
    hass: HomeAssistant, 
    hub_entry_id: str
) -> list[ConfigEntry]:
    """Get all charger config entries for a hub."""
```

---

## Future Enhancements (Ideas)

### Potential Features
- [ ] Time-based charging schedules
- [ ] Dynamic pricing integration (charge when electricity is cheap)
- [ ] Weather forecast integration (predict solar generation)
- [ ] Vehicle battery SOC integration (stop when car is full)
- [ ] Statistics and reporting (energy charged, cost savings)
- [ ] Smart preconditioning (start charging before departure)
- [ ] Multi-hub support (multiple electrical systems)
- [ ] Advanced battery management (prevent deep discharge cycles)
- [ ] Load balancing with other high-power devices
- [ ] Mobile app notifications (charging complete, errors)

### Code Improvements
- [ ] Add more unit tests (target 80%+ coverage)
- [ ] Add integration tests with Home Assistant test framework
- [ ] Improve type hints (full mypy compliance)
- [ ] Add performance monitoring (track calculation times)
- [ ] Optimize sensor polling (reduce API calls)
- [ ] Add configuration validation (prevent invalid setups)
- [ ] Improve error handling and recovery
- [ ] Add migration tests for future versions

---

## Resources

### Documentation
- Home Assistant Developer Docs: https://developers.home-assistant.io/
- OCPP 1.6 Specification: https://www.openchargealliance.org/protocols/ocpp-16/
- Python Type Hints: https://docs.python.org/3/library/typing.html

### Related Projects
- OCPP Integration: https://github.com/lbbrhzn/ocpp
- HACS: https://hacs.xyz/

### Support
- GitHub Issues: https://github.com/LeoAlioth/Dynamic_OCPP_EVSE/issues
- Home Assistant Community: https://community.home-assistant.io/

---

## Changelog

### v2.0.0 (Current)
- Hub-charger architecture
- Multi-charger support with distribution modes
- No-battery support for all modes
- Improved entity migration
- Enhanced OCPP profile management

### v1.x (Legacy)
- Initial release
- Single charger support
- Basic charging modes
- Battery integration

---

## Development Setup

### Prerequisites
```bash
# Python 3.11+
python --version

# Home Assistant Core
# Install in development mode or use devcontainer
```

### Installation for Development
```bash
# Clone repository
git clone https://gitea.alpacasbarn.com/LeoAlioth/Dynamic_OCPP_EVSE
cd Dynamic_OCPP_EVSE

# Install dev dependencies
pip install -r requirements_dev.txt

# Symlink to Home Assistant config
# Linux/Mac:
ln -s $(pwd)/custom_components/dynamic_ocpp_evse ~/.homeassistant/custom_components/

# Windows (as Administrator):
mklink /D "C:\Users\{USER}\.homeassistant\custom_components\dynamic_ocpp_evse" "C:\path\to\Dynamic_OCPP_EVSE\custom_components\dynamic_ocpp_evse"
```

### Running Tests
```bash
# Run all tests
python -m pytest tests/

# Run specific test
python tests/test_current_calculation.py

# Type checking
python mypy.py
```

---

## Contact

For questions, bugs, or contributions, please:
1. Check existing GitHub Issues
2. Read CHARGE_MODES_GUIDE.md for mode behavior
3. Enable debug logging and check logs
4. Open a new issue with details and logs

**Author:** @LeoAlioth  
**Repository:** https://gitea.alpacasbarn.com/LeoAlioth/Dynamic_OCPP_EVSE

---

*Last Updated: v2.0.0 - February 2026*
