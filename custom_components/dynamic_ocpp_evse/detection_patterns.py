"""Auto-detection patterns for grid CTs and inverter output entities.

Each pattern set contains regex patterns to match entity IDs for phase A, B, and C.
To add support for a new inverter/meter brand, add a new dict to the appropriate list.

Pattern sets are tried in order — the first complete match (all 3 phases found) wins.
"""

# Grid current / power sensor patterns (used during hub grid configuration)
PHASE_PATTERNS = [
    {
        "name": "SolarEdge",
        "patterns": {
            "phase_a": r'sensor\..*m.*ac_current_a.*',
            "phase_b": r'sensor\..*m.*ac_current_b.*',
            "phase_c": r'sensor\..*m.*ac_current_c.*',
        },
        "unit": "A",
    },
    {
        "name": "Solarman/Deye - external CTs",
        "patterns": {
            "phase_a": r'sensor\..*_external_ct1_current.*',
            "phase_b": r'sensor\..*_external_ct2_current.*',
            "phase_c": r'sensor\..*_external_ct3_current.*',
        },
        "unit": "A",
    },
    {
        "name": "Solarman/Deye - internal CTs",
        "patterns": {
            "phase_a": r'sensor\..*_internal_ct1_current.*',
            "phase_b": r'sensor\..*_internal_ct2_current.*',
            "phase_c": r'sensor\..*_internal_ct3_current.*',
        },
        "unit": "A",
    },
    {
        "name": "Solarman - grid power (individual phases)",
        "patterns": {
            "phase_a": r'sensor\..*grid_(?:1|l1|power_1|power_l1).*',
            "phase_b": r'sensor\..*grid_(?:2|l2|power_2|power_l2).*',
            "phase_c": r'sensor\..*grid_(?:3|l3|power_3|power_l3).*',
        },
        "unit": "W",
    },
]

# Inverter per-phase output sensor patterns (used during hub inverter configuration)
INVERTER_OUTPUT_PATTERNS = [
    {
        "name": "Solarman/Deye - output current",
        "patterns": {
            "phase_a": r'sensor\..*(?:output|inverter)_(?:current_)?(?:l1|1|phase_?a).*',
            "phase_b": r'sensor\..*(?:output|inverter)_(?:current_)?(?:l2|2|phase_?b).*',
            "phase_c": r'sensor\..*(?:output|inverter)_(?:current_)?(?:l3|3|phase_?c).*',
        },
    },
    {
        "name": "SolarEdge - AC current",
        "patterns": {
            "phase_a": r'sensor\..*i.*ac_current_a.*',
            "phase_b": r'sensor\..*i.*ac_current_b.*',
            "phase_c": r'sensor\..*i.*ac_current_c.*',
        },
    },
    {
        "name": "Fronius/Huawei - phase power",
        "patterns": {
            "phase_a": r'sensor\..*inverter.*(?:power|current).*(?:phase_?a|l1|_1).*',
            "phase_b": r'sensor\..*inverter.*(?:power|current).*(?:phase_?b|l2|_2).*',
            "phase_c": r'sensor\..*inverter.*(?:power|current).*(?:phase_?c|l3|_3).*',
        },
    },
]
