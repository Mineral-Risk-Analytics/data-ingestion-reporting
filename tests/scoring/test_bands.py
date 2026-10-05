"""Pin the 2026-08-11 band recalibration (35/60/90) + insufficient-data gate.

Concentration-only launch cuts — Option B of
docs/design/band_recalibration_proposal.md (sign-off: Nicole 2026-08-11).
Mirrors frontend lib/utils/risk-band.ts — if these cuts change, change both
files in the same commit (sole-source-of-truth pact).
"""
from app.services.scoring.bands import score_to_band


class TestBandCuts:
    def test_boundaries(self):
        assert score_to_band(0) == "LOW"
        assert score_to_band(34.9) == "LOW"
        assert score_to_band(35.0) == "MOD"
        assert score_to_band(59.9) == "MOD"
        assert score_to_band(60.0) == "HIGH"
        assert score_to_band(89.9) == "HIGH"
        assert score_to_band(90.0) == "CRIT"
        assert score_to_band(100) == "CRIT"

    def test_acceptance_materials_2026_08_10(self):
        # The 2026-08-10 audit-export anchors the cuts were calibrated
        # against (all launch binding stages at 2025 data).
        assert score_to_band(32.78) == "LOW"   # Iron Ore
        assert score_to_band(48.73) == "MOD"   # Copper
        assert score_to_band(71.67) == "HIGH"  # Aluminum
        assert score_to_band(81.35) == "HIGH"  # Phosphate
        assert score_to_band(86.83) == "HIGH"  # Lithium
        assert score_to_band(87.55) == "HIGH"  # Nickel
        assert score_to_band(90.01) == "CRIT"  # Cobalt (boundary case,
        # deliberate: published score understated by stale sulfate stage —
        # see proposal §3)
        assert score_to_band(96.70) == "CRIT"  # REE
        assert score_to_band(99.49) == "CRIT"  # Manganese
        # Documented churn-risk boundary (non-launch):
        assert score_to_band(60.51) == "HIGH"  # Tantalum, 0.5 above cut

    def test_fraction_axis_rescaled(self):
        assert score_to_band(0.91) == "CRIT"
        assert score_to_band(0.10) == "LOW"

    def test_none_passthrough(self):
        assert score_to_band(None) is None

    def test_insufficient_data_gate(self):
        # Rhenium/Sodium: concentration unscored -> NO band, never a
        # false-green LOW.
        assert score_to_band(18.8, sufficient=False) is None
        assert score_to_band(90.0, sufficient=False) is None
