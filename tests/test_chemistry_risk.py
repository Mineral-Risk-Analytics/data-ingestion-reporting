"""Tests for the battery chemistry risk scorer (app/services/scoring/chemistry_risk.py).

Tests for v1.0 functions (score_chemistry, rescore_one_chemistry,
rescore_all_chemistries, _resolve_criticality_signal, _geo_concentration,
PATENT_TREND_MODIFIERS) were removed in the PR 10 cleanup when those functions
were deleted. See docs/deprecation-audit.md §B1, §F2.

Remaining tests cover:
  - DATA_AVAILABILITY_CONFIDENCE constant
  - _sync_patent_trend
  - score_chemistry_from_rollup (v2.0)
  - score_all_chemistries_from_rollup (v2.0)

All tests are self-contained — no real database or API calls.
"""

from __future__ import annotations

import datetime
from unittest.mock import MagicMock, patch

import pytest

from app.services.scoring.chemistry_risk import (
    DATA_AVAILABILITY_CONFIDENCE,
    METHODOLOGY_VERSION_ROLLUP,
    _sync_patent_trend,
    score_chemistry_from_rollup,
)


# ---------------------------------------------------------------------------
# DATA_AVAILABILITY_CONFIDENCE
# ---------------------------------------------------------------------------

class TestDataAvailabilityConfidence:
    def test_commercial_full_confidence(self):
        assert DATA_AVAILABILITY_CONFIDENCE["commercial"] == 1.0

    def test_no_benchmark_lowest(self):
        assert DATA_AVAILABILITY_CONFIDENCE["no_benchmark"] < DATA_AVAILABILITY_CONFIDENCE["limited"]

    def test_all_values_between_zero_and_one(self):
        for v in DATA_AVAILABILITY_CONFIDENCE.values():
            assert 0.0 < v <= 1.0


# ---------------------------------------------------------------------------
# _sync_patent_trend
# ---------------------------------------------------------------------------

class TestSyncPatentTrend:
    def test_updates_material_trend(self):
        signal = MagicMock()
        signal.trend_direction = "rising"

        material = MagicMock()
        material.patent_occurrence_trend = "stable"

        session = MagicMock()
        session.scalar.return_value = signal
        session.get.return_value = material

        _sync_patent_trend(session, material_id=1)

        assert material.patent_occurrence_trend == "rising"

    def test_does_nothing_when_no_signal(self):
        session = MagicMock()
        session.scalar.return_value = None
        material = MagicMock()
        session.get.return_value = material

        _sync_patent_trend(session, material_id=1)

        material.patent_occurrence_trend  # assert not set
        session.get.assert_not_called()   # should not bother fetching material


# ---------------------------------------------------------------------------
# score_chemistry_from_rollup (v2.0)
# ---------------------------------------------------------------------------

def _mock_chemistry(id: int = 1, slug: str = "lfp", is_active: bool = True) -> MagicMock:
    c = MagicMock()
    c.id = id
    c.slug = slug
    c.is_active = is_active
    return c


def _mock_junction(
    material_id: int = 1,
    chemistry_id: int = 1,
    intensity: float = 0.9,
    valid_from: datetime.date = datetime.date(2024, 1, 1),
    valid_to: datetime.date | None = None,
) -> MagicMock:
    j = MagicMock()
    j.material_id = material_id
    j.battery_chemistry_id = chemistry_id
    j.intensity = intensity
    j.valid_from = valid_from
    j.valid_to = valid_to
    return j


def _mock_material(
    id: int = 1,
    canonical_name: str = "Lithium",
    data_availability: str = "commercial",
) -> MagicMock:
    m = MagicMock()
    m.id = id
    m.canonical_name = canonical_name
    m.data_availability = data_availability
    return m


def _mock_global_score(
    material_id: int = 1,
    as_of_date: datetime.date = datetime.date(2024, 1, 1),
    overall: float = 55.0,
) -> MagicMock:
    gs = MagicMock()
    gs.material_id = material_id
    gs.as_of_date = as_of_date
    gs.overall_risk_score = overall
    gs.trade_weighted_geo_count = 3
    gs.rationale_json = {"weight_source": "trade_flow"}
    # Populate all five pillar columns
    gs.material_concentration_score = 50.0
    gs.geopolitical_trade_score = 55.0
    gs.regulatory_compliance_score = 40.0
    gs.operational_score = 45.0
    gs.financial_pressure_score = 35.0
    return gs


class TestScoreChemistryFromRollup:
    def test_raises_if_chemistry_not_found(self):
        session = MagicMock()
        session.get.return_value = None

        with pytest.raises(ValueError, match="not found"):
            score_chemistry_from_rollup(
                session, chemistry_id=99, as_of_date=datetime.date(2024, 1, 1)
            )

    def test_raises_if_no_active_materials(self):
        session = MagicMock()
        chemistry = _mock_chemistry()
        session.get.return_value = chemistry

        scalars_result = MagicMock()
        scalars_result.all.return_value = []
        session.scalars.return_value = scalars_result

        with pytest.raises(ValueError, match="No active battery_chemistry_materials"):
            score_chemistry_from_rollup(
                session, chemistry_id=1, as_of_date=datetime.date(2024, 1, 1)
            )

    def test_raises_if_all_materials_missing_global_scores(self):
        """All constituent materials lack a MaterialGlobalRiskScore → ValueError."""
        session = MagicMock()
        chemistry = _mock_chemistry()
        session.get.return_value = chemistry

        junc = _mock_junction(material_id=1, intensity=0.9)
        mat = _mock_material(id=1)

        junc_result = MagicMock()
        junc_result.all.return_value = [junc]
        mat_result = MagicMock()
        mat_result.all.return_value = [mat]
        gs_result = MagicMock()
        gs_result.all.return_value = []  # no global scores

        call_n = {"n": 0}

        def scalars_se(stmt):
            call_n["n"] += 1
            if call_n["n"] == 1:
                return junc_result
            if call_n["n"] == 2:
                return mat_result
            return gs_result

        session.scalars.side_effect = scalars_se

        with pytest.raises(ValueError, match="No materials with global scores"):
            score_chemistry_from_rollup(
                session, chemistry_id=1, as_of_date=datetime.date(2024, 1, 1)
            )

    def test_methodology_version_is_rollup(self):
        assert METHODOLOGY_VERSION_ROLLUP == "2.0"
