"""Unit tests for auto_detect.py - grid CT inversion and phase mapping detection.
"""

import time

from custom_components.dynamic_ocpp_evse.calculations.models import (
    LoadContext, SiteContext, PhaseValues,
)
from custom_components.dynamic_ocpp_evse.engine.auto_detect import (
    check_inversion, check_phase_mapping,
    _INV_HOLD_S,
    _PM_NOTIFY_SCORE, _PM_REMAP_SCORE,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_charger(**kwargs):
    """Create a LoadContext with sensible defaults."""
    defaults = dict(
        load_id="c1", entity_id="charger_1",
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
    """check_inversion(): export on a phase where our loads draw while nothing
    on the site generates, held for _INV_HOLD_S, is a reversed reading."""

    @staticmethod
    def _night(grid, draw=(16.0, 16.0, 16.0), seconds=_INV_HOLD_S + 2, step=2.0,
               state=None, **site):
        """Run the check every ``step`` s for ``seconds`` against a constant
        grid reading (A per phase) and a charger drawing ``draw``; returns
        (the first notification or None, state)."""
        state = {} if state is None else state
        defaults = dict(solar_is_metered=True, solar_production_total=0.0)
        defaults.update(site)
        load_kw = {k: site.pop(k) for k in ("draw_blind", "dynamic_control") if k in site}
        ctx = SiteContext(loads=[_make_charger(
            l1_current=draw[0], l2_current=draw[1], l3_current=draw[2], **load_kw,
        )], **{k: v for k, v in defaults.items() if k not in load_kw})
        found, t = None, 1000.0
        while t <= 1000.0 + seconds:
            result = check_inversion(state, list(grid), ctx, "hub1", "Test Hub", now=t)
            found = found or result
            t += step
        return found, state

    def test_a_correctly_wired_site_at_night_never_fires(self):
        """The grid imports the charger and the house - never export."""
        found, _ = self._night((17.5, 16.8, 17.1), seconds=3600)
        assert found is None

    def test_export_under_a_charging_car_at_night_fires_and_names_the_phases(self):
        found, state = self._night((-17.5, -16.8, -17.1))
        assert found is not None and "Inversion" in found["title"]
        assert "phase A, B, C" in found["message"]
        assert found["notification_id"] == "dynamic_ocpp_evse_grid_inversion_hub1"
        assert state["inversion"]["notified"] is True

    def test_a_single_reversed_clamp_is_named(self):
        found, _ = self._night((-17.5, 16.8, 17.1))
        assert found is not None and "phase A " in found["message"]
        assert "B" not in found["message"].split("for over")[0].split("phase ")[1]

    def test_it_must_hold_for_the_whole_span(self):
        found, _ = self._night((-17.5, -16.8, -17.1), seconds=_INV_HOLD_S - 4)
        assert found is None
        # Broken once in between, the count starts over.
        state = {}
        self._night((-17.5,) * 3, seconds=_INV_HOLD_S - 10, state=state)
        self._night((17.5,) * 3, seconds=2, state=state)
        found, _ = self._night((-17.5,) * 3, seconds=_INV_HOLD_S - 10, state=state)
        assert found is None

    def test_export_while_anything_generates_is_not_judged(self):
        """Sun, a discharging battery, an unread battery, or solar worked out
        from the grid meter itself: export may be real."""
        grid = (-17.5, -16.8, -17.1)
        assert self._night(grid, solar_production_total=2500.0)[0] is None
        assert self._night(grid, battery_power=3000.0, battery_soc=60.0)[0] is None
        assert self._night(grid, battery_power=None, battery_soc=60.0)[0] is None
        assert self._night(grid, solar_is_metered=False)[0] is None
        # A battery charging or idle generates nothing.
        assert self._night(grid, battery_power=-500.0, battery_soc=60.0)[0] is not None

    def test_too_little_draw_or_an_assumed_draw_is_not_evidence(self):
        grid = (-17.5, -16.8, -17.1)
        assert self._night((-1.0,) * 3, draw=(2.0, 2.0, 2.0))[0] is None
        assert self._night(grid, draw_blind=True)[0] is None
        assert self._night(grid, dynamic_control=False)[0] is None
        # Export under half the draw is within what the readings' timing explains.
        assert self._night((-7.0,) * 3)[0] is None

    def test_off_grid_is_never_judged(self):
        assert self._night((-17.5,) * 3, is_off_grid=True)[0] is None

    def test_it_fires_once(self):
        found, state = self._night((-17.5,) * 3)
        assert found is not None
        again, _ = self._night((-17.5,) * 3, state=state)
        assert again is None


# ===========================================================================
# Feature 2: Phase Mapping Detection
# ===========================================================================

class TestPhaseMappingDetection:
    """Tests for check_phase_mapping() - guards and detection."""

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

        # Phase 1: single-phase car - L1 draws on phase A (correct)
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

        # Phase 2: two-phase car - L3 inactive, non-correlating on C (correct)
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

        # Car 2: L2 inactive - different inactive line triggers reset
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
