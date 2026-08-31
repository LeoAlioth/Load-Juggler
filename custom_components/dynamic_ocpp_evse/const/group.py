"""Circuit-group constants — shared breaker limit for co-located loads.

The ``DEVICE_TYPE_GROUP`` discriminator lives in ``common.py`` alongside the
other ``DEVICE_TYPE_*`` values.
"""

CONF_CIRCUIT_GROUP_CURRENT_LIMIT = "circuit_group_current_limit"
CONF_CIRCUIT_GROUP_MEMBERS = "circuit_group_members"
DEFAULT_CIRCUIT_GROUP_CURRENT_LIMIT = 20
