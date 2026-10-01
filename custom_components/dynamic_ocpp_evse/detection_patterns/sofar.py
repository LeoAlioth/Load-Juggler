"""Sofar Solar detection patterns (via Solarman integration).

Uses Solarman data logger.  3-phase models have excellent per-phase support.
Also covers OEM inverters using Sofar platforms (Turbo Energy, etc.).
"""

GRID_CT = [
    {
        "name": "Sofar - grid current",
        "patterns": {
            "phase_a": r'sensor\..*_current_grid_l1$',
            "phase_b": r'sensor\..*_current_grid_l2$',
            "phase_c": r'sensor\..*_current_grid_l3$',
        },
        "unit": "A",
    },
]
