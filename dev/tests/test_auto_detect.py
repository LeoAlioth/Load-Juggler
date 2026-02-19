"""Unit tests for auto_detect.py — grid CT inversion and phase mapping detection.

Pure Python, no Home Assistant dependencies.
"""

import sys
import types
import importlib.util
from pathlib import Path

# ---------------------------------------------------------------------------
# Module loading (same pattern as run_tests.py to avoid HA imports)
# ---------------------------------------------------------------------------
repo_root = Path(__file__).parents[2]
_comp_dir = repo_root / "custom_components" / "dynamic_ocpp_evse"
_calc_dir = _comp_dir / "calculations"

_PKG_ROOT = "custom_components"
_PKG_COMP = "custom_components.dynamic_ocpp_evse"
_PKG_CALC = "custom_components.dynamic_ocpp_evse.calculations"

for _pkg_name in (_PKG_ROOT, _PKG_COMP, _PKG_CALC):
    if _pkg_name not in sys.modules:
        _pkg = types.ModuleType(_pkg_name)
        _pkg.__path__ = []
        _pkg.__package__ = _pkg_name
        sys.modules[_pkg_name] = _pkg


def _load_module_as(fqn, path):
    spec = importlib.util.spec_from_file_location(fqn, str(path))
    module = importlib.util.module_from_spec(spec)
    module.__package__ = fqn.rsplit(".", 1)[0] if "." in fqn else fqn
    sys.modules[fqn] = module
    spec.loader.exec_module(module)
    return module


_load_module_as(f"{_PKG_COMP}.const", _comp_dir / "const.py")
_load_module_as(f"{_PKG_CALC}.models", _calc_dir / "models.py")
_load_module_as(f"{_PKG_CALC}.utils", _calc_dir / "utils.py")
_load_module_as(f"{_PKG_CALC}.target_calculator", _calc_dir / "target_calculator.py")
_load_module_as(f"{_PKG_COMP}.auto_detect", _comp_dir / "auto_detect.py")

from custom_components.dynamic_ocpp_evse.calculations.models import (
    LoadContext, SiteContext, PhaseValues,
)
from custom_components.dynamic_ocpp_evse.auto_detect import (
    check_inversion, check_phase_mapping,
    _INV_WINDOW_SIZE, _INV_THRESHOLD,
    _PM_NOTIFY_SCORE, _PM_REMAP_SCORE,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_charger(**kwargs):
    """Create a LoadContext with sensible defaults."""
    defaults = dict(
        charger_id="c1", entity_id="charger_1",
        min_current=6, max_current=16, phases=3,
        l1_phase="A", l2_phase="B", l3_phase="C",
        connector_status="Charging", device_type="evse",
        l1_current=0, l2_current=0, l3_current=0,
    )
    defaults.update(kwargs)
    return LoadContext(**defaults)


# ===========================================================================
# Feature 1: Grid CT Inversion Detection
# ===========================================================================

class TestInversionDetection:
    """Tests for check_inversion()."""

    def test_no_trigger_with_zero_draw(self):
        """No charger activity → no notification."""
        state = {}
        charger = _make_charger(l1_current=0, l2_current=0, l3_current=0)
        for _ in range(25):
            result = check_inversion(state, [5.0, 3.0, 4.0], [charger],
                                     "hub1", "Test Hub")
            assert result is None

    def test_normal_correlation_no_trigger(self):
        """Charger ramps up, grid import increases → normal, no notification."""
        state = {}
        base_grid = 5.0
        for i in range(25):
            draw = i * 1.5
            charger = _make_charger(
                l1_current=draw / 3, l2_current=draw / 3, l3_current=draw / 3,
            )
            # Grid increases with charger draw (correct behavior)
            smoothed = [base_grid + draw / 3] * 3
            result = check_inversion(state, smoothed, [charger],
                                     "hub1", "Test Hub")
            assert result is None, f"False positive at cycle {i}"

    def test_inverted_correlation_triggers(self):
        """Charger ramps up, grid decreases (inverted CTs) → notification fires."""
        state = {}
        notified = False
        for i in range(25):
            draw = i * 1.5  # ramp up 1.5A per cycle (>1.0A threshold)
            charger = _make_charger(
                l1_current=draw / 3, l2_current=draw / 3, l3_current=draw / 3,
            )
            # Grid DECREASES as charger ramps up → inverted
            smoothed = [10.0 - draw / 3] * 3
            result = check_inversion(state, smoothed, [charger],
                                     "hub1", "Test Hub")
            if result is not None:
                notified = True
                assert "Inversion" in result["title"]
                assert "notification_id" in result
                break

        assert notified, "Expected inversion notification but none fired"
        assert state["inversion"]["notified"] is True

    def test_notification_fires_only_once(self):
        """After notified=True, no more notifications."""
        state = {"inversion": {
            "prev_grid_total": None, "prev_charger_total": None,
            "window": [1] * _INV_WINDOW_SIZE, "notified": True,
        }}
        charger = _make_charger(l1_current=5, l2_current=5, l3_current=5)
        result = check_inversion(state, [-5.0, -5.0, -5.0], [charger],
                                 "hub1", "Test Hub")
        assert result is None

    def test_small_delta_does_not_pollute_window(self):
        """Charger draw change below threshold doesn't grow window."""
        state = {}
        for i in range(25):
            # Tiny draws — all below _INV_MIN_DELTA_A = 1.0A per cycle
            draw = 0.05 * i
            charger = _make_charger(
                l1_current=draw / 3, l2_current=draw / 3, l3_current=draw / 3,
            )
            smoothed = [5.0 - draw, 5.0 - draw, 5.0 - draw]
            result = check_inversion(state, smoothed, [charger],
                                     "hub1", "Test Hub")
            assert result is None

        # Window should still be empty
        assert state.get("inversion", {}).get("window", []) == []

    def test_mixed_signals_do_not_trigger(self):
        """Alternating normal/inverted signals don't reach threshold."""
        state = {}
        for i in range(30):
            if i % 2 == 0:
                # Even cycles: charger up, grid up (normal)
                draw = (i + 1) * 1.5
                smoothed = [5.0 + draw / 3] * 3
            else:
                # Odd cycles: charger up more, grid down (inverted)
                draw = (i + 1) * 1.5
                smoothed = [5.0 - draw / 3] * 3

            charger = _make_charger(
                l1_current=draw / 3, l2_current=draw / 3, l3_current=draw / 3,
            )
            result = check_inversion(state, smoothed, [charger],
                                     "hub1", "Test Hub")
            assert result is None, f"Should not trigger with mixed signals at cycle {i}"

    def test_no_chargers_no_crash(self):
        """No chargers at all → no crash, no notification."""
        state = {}
        for _ in range(20):
            result = check_inversion(state, [5.0, 3.0, 4.0], [],
                                     "hub1", "Test Hub")
            assert result is None


# ===========================================================================
# Feature 2: Phase Mapping Detection
# ===========================================================================

class TestPhaseMappingDetection:
    """Tests for check_phase_mapping() — guards and detection."""

    def test_not_charging_skipped(self):
        """Charger in Available state is not evaluated."""
        state = {}
        charger = _make_charger(connector_status="Available",
                                l1_current=0, l2_current=0, l3_current=0)
        result = check_phase_mapping(state, [5.0, 3.0, 4.0], [charger], "hub1")
        assert result == []

    def test_two_phase_site_skipped(self):
        """Site with <3 phases configured → no detection."""
        state = {}
        charger = _make_charger(l1_current=5, l2_current=5, l3_current=5)
        result = check_phase_mapping(
            state, [5.0, 3.0, None], [charger], "hub1",
        )
        assert result == []

    def test_zero_draw_charger_skipped(self):
        """Charger with 0A on all phases is skipped."""
        state = {}
        charger = _make_charger(l1_current=0, l2_current=0, l3_current=0)
        result = check_phase_mapping(state, [5.0, 3.0, 4.0], [charger], "hub1")
        assert result == []

    def test_symmetric_3phase_no_notification(self):
        """3-phase charger with 3-phase OBC (symmetric draw) → inconclusive.

        When all phases draw equally, active_lines == 3 → skipped entirely.
        No score accumulation, no notification.
        """
        state = {}
        for i in range(25):
            draw = max(0, (i - 2) * 2.0)
            charger = _make_charger(
                l1_current=draw, l2_current=draw, l3_current=draw,
                l1_phase="A", l2_phase="B", l3_phase="C",
            )
            # All 3 grid phases increase equally (symmetric)
            smoothed = [5.0 + draw, 3.0 + draw, 4.0 + draw]
            result = check_phase_mapping(state, smoothed, [charger], "hub1")
            assert all("Mismatch" not in n.get("title", "") for n in result), \
                f"False mismatch at cycle {i}"

    def test_1phase_obc_on_3phase_evse_wrong_phase_detected(self):
        """3-phase EVSE with 1-phase OBC car on phase B, configured as A → mismatch.

        A single-phase OBC car connected to a 3-phase charger only draws on
        L1. The charger is configured with l1_phase="A" but the draw actually
        shows up on grid phase B → detected.
        """
        state = {}
        notified = False
        for i in range(30):
            draw = max(0, (i - 2) * 2.0)
            charger = _make_charger(
                phases=3, l1_current=draw, l2_current=0, l3_current=0,
                l1_phase="A",  # configured: L1→A (WRONG)
            )
            # Physical: charger's L1 is on grid phase B
            smoothed = [5.0, 3.0 + draw, 4.0]
            result = check_phase_mapping(state, smoothed, [charger], "hub1")
            if result:
                notified = True
                assert "Mismatch" in result[0]["title"]
                assert "B" in result[0]["message"]  # detected phase B
                break

        assert notified, "Expected phase mismatch for 1-phase OBC on 3-phase EVSE"

    def test_1phase_obc_on_3phase_evse_correct_no_notification(self):
        """3-phase EVSE with 1-phase OBC car on correct phase → no notification."""
        state = {}
        for i in range(25):
            draw = max(0, (i - 2) * 2.0)
            charger = _make_charger(
                phases=3, l1_current=draw, l2_current=0, l3_current=0,
                l1_phase="A",  # configured: L1→A (correct)
            )
            # Physical: draw shows up on phase A (correct)
            smoothed = [5.0 + draw, 3.0, 4.0]
            result = check_phase_mapping(state, smoothed, [charger], "hub1")
            assert all("Mismatch" not in n.get("title", "") for n in result), \
                f"False mismatch at cycle {i}"

    def test_notification_fires_only_once(self):
        """After remapped=True, no repeat."""
        state = {"phase_map": {"c1": {
            "prev_draw": 0, "prev_grid_a": 0, "prev_grid_b": 0, "prev_grid_c": 0,
            "score": {"A": 0.0, "B": 0.0, "C": 0.0},
            "score_2ph": {"A": 0.0, "B": 0.0, "C": 0.0},
            "inactive_line": None,
            "notify_sent_1ph": False, "notify_sent_2ph": False,
            "confirmed_1ph": False, "confirmed_2ph": False,
            "remapped": True,
        }}}
        charger = _make_charger(l1_current=10, l2_current=10, l3_current=10)
        result = check_phase_mapping(state, [15.0, 13.0, 14.0], [charger], "hub1")
        assert result == []

    def test_auto_remap_after_sufficient_score(self):
        """Phase mismatch triggers notification first, then auto-remap."""
        state = {}
        notification_cycle = None
        remap_cycle = None
        for i in range(60):
            draw = max(0, (i - 2) * 2.0)
            charger = _make_charger(
                phases=3, l1_current=draw, l2_current=0, l3_current=0,
                l1_phase="A",  # configured: A (WRONG)
            )
            # Physical: draw on phase B
            smoothed = [5.0, 3.0 + draw, 4.0]
            result = check_phase_mapping(state, smoothed, [charger], "hub1")
            if result:
                if notification_cycle is None:
                    notification_cycle = i
                    assert "Mismatch" in result[0]["title"]
                if "auto_remap" in result[0]:
                    remap_cycle = i
                    assert result[0]["auto_remap"]["l1_phase"] == "B"
                    break

        assert notification_cycle is not None, "Expected notification"
        assert remap_cycle is not None, "Expected auto-remap"
        assert remap_cycle > notification_cycle

    def test_noisy_data_no_false_notification(self):
        """Noisy per-phase data (no clear leader) → no notification, scores decay."""
        state = {}
        for i in range(100):
            draw = max(0, (i - 2) * 2.0)
            charger = _make_charger(
                phases=3, l1_current=draw, l2_current=0, l3_current=0,
                l1_phase="A",
            )
            # Rotate which grid phase shows the draw → no clear winner
            phase_idx = i % 3
            smoothed = [5.0, 3.0, 4.0]
            smoothed[phase_idx] += draw
            result = check_phase_mapping(state, smoothed, [charger], "hub1")
            assert all("Mismatch" not in n.get("title", "") for n in result), \
                f"False mismatch at cycle {i}"


class TestSinglePhaseDetection:
    """Tests for single-phase EVSE and plug phase detection."""

    def test_single_phase_correct_phase_no_notification(self):
        """1-phase charger on phase A, actually on A → no notification."""
        state = {}
        for i in range(25):
            draw = max(0, (i - 2) * 2.0)
            charger = _make_charger(
                phases=1, l1_current=draw, l2_current=0, l3_current=0,
                l1_phase="A",
            )
            # Draw shows up on phase A (correct)
            smoothed = [5.0 + draw, 3.0, 4.0]
            result = check_phase_mapping(state, smoothed, [charger], "hub1")
            assert all("Mismatch" not in n.get("title", "") for n in result), \
                f"False mismatch at cycle {i}"

    def test_single_phase_wrong_phase_detected(self):
        """1-phase charger configured on A, but actually on B → mismatch."""
        state = {}
        notified = False
        for i in range(30):
            draw = max(0, (i - 2) * 2.0)
            charger = _make_charger(
                phases=1, l1_current=draw, l2_current=0, l3_current=0,
                l1_phase="A",  # configured: A (WRONG)
            )
            # Physical: draw shows up on phase B
            smoothed = [5.0, 3.0 + draw, 4.0]
            result = check_phase_mapping(state, smoothed, [charger], "hub1")
            if result:
                notified = True
                assert "Mismatch" in result[0]["title"]
                assert "B" in result[0]["message"]  # detected phase B
                break

        assert notified, "Expected single-phase mismatch notification"

    def test_plug_correct_phase_no_notification(self):
        """Smart plug on phase C, actually on C → no notification."""
        state = {}
        for i in range(25):
            draw = max(0, (i - 2) * 2.0)
            charger = _make_charger(
                phases=1, device_type="plug",
                l1_current=draw, l2_current=0, l3_current=0,
                l1_phase="C", active_phases_mask="C",
            )
            # Draw shows up on phase C (correct)
            smoothed = [5.0, 3.0, 4.0 + draw]
            result = check_phase_mapping(state, smoothed, [charger], "hub1")
            assert all("Mismatch" not in n.get("title", "") for n in result), \
                f"False mismatch at cycle {i}"

    def test_plug_wrong_phase_detected(self):
        """Smart plug configured on A, but actually on C → mismatch."""
        state = {}
        notified = False
        for i in range(30):
            draw = max(0, (i - 2) * 2.0)
            charger = _make_charger(
                phases=1, device_type="plug",
                l1_current=draw, l2_current=0, l3_current=0,
                l1_phase="A", active_phases_mask="A",  # configured: A (WRONG)
            )
            # Physical: draw shows up on phase C
            smoothed = [5.0, 3.0, 4.0 + draw]
            result = check_phase_mapping(state, smoothed, [charger], "hub1")
            if result:
                notified = True
                assert "Mismatch" in result[0]["title"]
                assert "C" in result[0]["message"]
                break

        assert notified, "Expected plug phase mismatch notification"

    def test_single_phase_notification_fires_only_once(self):
        """After remapped=True, no repeat for single-phase."""
        state = {"phase_map": {"c1": {
            "prev_draw": 0, "prev_grid_a": 0, "prev_grid_b": 0, "prev_grid_c": 0,
            "score": {"A": 0.0, "B": 0.0, "C": 0.0},
            "score_2ph": {"A": 0.0, "B": 0.0, "C": 0.0},
            "inactive_line": None,
            "notify_sent_1ph": False, "notify_sent_2ph": False,
            "confirmed_1ph": False, "confirmed_2ph": False,
            "remapped": True,
        }}}
        charger = _make_charger(phases=1, l1_current=10)
        result = check_phase_mapping(state, [15.0, 3.0, 4.0], [charger], "hub1")
        assert result == []


# ===========================================================================
# Feature 2b: Two-Phase Car Detection (inactive line mapping)
# ===========================================================================

class TestTwoPhaseDetection:
    """Tests for 2-phase car → inactive line phase detection."""

    def test_2phase_inactive_line_wrong_phase_detected(self):
        """2-phase car on 3-phase EVSE: inactive L3 detected on A, mapped to C → mismatch."""
        state = {}
        notified = False
        for i in range(30):
            draw = max(0, (i - 2) * 2.0)
            charger = _make_charger(
                phases=3,
                l1_current=draw, l2_current=draw, l3_current=0,
                l1_phase="A", l2_phase="B", l3_phase="C",  # configured
            )
            # Physical: L1 on B, L2 on C, L3 on A (inactive)
            # Phase A stays flat, B and C increase with draw
            smoothed = [5.0, 3.0 + draw, 4.0 + draw]
            result = check_phase_mapping(state, smoothed, [charger], "hub1")
            if result:
                notified = True
                assert "Mismatch" in result[0]["title"]
                assert "L3" in result[0]["message"]
                assert "A" in result[0]["message"]  # detected phase
                break

        assert notified, "Expected 2-phase inactive line mismatch notification"

    def test_2phase_correct_mapping_no_notification(self):
        """2-phase car, correct mapping (L3 on C) → confirmed, no notification."""
        state = {}
        for i in range(25):
            draw = max(0, (i - 2) * 2.0)
            charger = _make_charger(
                phases=3,
                l1_current=draw, l2_current=draw, l3_current=0,
                l1_phase="A", l2_phase="B", l3_phase="C",
            )
            # Physical: L1 on A, L2 on B, L3 on C (correct)
            # Phase C doesn't change, A and B increase
            smoothed = [5.0 + draw, 3.0 + draw, 4.0]
            result = check_phase_mapping(state, smoothed, [charger], "hub1")
            assert all("Mismatch" not in n.get("title", "") for n in result), \
                f"False mismatch at cycle {i}"

        # L3 confirmed on C
        cs = state["phase_map"]["c1"]
        assert cs.get("confirmed_2ph") is True

    def test_combined_1ph_then_2ph_full_verification(self):
        """1-phase car confirms L1, then 2-phase car confirms L3 → full verification."""
        state = {}

        # Phase 1: single-phase car — L1 draws on phase A (correct)
        for i in range(20):
            draw = max(0, (i - 2) * 2.0)
            charger = _make_charger(
                phases=3,
                l1_current=draw, l2_current=0, l3_current=0,
                l1_phase="A", l2_phase="B", l3_phase="C",
            )
            smoothed = [5.0 + draw, 3.0, 4.0]
            result = check_phase_mapping(state, smoothed, [charger], "hub1")
            assert result == [], f"Unexpected notification at 1ph cycle {i}"

        cs = state["phase_map"]["c1"]
        assert cs["confirmed_1ph"] is True
        assert cs["confirmed_2ph"] is False

        # Phase 2: two-phase car — L3 inactive, non-correlating on C (correct)
        for i in range(20):
            draw = max(0, (i - 2) * 2.0)
            charger = _make_charger(
                phases=3,
                l1_current=draw, l2_current=draw, l3_current=0,
                l1_phase="A", l2_phase="B", l3_phase="C",
            )
            smoothed = [5.0 + draw, 3.0 + draw, 4.0]
            result = check_phase_mapping(state, smoothed, [charger], "hub1")
            assert result == [], f"Unexpected notification at 2ph cycle {i}"

        assert cs["confirmed_1ph"] is True
        assert cs["confirmed_2ph"] is True

    def test_inactive_line_change_resets_2ph_tracking(self):
        """When a different 2-phase car plugs in (different inactive line),
        the 2-phase score data resets."""
        state = {}

        # Car 1: L3 inactive, builds up a few samples
        for i in range(8):
            draw = max(0, (i - 2) * 0.8)
            charger = _make_charger(
                phases=3,
                l1_current=draw, l2_current=draw, l3_current=0,
                l1_phase="A", l2_phase="B", l3_phase="C",
            )
            smoothed = [5.0 + draw, 3.0 + draw, 4.0]
            check_phase_mapping(state, smoothed, [charger], "hub1")

        cs = state["phase_map"]["c1"]
        assert cs["inactive_line"] == "l3"
        assert sum(cs["score_2ph"].values()) > 0
        # Car 1 (correct mapping, L3→C) accumulated on phase C
        assert cs["score_2ph"]["C"] > 0

        # Car 2: L2 inactive — different inactive line triggers reset
        for i in range(3):
            draw = 5.0 + i * 2.0
            charger = _make_charger(
                phases=3,
                l1_current=draw, l2_current=0, l3_current=draw,
                l1_phase="A", l2_phase="B", l3_phase="C",
            )
            smoothed = [5.0 + draw, 3.0, 4.0 + draw]
            check_phase_mapping(state, smoothed, [charger], "hub1")

        assert cs["inactive_line"] == "l2"
        # Reset wiped car 1's phase C accumulation
        assert cs["score_2ph"]["C"] == 0.0

    def test_2phase_on_single_phase_evse_skipped(self):
        """2-phase detection only runs on 3-phase EVSEs (phases >= 3)."""
        state = {}
        for i in range(20):
            draw = max(0, (i - 2) * 2.0)
            charger = _make_charger(
                phases=1,  # single-phase EVSE
                l1_current=draw, l2_current=draw, l3_current=0,
                l1_phase="A",
            )
            smoothed = [5.0 + draw, 3.0 + draw, 4.0]
            check_phase_mapping(state, smoothed, [charger], "hub1")

        cs = state["phase_map"]["c1"]
        # No 2-phase data accumulated (charger.phases < 3)
        assert sum(cs.get("score_2ph", {"A": 0, "B": 0, "C": 0}).values()) == 0


# ---------------------------------------------------------------------------
# Run with pytest
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
