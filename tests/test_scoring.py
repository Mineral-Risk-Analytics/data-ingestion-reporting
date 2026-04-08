"""Scoring module tests."""

from app.services.scoring.macro_context import macro_context_score
from app.services.scoring.material_risk import score_material_exposure
from app.services.scoring.regulatory_risk import score_regulatory_profile
from app.services.scoring.supplier_risk import aggregate_supplier_risk


def test_material_risk_clamped() -> None:
    r = score_material_exposure(
        exposure_score=80,
        num_distinct_sources=1,
        is_critical_mineral=True,
    )
    assert 0 <= r.score <= 100
    assert r.rationale


def test_regulatory_risk() -> None:
    r = score_regulatory_profile(recent_high_severity_events=2, active_regulation_count=3)
    assert r.score > 0


def test_supplier_aggregate() -> None:
    r = aggregate_supplier_risk(
        material_score=50,
        regulatory_score=60,
        financial_pressure_score=40,
        operational_score=30,
    )
    assert 40 < r.score < 55


def test_macro_context() -> None:
    r = macro_context_score(demand_index=70, infrastructure_stress_index=50)
    assert r.score == 60.0
