"""Pin the 5.0 concentration-first weight demotion (B1, 2026-08-16).

The published overall IS the material-concentration pillar; geopolitical /
regulatory / operational / financial are shadow-scored at weight 0.0
(computed + persisted, not published — the financial-pillar pattern from
4.0, generalised).  See docs/design/concentration_first_scoring_plan.md §3
and the MARKET_PILLAR_WEIGHTS comment block.

If a re-promotion changes these weights, that is a deliberate versioned
methodology change — update these pins AND bump SCORING_VERSION in the
same commit.
"""
import pytest

from app.services.scoring.global_rollup import compute_overall_from_pillars
from app.services.scoring.market_aggregator import (
    MARKET_PILLAR_WEIGHTS,
    _aggregate_market_score,
)
from app.services.scoring.supplier_risk import SCORING_VERSION


class TestWeightPins:
    def test_concentration_only(self):
        assert MARKET_PILLAR_WEIGHTS["material"] == 1.0
        assert MARKET_PILLAR_WEIGHTS["geopolitical"] == 0.0
        assert MARKET_PILLAR_WEIGHTS["regulatory"] == 0.0
        assert MARKET_PILLAR_WEIGHTS["operational"] == 0.0
        assert MARKET_PILLAR_WEIGHTS["financial"] == 0.0

    def test_scoring_version_bumped(self):
        # 5.0 = the demotion. A weight change without a version bump would
        # make two rescores under one version mean different things.
        assert SCORING_VERSION == "5.0"


class TestL1OverallEqualsConcentration:
    def test_overall_is_concentration_exactly(self):
        # Event pillars at extreme values must not move the overall.
        assert _aggregate_market_score(72.4, 99.0, 99.0, 99.0, 99.0) == pytest.approx(72.4)
        assert _aggregate_market_score(72.4, 0.0, 0.0, 0.0, 0.0) == pytest.approx(72.4)

    def test_zero_concentration_scores_zero(self):
        # Non-producer / no-share geography: 0 stays 0 regardless of events.
        assert _aggregate_market_score(0.0, 88.0, 77.0, 66.0, 55.0) == 0.0


class TestL2OverallGate:
    _W = {
        "material_concentration_score": MARKET_PILLAR_WEIGHTS["material"],
        "geopolitical_trade_score": MARKET_PILLAR_WEIGHTS["geopolitical"],
        "regulatory_compliance_score": MARKET_PILLAR_WEIGHTS["regulatory"],
        "operational_score": MARKET_PILLAR_WEIGHTS["operational"],
        "financial_pressure_score": MARKET_PILLAR_WEIGHTS["financial"],
    }

    def test_overall_equals_concentration(self):
        vals = {
            "material_concentration_score": 90.01,
            "geopolitical_trade_score": 55.0,
            "regulatory_compliance_score": 60.0,
            "operational_score": 10.0,
            "financial_pressure_score": 40.0,
        }
        assert compute_overall_from_pillars(vals, self._W) == pytest.approx(90.01)

    def test_unscored_concentration_yields_none(self):
        # Rhenium/Sodium case: no scored stage -> overall None (the hub's
        # insufficient-data gate excludes the row; the platform renders
        # "Insufficient data").  Pre-5.0 this fell back to event pillars,
        # which would have published a number built on unvalidated input.
        vals = {
            "material_concentration_score": None,
            "geopolitical_trade_score": 55.0,
            "regulatory_compliance_score": 60.0,
            "operational_score": 10.0,
            "financial_pressure_score": 40.0,
        }
        assert compute_overall_from_pillars(vals, self._W) is None
