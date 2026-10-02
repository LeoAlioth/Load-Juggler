"""Validation tests for the shipped charger-settings validator.

This exercises the REAL helpers.validate_charger_settings. It used to define a
local copy of the function and assert against that - which passes no matter what
the integration actually does. (The equally tautological twin,
test_validation.py, was deleted rather than kept in sync.)
"""

import pytest

from custom_components.dynamic_ocpp_evse.helpers import validate_charger_settings


@pytest.mark.parametrize(
    ("min_a", "max_a", "err"),
    [
        (6, 16, None),
        (20, 16, "min_exceeds_max"),
        (0, 16, "invalid_current"),
        (6, 0, "invalid_current"),
        # min == max is valid: it allows a fixed current.
        (16, 16, None),
        (-5, 16, "invalid_current"),
        (6, -10, "invalid_current"),
    ],
    ids=["valid", "min_exceeds_max", "zero_min", "zero_max", "equal_min_max",
         "negative_min", "negative_max"],
)
def test_validate_charger_settings(min_a, max_a, err):
    errors = {}
    data = {"evse_minimum_charge_current": min_a, "evse_maximum_charge_current": max_a}
    validate_charger_settings(data, errors)
    assert errors == ({"base": err} if err else {}), errors
