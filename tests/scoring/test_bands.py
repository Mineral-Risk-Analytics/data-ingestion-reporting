"""Pin the 2026-07-20 band recalibration (25/45/60) + insufficient-data gate.

Mirrors frontend lib/utils/risk-band.ts — if these cuts change, change both
files in the same commit (sole-source-of-truth pact).
"""
from app.services.scoring.bands import score_to_band


class TestBandCuts:
    def test_boundaries(self):
        assert score_to_band(0) == "LOW"
        assert score_to_band(24.9) == "LOW"
        assert score_to_band(25.0) == "MOD"
        assert score_to_band(44.9) == "MOD"
        assert score_to_band(45.0) == "HIGH"
        assert score_to_band(59.9) == "HIGH"
        assert score_to_band(60.0) == "CRIT"
        assert score_to_band(100) == "CRIT"

    def test_acceptance_materials_2026_07_20(self):
        # Live rollup-1.2 anchors the cuts were calibrated against.
        assert score_to_band(65.1) == "CRIT"   # Gallium
        assert score_to_band(64.8) == "CRIT"   # Cobalt
        assert score_to_band(60.8) == "CRIT"   # REE
        assert score_to_band(59.9) == "HIGH"   # Nickel (documented near-miss)
        assert score_to_band(43.0) == "MOD"    # Synthetic Graphite
        assert score_to_band(22.6) == "LOW"    # Iron Ore

    def test_fraction_axis_rescaled(self):
        assert score_to_band(0.61) == "CRIT"
        assert score_to_band(0.10) == "LOW"

    def test_none_passthrough(self):
        assert score_to_band(None) is None

    def test_insufficient_data_gate(self):
        # Germanium: score 18.2 but concentration unscored -> NO band,
        # never a false-green LOW.
        assert score_to_band(18.2, sufficient=False) is None
        assert score_to_band(90.0, sufficient=False) is None
        assert score_to_band(18.2, sufficient=True) == "LOW"
