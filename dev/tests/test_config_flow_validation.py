#!/usr/bin/env python3
"""Validation tests for Dynamic OCPP EVSE config flow."""

# Pure Python - no HA dependencies

def validate_charger_settings(data: dict[str, any], errors: dict[str, str]) -> None:
    """Validate charger settings (pure Python, no HA dependencies)."""
    min_current = data.get("evse_minimum_charge_current")
    max_current = data.get("evse_maximum_charge_current")

    if min_current is not None and max_current is not None:
        if min_current <= 0 or max_current <= 0:
            errors["base"] = "invalid_current"
        elif min_current > max_current:
            errors["base"] = "min_exceeds_max"


def test_validate_min_max_current_valid():
    """Test that valid min <= max passes."""
    errors = {}
    data = {"evse_minimum_charge_current": 6, "evse_maximum_charge_current": 16}
    validate_charger_settings(data, errors)
    assert errors == {}, f"Expected no errors, got {errors}"
    print("PASS: test_validate_min_max_current_valid")


def test_validate_min_exceeds_max():
    """Test that min > max produces error."""
    errors = {}
    data = {"evse_minimum_charge_current": 20, "evse_maximum_charge_current": 16}
    validate_charger_settings(data, errors)
    assert "base" in errors and errors["base"] == "min_exceeds_max", f"Expected min_exceeds_max error, got {errors}"
    print("PASS: test_validate_min_exceeds_max")


def test_validate_zero_current():
    """Test that zero current values produce error."""
    errors = {}
    data = {"evse_minimum_charge_current": 0, "evse_maximum_charge_current": 16}
    validate_charger_settings(data, errors)
    assert "base" in errors and errors["base"] == "invalid_current", f"Expected invalid_current for min=0, got {errors}"

    errors = {}
    data = {"evse_minimum_charge_current": 6, "evse_maximum_charge_current": 0}
    validate_charger_settings(data, errors)
    assert "base" in errors and errors["base"] == "invalid_current", f"Expected invalid_current for max=0, got {errors}"

    print("PASS: test_validate_zero_current")


def test_validate_equal_min_max():
    """Test that min == max is valid (allows fixed current)."""
    errors = {}
    data = {"evse_minimum_charge_current": 16, "evse_maximum_charge_current": 16}
    validate_charger_settings(data, errors)
    assert errors == {}, f"Expected no errors for equal values, got {errors}"
    print("PASS: test_validate_equal_min_max")


def test_validate_negative_current():
    """Test that negative current values produce error."""
    errors = {}
    data = {"evse_minimum_charge_current": -5, "evse_maximum_charge_current": 16}
    validate_charger_settings(data, errors)
    assert "base" in errors and errors["base"] == "invalid_current", f"Expected invalid_current for negative min, got {errors}"

    errors = {}
    data = {"evse_minimum_charge_current": 6, "evse_maximum_charge_current": -10}
    validate_charger_settings(data, errors)
    assert "base" in errors and errors["base"] == "invalid_current", f"Expected invalid_current for negative max, got {errors}"

    print("PASS: test_validate_negative_current")


if __name__ == "__main__":
    test_validate_min_max_current_valid()
    test_validate_min_exceeds_max()
    test_validate_zero_current()
    test_validate_equal_min_max()
    test_validate_negative_current()
    print("\nAll config flow validation tests passed!")