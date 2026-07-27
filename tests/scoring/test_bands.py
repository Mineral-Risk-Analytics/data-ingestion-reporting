"""Pin the 2026-07-26 band recalibration (30/50/65) + insufficient-data gate.

Mirrors frontend lib/utils/risk-band.ts — if these cuts change, change both
files in the same commit (sole-source-of-truth pact).
"""
from app.services.scoring.bands import score_to_band


class TestBandCuts:
    def test_boundaries(self):
        assert score_to_band(0) == "LOW"
        assert score_to_band(29.9) == "LOW"
        assert score_to_band(30.0) == "MOD"
        assert score_to_band(49.9) == "MOD"
        assert score_to_band(50.0) == "HIGH"
        assert score_to_band(64.9) == "HIGH"
        assert score_to_band(65.0) == "CRIT"
        assert score_to_band(100) == "CRIT"

    def test_acceptance_materials_2026_07_26(self):
        # Live 4.3 rollup anchors the cuts were calibrated against.
        assert score_to_band(69.9) == "CRIT"   # Gallium
        assert score_to_band(66.2) == "CRIT"   # Cobalt
        assert score_to_band(65.6) == "CRIT"   # REE
        assert score_to_band(63.9) == "HIGH"   # Nickel (documented near-miss)
        assert score_to_band(47.7) == "MOD"    # Fluorspar
        assert score_to_band(29.7) == "LOW"    # Tin
        assert score_to_band(31.6) == "MOD"    # Iron Ore

    def test_fraction_axis_rescaled(self):
        assert score_to_band(0.66) == "CRIT"
        assert score_to_band(0.10) == "LOW"

    def test_none_passthrough(self):
        assert score_to_band(None) is None

    def test_insufficient_data_gate(self):
        # Germanium: concentration unscored -> NO band, never a false-green LOW.
        assert score_to_band(18.8, sufficient=False) is None
        assert score_to_band(90.0, sufficient=False) is None
