"""Phase detection utilities for Dynamic OCPP EVSE."""
import logging
from .utils import is_number

_LOGGER = logging.getLogger(__name__)


def determine_phases(sensor, state):
    """
    Determine number of phases from charger data.
    
    Detection priority:
    1. If car is actively charging: detect from OCPP L1/L2/L3 current attributes
    2. If no active charging: use configured phases from state
    3. Fallback: assume 1 phase (safest assumption for new connections)
    
    Returns:
        tuple: (phases, calc_used, active_mask, l1, l2, l3) where:
            - phases: number of active phases
            - calc_used: detection method used
            - active_mask: which phases are active ("A", "AB", "ABC", etc.)
            - l1, l2, l3: individual phase currents
    """
    phases = 0
    calc_used = ""
    active_mask = None
    l1_current = 0
    l2_current = 0
    l3_current = 0
    
    # Threshold for considering a phase as active (Amperes)
    PHASE_DETECTION_THRESHOLD = 2.0
    
    # Import CONF constants here to avoid circular dependency
    try:
        from ..const import CONF_EVSE_CURRENT_IMPORT_ENTITY_ID, CONF_PHASES
    except ImportError:
        # For standalone testing
        CONF_EVSE_CURRENT_IMPORT_ENTITY_ID = "evse_current_import_entity_id"
        CONF_PHASES = "phases"
    
    # Try to detect from EVSE current import entity attributes
    try:
        from ..helpers import get_entry_value
        evse_import_entity = get_entry_value(sensor.config_entry, CONF_EVSE_CURRENT_IMPORT_ENTITY_ID)
    except Exception:
        evse_import_entity = sensor.config_entry.data.get(CONF_EVSE_CURRENT_IMPORT_ENTITY_ID)
    
    if evse_import_entity:
        evse_state = sensor.hass.states.get(evse_import_entity)
        if evse_state and evse_state.state not in ['unknown', 'unavailable', None]:
            evse_attributes = evse_state.attributes
            
            # Check for L1, L2, L3 current attributes
            l1_current = None
            l2_current = None
            l3_current = None
            
            # Look for various attribute naming patterns
            for attr, value in evse_attributes.items():
                attr_lower = attr.lower()
                if is_number(value):
                    val = float(value)
                    # Match L1, l1, phase_1, etc.
                    if attr_lower in ['l1', 'phase_1', 'phase1', 'current_phase_1']:
                        l1_current = val
                    elif attr_lower in ['l2', 'phase_2', 'phase2', 'current_phase_2']:
                        l2_current = val
                    elif attr_lower in ['l3', 'phase_3', 'phase3', 'current_phase_3']:
                        l3_current = val
            
            # Count active phases (those above threshold)
            active_phases = 0
            phase_details = []
            
            if l1_current is not None and l1_current > PHASE_DETECTION_THRESHOLD:
                active_phases += 1
                phase_details.append(f"L1:{l1_current:.1f}A")
            
            if l2_current is not None and l2_current > PHASE_DETECTION_THRESHOLD:
                active_phases += 1
                phase_details.append(f"L2:{l2_current:.1f}A")
            
            if l3_current is not None and l3_current > PHASE_DETECTION_THRESHOLD:
                active_phases += 1
                phase_details.append(f"L3:{l3_current:.1f}A")
            
            # If we detected active charging on any phases, use that
            if active_phases > 0:
                phases = active_phases
                calc_used = f"1-detected_{phases}ph_from_ocpp"
                
                # Build active phase mask
                active_mask = ""
                if l1_current is not None and l1_current > PHASE_DETECTION_THRESHOLD:
                    active_mask += "A"
                if l2_current is not None and l2_current > PHASE_DETECTION_THRESHOLD:
                    active_mask += "B"
                if l3_current is not None and l3_current > PHASE_DETECTION_THRESHOLD:
                    active_mask += "C"
                
                # Store individual currents (default to 0 if None)
                l1 = l1_current if l1_current is not None else 0
                l2 = l2_current if l2_current is not None else 0
                l3 = l3_current if l3_current is not None else 0
                
                _LOGGER.info(f"Detected {phases} active phase(s) from OCPP: {', '.join(phase_details)}, mask={active_mask}")
                return phases, calc_used, active_mask, l1, l2, l3
            
            # If no phases detected but sensor exists, check if car is charging
            # If current_import > threshold but no phase breakdown, log warning
            evse_current = state.get(CONF_EVSE_CURRENT_IMPORT_ENTITY_ID) if isinstance(state, dict) else None
            if evse_current and is_number(evse_current) and float(evse_current) > PHASE_DETECTION_THRESHOLD:
                _LOGGER.warning(
                    f"EVSE is drawing {evse_current}A but no phase breakdown detected. "
                    f"Sensor {evse_import_entity} attributes: {list(evse_attributes.keys())}"
                )
    
    # Fallback 1: Use configured phases from state (from config or previous detection)
    if phases == 0 and isinstance(state, dict) and state.get(CONF_PHASES) is not None and is_number(state[CONF_PHASES]):
        phases = int(state[CONF_PHASES])
        calc_used = f"2-config_{phases}ph"
        # Set default mask based on configured phases
        if phases == 1:
            active_mask = "A"
        elif phases == 3:
            active_mask = "ABC"
        else:
            active_mask = "AB"
        _LOGGER.debug(f"Using configured phases: {phases}, mask={active_mask}")
        return phases, calc_used, active_mask, 0, 0, 0
    
    # Fallback 2: Default to 1 phase (safest assumption for new car connections)
    if phases == 0:
        phases = 1
        calc_used = "3-default_1ph"
        active_mask = "A"  # Default to phase A
        _LOGGER.debug(f"No phase data available, defaulting to 1 phase, mask={active_mask}")
    
    return phases, calc_used, active_mask, 0, 0, 0
