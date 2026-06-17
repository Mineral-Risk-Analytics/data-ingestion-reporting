"""Tests for the Step 3 WGI governance overlay helper.

``apply_wgi_governance_overlay`` is a pure function — caller pre-fetches
the country's WGI row — so we don't need a DB fixture.  These tests
lock in the JRC-aligned multiplier shape and the no-data fallbacks so
the published scoring math is stable across edits.

Calibration anchor: at α = 0.5, the multiplier is ``1 - 0.5 × wgi_norm``.
So the multiplier ranges from 1.0 (worst governance — concentration
unchanged) to 0.5 (best governance — concentration halved).

Anchored against the worked examples in the helper's docstring:
    AU (WGI ~93) → multiplier ~0.535
    CN (WGI ~46) → multiplier ~0.770
    CD (WGI ~13) → multiplier ~0.935
"""

import pytest

from app.services.scoring.geopolitical_risk import apply_wgi_governance_overlay


class TestOverlayFires:
    """Cases where the overlay actually adjusts the score."""

    def test_australia_well_governed_strong_discount(self):
        adjusted, diag = apply_wgi_governance_overlay(
            country_concentration_raw=0.32,  # AU's lithium share
            wgi_composite_pct=93.0,
            wgi_n_dimensions_present=6,
        )
        # Multiplier = 1 - 0.5 * 0.93 = 0.535
        # Adjusted = 0.32 * 0.535 = 0.1712
        assert adjusted == pytest.approx(0.1712, abs=1e-4)
        assert diag["applied"] is True
        assert diag["multiplier"] == pytest.approx(0.535, abs=1e-4)
        assert diag["reason"] == "jrc_aligned_wgi_overlay_applied"

    def test_drc_poor_governance_minimal_discount(self):
        adjusted, diag = apply_wgi_governance_overlay(
            country_concentration_raw=0.75,  # DRC's cobalt share
            wgi_composite_pct=13.0,
            wgi_n_dimensions_present=6,
        )
        # Multiplier = 1 - 0.5 * 0.13 = 0.935
        # Adjusted = 0.75 * 0.935 = 0.70125
        assert adjusted == pytest.approx(0.70125, abs=1e-4)
        assert diag["applied"] is True
        assert diag["multiplier"] == pytest.approx(0.935, abs=1e-4)

    def test_china_mid_governance_modest_discount(self):
        adjusted, diag = apply_wgi_governance_overlay(
            country_concentration_raw=0.80,  # CN's graphite-refining share
            wgi_composite_pct=46.0,
            wgi_n_dimensions_present=6,
        )
        # Multiplier = 1 - 0.5 * 0.46 = 0.77
        # Adjusted = 0.80 * 0.77 = 0.616
        assert adjusted == pytest.approx(0.616, abs=1e-4)
        assert diag["multiplier"] == pytest.approx(0.77, abs=1e-4)

    def test_perfect_governance_halves_score(self):
        adjusted, diag = apply_wgi_governance_overlay(
            country_concentration_raw=1.0,
            wgi_composite_pct=100.0,
            wgi_n_dimensions_present=6,
        )
        assert adjusted == pytest.approx(0.5, abs=1e-9)
        assert diag["multiplier"] == pytest.approx(0.5, abs=1e-9)

    def test_worst_governance_no_discount(self):
        adjusted, diag = apply_wgi_governance_overlay(
            country_concentration_raw=0.6,
            wgi_composite_pct=0.0,
            wgi_n_dimensions_present=6,
        )
        # Multiplier = 1 - 0.5 * 0 = 1.0 → unchanged
        assert adjusted == pytest.approx(0.6, abs=1e-9)
        assert diag["multiplier"] == pytest.approx(1.0, abs=1e-9)
        assert diag["applied"] is True  # overlay ran, just zero effect


class TestNoOpBranches:
    def test_missing_wgi_signal_passes_through(self):
        adjusted, diag = apply_wgi_governance_overlay(
            country_concentration_raw=0.5,
            wgi_composite_pct=None,
            wgi_n_dimensions_present=0,
        )
        assert adjusted == 0.5
        assert diag["applied"] is False
        assert diag["reason"] == "no_wgi_signal_for_country"

    def test_insufficient_dimensions_passes_through(self):
        adjusted, diag = apply_wgi_governance_overlay(
            country_concentration_raw=0.5,
            wgi_composite_pct=42.0,
            wgi_n_dimensions_present=2,  # below the 4-dim minimum
        )
        assert adjusted == 0.5
        assert diag["applied"] is False
        assert diag["reason"] == "insufficient_wgi_dimensions"

    def test_n_dimensions_none_does_not_block_overlay(self):
        # When the column is NULL on the row, we don't block — the count
        # is just unknown.  Better to apply the overlay than skip a
        # legitimate signal because of an inventory gap.
        adjusted, diag = apply_wgi_governance_overlay(
            country_concentration_raw=0.4,
            wgi_composite_pct=80.0,
            wgi_n_dimensions_present=None,
        )
        assert diag["applied"] is True
        assert adjusted == pytest.approx(0.4 * 0.6, abs=1e-6)


class TestClampingAndEdgeCases:
    def test_above_100_percentile_clamps(self):
        # Defensive: rounding artifacts in source data shouldn't blow
        # past the [0, 1] multiplier range.
        adjusted, diag = apply_wgi_governance_overlay(
            country_concentration_raw=0.5,
            wgi_composite_pct=105.0,  # impossible value
            wgi_n_dimensions_present=6,
        )
        assert adjusted == pytest.approx(0.25, abs=1e-9)
        assert diag["multiplier"] == pytest.approx(0.5, abs=1e-9)

    def test_negative_percentile_clamps(self):
        adjusted, diag = apply_wgi_governance_overlay(
            country_concentration_raw=0.5,
            wgi_composite_pct=-5.0,
            wgi_n_dimensions_present=6,
        )
        assert adjusted == 0.5
        assert diag["multiplier"] == 1.0

    def test_zero_raw_concentration_returns_zero(self):
        adjusted, diag = apply_wgi_governance_overlay(
            country_concentration_raw=0.0,
            wgi_composite_pct=50.0,
            wgi_n_dimensions_present=6,
        )
        assert adjusted == 0.0
        # Diagnostic still records that the overlay ran.
        assert diag["applied"] is True


class TestDiagnosticShape:
    REQUIRED_KEYS = frozenset({
        "applied", "reason",
        "wgi_composite_pct", "wgi_n_dimensions",
        "weight_alpha", "multiplier",
        "country_concentration_raw", "country_concentration_adjusted",
    })

    def test_all_branches_carry_full_diagnostic(self):
        # No-data path
        _, d1 = apply_wgi_governance_overlay(
            country_concentration_raw=0.3,
            wgi_composite_pct=None, wgi_n_dimensions_present=0,
        )
        # Sparse-data path
        _, d2 = apply_wgi_governance_overlay(
            country_concentration_raw=0.3,
            wgi_composite_pct=50.0, wgi_n_dimensions_present=2,
        )
        # Applied path
        _, d3 = apply_wgi_governance_overlay(
            country_concentration_raw=0.3,
            wgi_composite_pct=50.0, wgi_n_dimensions_present=6,
        )
        for d in (d1, d2, d3):
            assert set(d.keys()) >= self.REQUIRED_KEYS
