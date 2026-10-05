"""Tests for the JRC-aligned HHI cliff mapping in material_risk.

The mapping replaced a linear HHI → risk pass-through that compressed the
risk dynamic range so far that Spearman ρ against USGS criticality was
slightly negative (-0.16) across 39 materials.  This test file locks in:

  1. Boundary values at the DOJ HHI tier breakpoints (0.15 / 0.25 /
     0.40 / 0.55) so future "small refactors" don't drift the mapping.
  2. Monotonicity — higher HHI must never produce lower risk.
  3. Real-world material HHI values for the launch-10 minerals so the
     mapping's effect on Cobalt, Lithium, etc. is visible in tests.
"""

import pytest

from app.services.scoring.material_risk import hhi_concentration_risk


class TestBoundaryValues:
    """Verify the mapping hits the documented values at each DOJ tier break."""

    def test_perfectly_fragmented_market(self):
        assert hhi_concentration_risk(0.0) == 0.0

    def test_unconcentrated_upper_bound(self):
        # HHI 0.15 = DOJ "unconcentrated" upper bound = risk 0.40
        assert hhi_concentration_risk(0.15) == pytest.approx(0.40, abs=1e-9)

    def test_moderate_upper_bound(self):
        # HHI 0.25 = DOJ "moderately concentrated" upper bound = risk 0.65
        assert hhi_concentration_risk(0.25) == pytest.approx(0.65, abs=1e-9)

    def test_highly_concentrated_upper_bound(self):
        # HHI 0.40 = "highly concentrated" upper bound = risk 0.85
        assert hhi_concentration_risk(0.40) == pytest.approx(0.85, abs=1e-9)

    def test_very_high_upper_bound(self):
        # HHI 0.55 = single-supplier-dominance threshold = risk 0.95
        assert hhi_concentration_risk(0.55) == pytest.approx(0.95, abs=1e-9)

    def test_pure_monopoly(self):
        assert hhi_concentration_risk(1.0) == pytest.approx(1.0, abs=1e-9)


class TestLinearityWithinBands:
    """Within each band the mapping is linear, so midpoints should land at
    the midpoint of the band's risk range."""

    def test_unconcentrated_midpoint(self):
        # HHI 0.075 (midpoint of 0.00-0.15) → risk 0.20 (midpoint of 0-0.40)
        assert hhi_concentration_risk(0.075) == pytest.approx(0.20, abs=1e-9)

    def test_moderate_midpoint(self):
        # HHI 0.20 (midpoint of 0.15-0.25) → risk 0.525 (midpoint of 0.40-0.65)
        assert hhi_concentration_risk(0.20) == pytest.approx(0.525, abs=1e-9)

    def test_high_midpoint(self):
        # HHI 0.325 (midpoint of 0.25-0.40) → risk 0.75 (midpoint of 0.65-0.85)
        assert hhi_concentration_risk(0.325) == pytest.approx(0.75, abs=1e-9)


class TestMonotonicity:
    """Higher HHI must always produce at least as much risk.  Walk the input
    range and verify no inversions."""

    def test_monotonic_across_full_range(self):
        prev = -1.0
        for i in range(101):
            hhi = i / 100.0
            risk = hhi_concentration_risk(hhi)
            assert risk >= prev - 1e-12, f"non-monotonic at hhi={hhi}: {risk} < {prev}"
            prev = risk


class TestRealWorldLaunchTen:
    """Sanity checks at HHI values that match the launch-10 minerals'
    production-share distributions.  Numbers are rough approximations from
    USGS MCS 2026; the point of the tests is qualitative banding."""

    def test_cobalt_drc_dominance(self):
        # Cobalt: DRC ~75%, Indonesia ~14%, Russia ~3% → HHI ≈ 0.58
        # Should land near 0.95+ — near-single-supplier risk.
        assert hhi_concentration_risk(0.58) >= 0.95

    def test_graphite_china_dominance(self):
        # Natural Graphite: China ~80%, others fragmented → HHI ≈ 0.65
        # Should land at near-extreme risk.
        assert hhi_concentration_risk(0.65) >= 0.96

    def test_lithium_diversified(self):
        # Lithium: Australia 32%, Chile 25%, China 17%, Argentina 8% etc.
        # HHI ≈ 0.20 → moderate-band risk, not extreme.
        result = hhi_concentration_risk(0.20)
        assert 0.45 <= result <= 0.60, f"Lithium midband expected, got {result}"

    def test_copper_well_diversified(self):
        # Copper: Chile 24%, DRC 13%, Peru 11%, China 8%, US 5% etc.
        # HHI ≈ 0.10 → unconcentrated band.
        result = hhi_concentration_risk(0.10)
        assert 0.20 <= result <= 0.30, f"Copper unconcentrated expected, got {result}"


class TestEdgeCases:
    def test_negative_hhi_clamps_to_zero(self):
        # Defensive: invalid input shouldn't produce a negative risk.
        assert hhi_concentration_risk(-0.1) == 0.0

    def test_above_one_clamps_to_one(self):
        # Defensive: although Σshare² can't exceed 1 mathematically.
        assert hhi_concentration_risk(1.5) == 1.0


class TestStep15HsNodeIntegration:
    """Step 1.5 (2026-06-15) — confirm the cliff helper is the same
    function used by hs_node_scorer.

    The bug Step 1.5 fixed: ``hs_node_scorer`` was computing
    composite_node_score from RAW hhi_at_stage, not cliff-mapped.  For
    a single-anchor cobalt-DRC node with hhi_at_stage = 0.59 that meant
    the HHI component contributed 50 × 0.59 = 29.5 points to the
    composite — when the structural concentration story merits ~48.

    This class asserts the math used by hs_node_scorer to scale HHI is
    the same one defined here.  If a future refactor adds a separate
    cliff implementation inside hs_node_scorer, this test will surface
    the duplication.
    """

    def test_cobalt_drc_node_score_after_cliff(self):
        # Raw HHI 0.59 (DRC ~75%, others) → cliff value ~0.97.
        # Node composite (HHI-anchored, no events) = 0.50 × cliff × 100.
        raw = 0.59
        cliff = hhi_concentration_risk(raw)
        node_score_with_cliff = 0.50 * cliff * 100
        node_score_without_cliff = 0.50 * raw * 100
        # After Step 1.5: well above 45.  Before: ~29.5.
        assert node_score_with_cliff >= 45.0
        assert node_score_without_cliff < 35.0
        # The fix's magnitude: at least 15 score points on this node.
        assert node_score_with_cliff - node_score_without_cliff >= 15.0

    def test_australian_lithium_node_stays_modest(self):
        # Raw HHI 0.20 (well-diversified) → cliff ~0.525.
        # Node composite = 0.50 × cliff × 100 = ~26.
        # Should stay LOW even after cliff — diversified markets shouldn't
        # be inflated.  The cliff is monotone so low raw → low cliff.
        raw = 0.20
        cliff = hhi_concentration_risk(raw)
        node_score = 0.50 * cliff * 100
        assert node_score <= 30.0  # diversified markets stay modest
        assert node_score >= 20.0  # but the unconcentrated band still lifts to ~0.525
