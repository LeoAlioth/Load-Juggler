"""Utility functions for Dynamic OCPP EVSE calculations."""


def is_number(value):
    """Check if a value can be converted to a float."""
    try:
        float(value)
        return True
    except (ValueError, TypeError):
        return False
