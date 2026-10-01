"""Victron Energy detection patterns.

Custom Modbus TCP integration (sfstar/hass-victron).
Cerbo GX or Venus GX required for Modbus TCP.
Comprehensive per-phase grid support.
"""

GRID_CT = [
    {
        "name": "Victron",
        "patterns": {
            "phase_a": r'sensor\..*_grid_l1_current$',
            "phase_b": r'sensor\..*_grid_l2_current$',
            "phase_c": r'sensor\..*_grid_l3_current$',
        },
        "unit": "A",
    },
]
