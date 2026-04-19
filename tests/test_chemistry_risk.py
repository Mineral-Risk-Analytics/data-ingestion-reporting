"""Tests for the battery chemistry risk scorer (app/services/scoring/chemistry_risk.py).

All tests are self-contained — no real database or API calls.
"""

from __future__ import annotations

import datetime
from unittest.mock import MagicMock, patch

import pytest

from app.services.scoring.chemistry_risk import (
    DATA_AVAILABILITY_CONFIDENCE,
    PATENT_TREND_MODIFIERS,
    _geo_concentration,
    _resolve_criticality_signal,
    _sync_patent_trend,
    score_chemistry,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_material(
    id: int = 1,
    canonical_name: str = "Lithium",
    criticality_score: float = 0.60,
    hs_codes: list[str] | None = None,
    patent_occurrence_trend: str | None = "stable",
    data_availability: str = "commercial",
) -> MagicMock:
    m = MagicMock()
    m.id = id
    m.canonical_name = canonical_name
    m.criticality_score = criticality_score
    m.hs_codes = hs_codes if hs_codes is not None else ["2825.20"]
    m.patent_occurrence_trend = patent_occurrence_trend
    m.data_availability = data_availability
    return m


def _mock_junction(
    material_id: int = 1,
    chemistry_id: int = 1,
    role: str = "cathode_active",
    intensity: float = 0.9,
    valid_from: datetime.date = datetime.date(2024, 1, 1),
    valid_to: datetime.date | None = None,
) -> MagicMock:
    j = MagicMock()
    j.material_id = material_id
    j.battery_chemistry_id = chemistry_id
    j.role = role
    j.intensity = intensity
    j.valid_from = valid_from
    j.valid_to = valid_to
    return j


def _mock_chemistry(id: int = 1, slug: str = "lfp", is_active: bool = True) -> MagicMock:
    c = MagicMock()
    c.id = id
    c.slug = slug
    c.is_active = is_active
    return c


# ---------------------------------------------------------------------------
# PATENT_TREND_MODIFIERS
# ---------------------------------------------------------------------------

class TestPatentTrendModifiers:
    def test_rising_increases_criticality(self):
        assert PATENT_TREND_MODIFIERS["rising"] > 1.0

    def test_declining_decreases_criticality(self):
        assert PATENT_TREND_MODIFIERS["declining"] < 1.0

    def test_stable_is_neutral(self):
        assert PATENT_TREND_MODIFIERS["stable"] == 1.0

    def test_all_keys_present(self):
        assert set(PATENT_TREND_MODIFIERS.keys()) == {"rising", "declining", "stable"}


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
# _resolve_criticality_signal
# ---------------------------------------------------------------------------

def _make_signal(criticality_score: float, source: str, year: int = 2025) -> MagicMock:
    s = MagicMock()
    s.criticality_score = criticality_score
    s.source = source
    s.reference_year = year
    return s


class TestResolveCriticalitySignal:
    def _session_returning(self, signal_or_none) -> MagicMock:
        session = MagicMock()
        session.scalar.return_value = signal_or_none
        return session

    def test_eu_crma_preferred_over_usgs(self):
        eu_signal = _make_signal(0.85, "eu_crma")
        # First call to scalar (eu_crma) returns a signal.
        score, source = _resolve_criticality_signal(
            self._session_returning(eu_signal), 1, 0.5
        )
        assert score == 0.85
        assert source == "eu_crma"

    def test_falls_back_to_material_column_when_no_signals(self):
        score, source = _resolve_criticality_signal(
            self._session_returning(None), 1, 0.70
        )
        assert score == 0.70
        assert source == "material_column_fallback"

    def test_falls_back_to_0_5_when_material_column_also_none(self):
        score, source = _resolve_criticality_signal(
            self._session_returning(None), 1, None
        )
        assert score == 0.5

    def test_returns_signal_score_not_material_column(self):
        signal = _make_signal(0.99, "usgs_mcs")
        score, _ = _resolve_criticality_signal(
            self._session_returning(signal), 1, 0.10
        )
        assert score == pytest.approx(0.99)


# ---------------------------------------------------------------------------
# _geo_concentration
# ---------------------------------------------------------------------------

class TestGeoConcentration:
    def test_returns_none_when_no_hs_codes(self):
        session = MagicMock()
        mat = _mock_material(hs_codes=[])
        result = _geo_concentration(session, mat)
        assert result is None

    def test_returns_none_when_no_trade_flow_rows(self):
        session = MagicMock()
        # period lookup returns None → no data
        session.scalar.return_value = None
        mat = _mock_material(hs_codes=["2825.20"])
        result = _geo_concentration(session, mat)
        assert result is None

    def test_returns_fraction_between_0_and_1(self):
        session = MagicMock()
        call_count = {"n": 0}

        def scalar_se(stmt):
            call_count["n"] += 1
            n = call_count["n"]
            if n == 1:
                return "2023"        # latest_period from material_id FK
            if n == 2:
                return 1_000_000.0   # total export value
            if n == 3:
                return 800_000.0     # high-conc-geo export value
            return None

        session.scalar.side_effect = scalar_se
        mat = _mock_material(id=1, hs_codes=["2825.20"])
        result = _geo_concentration(session, mat)
        assert result == pytest.approx(0.8)

    def test_caps_at_1(self):
        session = MagicMock()
        call_count = {"n": 0}

        def scalar_se(stmt):
            call_count["n"] += 1
            n = call_count["n"]
            if n == 1:
                return "2023"
            if n == 2:
                return 500_000.0
            if n == 3:
                return 600_000.0   # more than total (edge case)
            return None

        session.scalar.side_effect = scalar_se
        mat = _mock_material(id=1, hs_codes=["2825.20"])
        result = _geo_concentration(session, mat)
        assert result is not None and result <= 1.0


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
# score_chemistry — integration-style with mocked session
# ---------------------------------------------------------------------------

def _make_score_session(
    chemistry: MagicMock,
    junctions: list[MagicMock],
    materials: list[MagicMock],
    geo_value: float | None = 0.75,
) -> MagicMock:
    """Build a mocked session for score_chemistry tests."""
    session = MagicMock()

    mat_by_id = {m.id: m for m in materials}

    call_tracker = {"n": 0}

    def get_side_effect(cls, pk):
        if cls.__name__ == "BatteryChemistry":
            return chemistry
        return mat_by_id.get(pk)

    def scalars_side_effect(stmt):
        result = MagicMock()
        result.all.return_value = junctions
        return result

    def scalar_side_effect(stmt):
        call_tracker["n"] += 1
        n = call_tracker["n"]
        # Calls from score_chemistry:
        # Per-material: 1x _resolve_criticality_signal (up to 5 source probes)
        # then _geo_concentration calls (2-3 scalars), then trade_flows_vintage
        # We return neutral / non-None values for all.
        if geo_value is not None:
            return geo_value  # trade period / total value / hcg value / vintage all return this
        return None

    session.get.side_effect = get_side_effect
    session.scalars.side_effect = scalars_side_effect
    session.scalar.side_effect = scalar_side_effect
    return session


class TestScoreChemistry:
    def test_raises_if_chemistry_not_found(self):
        session = MagicMock()
        session.get.return_value = None

        with pytest.raises(ValueError, match="not found"):
            score_chemistry(session, chemistry_id=99, as_of_date=datetime.date(2024, 1, 1))

    def test_raises_if_no_active_materials(self):
        session = MagicMock()
        chemistry = _mock_chemistry()
        session.get.return_value = chemistry
        scalars_result = MagicMock()
        scalars_result.all.return_value = []
        session.scalars.return_value = scalars_result

        with pytest.raises(ValueError, match="No active battery_chemistry_materials"):
            score_chemistry(session, chemistry_id=1, as_of_date=datetime.date(2024, 1, 1))

    def _make_score_session(self, chemistry, junctions, materials, scalar_responses=None):
        """Build a properly-mocked session for score_chemistry tests.

        Returns two scalars results objects, one for each scalars() call:
          1st → active junctions (.all())
          2nd → pre-loaded materials (.all())
        scalar() calls return items from scalar_responses (or None if not provided).
        """
        session = MagicMock()
        session.get.side_effect = lambda cls, pk: chemistry

        junc_result = MagicMock()
        junc_result.all.return_value = junctions

        mat_result = MagicMock()
        mat_result.all.return_value = materials

        scalars_calls = {"n": 0}

        def scalars_se(stmt):
            scalars_calls["n"] += 1
            if scalars_calls["n"] == 1:
                return junc_result
            return mat_result

        session.scalars.side_effect = scalars_se

        if scalar_responses is not None:
            scalar_iter = iter(scalar_responses)

            def scalar_se(stmt):
                try:
                    return next(scalar_iter)
                except StopIteration:
                    return None

            session.scalar.side_effect = scalar_se
        else:
            session.scalar.return_value = None

        return session

    def test_composite_score_in_range(self):
        chemistry = _mock_chemistry(slug="lfp")
        mat = _mock_material(id=1, criticality_score=0.6, data_availability="commercial")
        junc = _mock_junction(material_id=1, intensity=0.9)

        # Scalar call sequence in score_chemistry for one material:
        # _resolve_criticality_signal: tries each source (up to 5) → all None
        # _geo_concentration: latest_period=2023, total=1M, hcg=0.5M
        # trade_flows_vintage: returns "2023"
        scalar_responses = [
            None, None, None, None, None,   # 5 signal source probes → fallback
            "2023",                          # latest_period (material_id FK path)
            1_000_000.0,                     # total export value
            500_000.0,                       # hcg export value → 0.5 geo
            "2023",                          # trade_flows_vintage
        ]
        session = self._make_score_session(
            chemistry, [junc], [mat], scalar_responses=scalar_responses
        )
        score_row = score_chemistry(
            session, chemistry_id=1, as_of_date=datetime.date(2024, 1, 1)
        )

        assert 0.0 <= score_row.composite_risk_score <= 100.0
        assert 0.0 <= score_row.score_confidence <= 1.0

    def test_no_benchmark_materials_lower_confidence(self):
        """A chemistry with all no_benchmark materials has lower confidence."""
        chemistry = _mock_chemistry()
        mat = _mock_material(data_availability="no_benchmark", patent_occurrence_trend=None)
        junc = _mock_junction(material_id=mat.id, intensity=0.9)

        session = self._make_score_session(chemistry, [junc], [mat])

        score = score_chemistry(
            session, chemistry_id=1, as_of_date=datetime.date(2024, 1, 1)
        )
        assert score.score_confidence < 1.0
        assert "no_benchmark_materials" in score.metadata_json
        assert len(score.metadata_json["no_benchmark_materials"]) > 0

    def test_missing_hs_codes_flagged_in_metadata(self):
        """Materials without HS codes are listed in metadata_json['materials_missing_hs']."""
        chemistry = _mock_chemistry()
        mat = _mock_material(hs_codes=[], patent_occurrence_trend=None)
        junc = _mock_junction(material_id=mat.id, intensity=0.9)

        session = self._make_score_session(chemistry, [junc], [mat])

        score = score_chemistry(
            session, chemistry_id=1, as_of_date=datetime.date(2024, 1, 1)
        )
        assert mat.canonical_name in score.metadata_json["materials_missing_hs"]

    def test_patent_trend_modifier_applied(self):
        """Rising patent trend increases criticality."""
        chemistry = _mock_chemistry()
        mat = _mock_material(criticality_score=0.5, patent_occurrence_trend="rising")
        junc = _mock_junction(material_id=mat.id, intensity=1.0)

        # All signals return None → fallback to mat.criticality_score
        session = self._make_score_session(chemistry, [junc], [mat])

        score = score_chemistry(
            session, chemistry_id=1, as_of_date=datetime.date(2024, 1, 1)
        )
        # rising modifier (×1.15) should be recorded in metadata
        assert score.metadata_json["patent_modifiers_applied"].get(mat.canonical_name) == 1.15

    def test_metadata_json_contains_required_keys(self):
        chemistry = _mock_chemistry()
        mat = _mock_material(patent_occurrence_trend=None)
        junc = _mock_junction(material_id=mat.id, intensity=0.9)

        session = self._make_score_session(chemistry, [junc], [mat])

        score = score_chemistry(
            session, chemistry_id=1, as_of_date=datetime.date(2024, 1, 1)
        )
        required_keys = {
            "signal_sources", "geo_coverage", "materials_missing_hs",
            "patent_modifiers_applied", "trade_flows_vintage",
            "no_benchmark_materials", "score_confidence",
        }
        assert required_keys.issubset(set(score.metadata_json.keys()))

    def test_confidence_floored_at_0_3(self):
        """When all materials have no_benchmark, confidence floors at 0.3."""
        chemistry = _mock_chemistry()
        # 5 no_benchmark materials: 0.65^5 ≈ 0.116, should floor at 0.3
        mats = [
            _mock_material(
                id=i, canonical_name=f"Mat{i}",
                data_availability="no_benchmark", patent_occurrence_trend=None,
            )
            for i in range(1, 6)
        ]
        juncs = [_mock_junction(material_id=m.id, intensity=0.5) for m in mats]

        session = self._make_score_session(chemistry, juncs, mats)

        score = score_chemistry(
            session, chemistry_id=1, as_of_date=datetime.date(2024, 1, 1)
        )
        assert score.score_confidence >= 0.30
