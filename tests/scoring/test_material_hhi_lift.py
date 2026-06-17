"""Tests for the Step 2B material-level HHI lift helper.

These tests exercise ``apply_material_hhi_lift`` in isolation so we don't
need a database fixture for the scoring engine integration test.  The
helper is intentionally pure — caller is responsible for both inputs
(stage_rollup_score and material_floor_score), so we can assert on the
blend math and the diagnostic shape without standing up a session.

Scenarios covered:
  * No material HHI signal → no-op, stage rollup returned as-is.
  * Empty score_method_breakdown → no-op (no contributing nodes).
  * Mostly hhi_anchored nodes (≥ 80%) → no-op (rollup is trustworthy).
  * Mostly event_only nodes → blend kicks in, weighted toward floor.
  * Floor invariant: lifted_score ≥ stage_rollup_score always.
  * Diagnostic shape stable across all branches.
"""

import pytest

from app.services.scoring.material_risk import (
    apply_material_hhi_lift,
    score_material_exposure,
)


class TestNoOpBranches:
    """Cases where the helper should return the stage rollup unchanged."""

    def test_no_material_hhi_signal_returns_input(self):
        score, diag = apply_material_hhi_lift(
            stage_rollup_score=30.0,
            material_floor_score=70.0,
            score_method_breakdown={"event_only_no_hhi": 5},
            has_material_hhi_signal=False,
        )
        assert score == 30.0
        assert diag["applied"] is False
        assert diag["reason"] == "no_material_hhi_signal"

    def test_empty_breakdown_returns_input(self):
        score, diag = apply_material_hhi_lift(
            stage_rollup_score=30.0,
            material_floor_score=70.0,
            score_method_breakdown={},
            has_material_hhi_signal=True,
        )
        assert score == 30.0
        assert diag["applied"] is False
        assert diag["reason"] == "no_contributing_nodes"

    def test_high_hhi_coverage_no_lift(self):
        # 8 of 10 nodes are HHI-anchored = 80% → above threshold, no lift.
        score, diag = apply_material_hhi_lift(
            stage_rollup_score=40.0,
            material_floor_score=70.0,
            score_method_breakdown={
                "hhi_anchored": 8, "event_only_no_hhi": 2,
            },
            has_material_hhi_signal=True,
        )
        assert score == 40.0
        assert diag["applied"] is False
        assert diag["reason"] == "rollup_is_hhi_dominated"
        assert diag["hhi_node_fraction"] == pytest.approx(0.80, abs=1e-4)


class TestLiftFires:
    """Cases where the blend actually runs."""

    def test_all_event_only_uses_pure_material_floor(self):
        # 0 of 10 nodes have HHI = alpha is 0 = blended is 100% floor
        score, diag = apply_material_hhi_lift(
            stage_rollup_score=20.0,
            material_floor_score=70.0,
            score_method_breakdown={"event_only_no_hhi": 10},
            has_material_hhi_signal=True,
        )
        assert score == 70.0
        assert diag["applied"] is True
        assert diag["blend_alpha"] == pytest.approx(0.0, abs=1e-4)

    def test_50_50_blend(self):
        # 5 HHI / 5 event-only = alpha 0.5
        # Blended = 0.5 * 30 + 0.5 * 60 = 45
        # max(30, 45) = 45
        score, diag = apply_material_hhi_lift(
            stage_rollup_score=30.0,
            material_floor_score=60.0,
            score_method_breakdown={
                "hhi_anchored": 5, "event_only_no_hhi": 5,
            },
            has_material_hhi_signal=True,
        )
        assert score == pytest.approx(45.0, abs=1e-3)
        assert diag["applied"] is True
        assert diag["blend_alpha"] == pytest.approx(0.5, abs=1e-4)

    def test_cobalt_realistic_15pct_lift(self):
        # Cobalt-like scenario after Step 2A: 15% nodes have HHI.
        # Stage rollup = 35 (event-only signal), floor = 70 (material HHI 0.58
        # passing through cliff mapping → strong material-level signal)
        score, diag = apply_material_hhi_lift(
            stage_rollup_score=35.0,
            material_floor_score=70.0,
            score_method_breakdown={
                "hhi_anchored": 3, "event_only_no_hhi": 17,
            },
            has_material_hhi_signal=True,
        )
        # alpha = 3/20 = 0.15
        # blended = 0.15 * 35 + 0.85 * 70 = 5.25 + 59.5 = 64.75
        # max(35, 64.75) = 64.75
        assert score == pytest.approx(64.75, abs=1e-3)
        assert diag["applied"] is True
        assert 60.0 <= score <= 70.0  # Cobalt now scores closer to material-level

    def test_hhi_anchored_with_operational_counts_as_hhi(self):
        # Both "hhi_anchored" and "hhi_anchored_with_operational" count
        # toward the HHI-bearing fraction.
        score, diag = apply_material_hhi_lift(
            stage_rollup_score=50.0,
            material_floor_score=30.0,
            score_method_breakdown={
                "hhi_anchored_with_operational": 9, "event_only_no_hhi": 1,
            },
            has_material_hhi_signal=True,
        )
        # 9 HHI / 10 = 90% — above 80% threshold → no lift
        assert score == 50.0
        assert diag["applied"] is False


class TestFloorInvariant:
    """``lifted_score`` must never be less than ``stage_rollup_score``."""

    def test_floor_when_material_estimate_is_lower(self):
        # Recent tariff events pushed the rollup to 80; structural HHI is
        # mild (Lithium, HHI ~0.20 → cliff ~0.525 → floor ~40).  The
        # blend would lower the rollup, but max() floors it.
        score, diag = apply_material_hhi_lift(
            stage_rollup_score=80.0,
            material_floor_score=40.0,
            score_method_breakdown={
                "hhi_anchored": 2, "event_only_no_hhi": 8,
            },
            has_material_hhi_signal=True,
        )
        # blended = 0.2 * 80 + 0.8 * 40 = 16 + 32 = 48
        # max(80, 48) = 80
        assert score == 80.0
        # The diagnostic still records the blend math even though the
        # floor wins.
        assert diag["applied"] is True
        assert diag["lifted_score"] == 80.0


class TestDiagnosticShape:
    """The diagnostic dict shape must be stable across branches so the
    UI consumer can rely on the keys being present."""

    REQUIRED_KEYS = frozenset({
        "applied", "reason",
        "hhi_node_count", "total_eligible_nodes", "hhi_node_fraction",
        "stage_rollup_score", "material_floor_score",
        "blend_alpha", "lifted_score",
    })

    def test_keys_present_in_no_op_branch(self):
        _, diag = apply_material_hhi_lift(
            stage_rollup_score=10.0,
            material_floor_score=10.0,
            score_method_breakdown={"hhi_anchored": 10},
            has_material_hhi_signal=True,
        )
        assert set(diag.keys()) >= self.REQUIRED_KEYS

    def test_keys_present_in_lift_branch(self):
        _, diag = apply_material_hhi_lift(
            stage_rollup_score=10.0,
            material_floor_score=50.0,
            score_method_breakdown={"event_only_no_hhi": 10},
            has_material_hhi_signal=True,
        )
        assert set(diag.keys()) >= self.REQUIRED_KEYS

    def test_keys_present_when_no_material_signal(self):
        _, diag = apply_material_hhi_lift(
            stage_rollup_score=10.0,
            material_floor_score=10.0,
            score_method_breakdown={"event_only_no_hhi": 10},
            has_material_hhi_signal=False,
        )
        assert set(diag.keys()) >= self.REQUIRED_KEYS


class TestIntegrationWithScoreMaterialExposure:
    """End-to-end sanity check: the floor input should be the value
    ``score_material_exposure`` produces, so callers using it that way
    don't drift from the math."""

    def test_floor_matches_score_material_exposure(self):
        # Cobalt-like sub-inputs after Step 1's cliff mapping:
        # crit=0.4, conc=0.55 (combining cliff'd prod_hhi + reserves + producer signal),
        # trade_vol=0.3
        crit, conc, trade_vol = 0.4, 0.55, 0.3
        expected_floor = score_material_exposure(crit, conc, trade_vol)
        score, diag = apply_material_hhi_lift(
            stage_rollup_score=30.0,
            material_floor_score=expected_floor,
            score_method_breakdown={"event_only_no_hhi": 10},
            has_material_hhi_signal=True,
        )
        # Pure floor at alpha=0
        assert score == pytest.approx(expected_floor, abs=1e-3)
        assert diag["material_floor_score"] == pytest.approx(expected_floor, abs=1e-3)
