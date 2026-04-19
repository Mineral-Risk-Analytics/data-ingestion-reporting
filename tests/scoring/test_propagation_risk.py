"""Tests for the supply-chain propagation pillar (sixth pillar).

``score_propagation`` is a pure function — no DB, no I/O. We assert:

* empty input collapses to ``0.0`` (orchestrator handles None policy)
* depth weights decay correctly (tier-2 contributes less than tier-1 for the
  same supplier risk + volume share)
* volume share zeros are dropped (no contribution)
* clamping to ``[0, 100]`` and to ``[0, 1]`` for share
* deeper tiers reuse the tail weight (defensive math beyond DEFAULT_DEPTH_WEIGHTS)
"""

from __future__ import annotations

import pytest

from app.services.scoring.propagation_risk import (
    DEFAULT_DEPTH_WEIGHTS,
    _depth_weight,
    score_propagation,
)


# ---------------------------------------------------------------------------
# _depth_weight
# ---------------------------------------------------------------------------

class TestDepthWeight:
    def test_tier1_uses_first_weight(self):
        assert _depth_weight(1, (1.0, 0.4)) == 1.0

    def test_tier2_uses_second_weight(self):
        assert _depth_weight(2, (1.0, 0.4)) == 0.4

    def test_deeper_tiers_reuse_tail(self):
        assert _depth_weight(5, (1.0, 0.4, 0.1)) == 0.1

    def test_empty_weights_returns_zero(self):
        assert _depth_weight(1, ()) == 0.0

    def test_depth_zero_or_negative_uses_first(self):
        # Defensive: out-of-range depth indices clamp to first.
        assert _depth_weight(0, (1.0, 0.4)) == 1.0
        assert _depth_weight(-1, (1.0, 0.4)) == 1.0


# ---------------------------------------------------------------------------
# score_propagation
# ---------------------------------------------------------------------------

class TestScorePropagation:
    def test_empty_input_returns_zero(self):
        assert score_propagation([]) == 0.0

    def test_single_tier1_supplier_passes_score_through(self):
        # One tier-1 supplier at full volume → its score IS the propagation score.
        out = score_propagation([(80.0, 1.0, 1)])
        assert out == pytest.approx(80.0)

    def test_tier2_decays_below_tier1(self):
        """Same supplier risk, same volume — tier-2 contribution must be smaller."""
        tier1 = score_propagation([(80.0, 1.0, 1)])
        # A tier-2 supplier alone still averages to its own score (the
        # weighted-average is dominated by its single contribution); the
        # decay shows up when COMPARED with a mixed list. Verify via mix.
        mixed = score_propagation([(80.0, 1.0, 1), (80.0, 1.0, 2)])
        assert mixed == pytest.approx(80.0)  # both 80, average is 80
        # But mixing two equal-volume suppliers with different scores shows decay
        biased = score_propagation([(20.0, 1.0, 1), (100.0, 1.0, 2)])
        # tier-1 weight 1.0 dominates over tier-2 weight 0.4
        # → 0.286 * (20*1.0) + 0.714/... actually just verify shifted toward tier-1
        assert biased < 60.0  # midpoint would be 60; bias pulls toward tier-1 (20)

    def test_volume_share_weights_contribution(self):
        # Two tier-1 suppliers; the higher-volume one should dominate.
        out = score_propagation([(20.0, 0.1, 1), (100.0, 0.9, 1)])
        # Pure volume-weighted: 0.1*20 + 0.9*100 = 92
        assert out == pytest.approx(92.0)

    def test_zero_volume_share_dropped(self):
        # A 0-share supplier must not influence the result.
        out = score_propagation([(0.0, 0.0, 1), (50.0, 1.0, 1)])
        assert out == pytest.approx(50.0)

    def test_all_zero_volume_collapses_to_zero(self):
        assert score_propagation([(99.0, 0.0, 1), (99.0, 0.0, 2)]) == 0.0

    def test_share_clamped_to_unit_interval(self):
        # Share > 1 is clamped to 1; result equals the single supplier's score.
        out = score_propagation([(40.0, 5.0, 1)])
        assert out == pytest.approx(40.0)

    def test_share_negative_clamped_to_zero(self):
        # Negative share contributes nothing; second supplier defines result.
        out = score_propagation([(99.0, -0.5, 1), (30.0, 1.0, 1)])
        assert out == pytest.approx(30.0)

    def test_score_clamped_to_0_100_range(self):
        # Defensive: even if a supplier carries an out-of-range score,
        # the final value never exceeds [0, 100].
        out = score_propagation([(150.0, 1.0, 1)])
        assert 0.0 <= out <= 100.0

    def test_default_depth_weights_constant(self):
        # The pillar's design contract: tier-1 = 1.00, tier-2 = 0.40.
        assert DEFAULT_DEPTH_WEIGHTS == (1.00, 0.40)

    def test_custom_depth_weights_respected(self):
        # Aggressive tail (0.05) should make a tier-2 supplier almost
        # invisible alongside an equal-volume tier-1.
        out = score_propagation(
            [(20.0, 1.0, 1), (100.0, 1.0, 2)],
            depth_weights=(1.0, 0.05),
        )
        # Effective weights: 1.0 and 0.05 → ~95% mass on the tier-1 (20)
        assert out < 30.0
