"""Scoring module tests (v2 five-pillar framework)."""

import pytest

from app.services.scoring.event_impact import compute_effective_confidence, compute_event_impact
from app.services.scoring.financial_pressure import score_financial_pressure
from app.services.scoring.geopolitical_risk import score_geopolitical_trade
from app.services.scoring.material_risk import score_material_exposure
from app.services.scoring.regulatory_risk import COMPLIANCE_OBLIGATIONS, score_regulatory_profile
from app.services.scoring.supplier_risk import SCORING_VERSION, aggregate_supplier_risk


# ---------------------------------------------------------------------------
# material_risk
# ---------------------------------------------------------------------------

def test_material_risk_basic() -> None:
    score = score_material_exposure(criticality=0.8, concentration=0.6, trade_volatility=0.5)
    assert 0 <= score <= 100
    # Expected: (0.35*0.8 + 0.35*0.6 + 0.30*0.5) * 100 = (0.28 + 0.21 + 0.15) * 100 = 64.0
    assert abs(score - 64.0) < 0.01


def test_material_risk_clamped_at_100() -> None:
    score = score_material_exposure(criticality=1.0, concentration=1.0, trade_volatility=1.0)
    assert score == 100.0


def test_material_risk_zero() -> None:
    score = score_material_exposure(criticality=0.0, concentration=0.0, trade_volatility=0.0)
    assert score == 0.0


# ---------------------------------------------------------------------------
# regulatory_risk
# ---------------------------------------------------------------------------

def test_regulatory_risk_event_driven_only() -> None:
    score = score_regulatory_profile(top_event_impacts=[0.8, 0.6, 0.4], active_obligations=[])
    # top-3 avg = (0.8+0.6+0.4)/3 = 0.6; * 1.0 * 60 = 36.0
    assert abs(score - 36.0) < 0.01


def test_regulatory_obligation_uplift() -> None:
    # UFLPA (25) + EU_BATTERY_REG_2023 (20) = 45 → capped at 40
    uplift = min(40.0, COMPLIANCE_OBLIGATIONS["UFLPA"] + COMPLIANCE_OBLIGATIONS["EU_BATTERY_REG_2023"])
    assert uplift == 40.0

    score = score_regulatory_profile(
        top_event_impacts=[],
        active_obligations=[("UFLPA", 1.0), ("EU_BATTERY_REG_2023", 1.0)],
    )
    assert score == 40.0


def test_regulatory_uflpa_only_uplift() -> None:
    score = score_regulatory_profile(
        top_event_impacts=[], active_obligations=[("UFLPA", 1.0)]
    )
    assert score == 25.0


def test_regulatory_combined_capped_at_100() -> None:
    # Max event score 60 + max obligation 40 = 100
    score = score_regulatory_profile(
        top_event_impacts=[1.0, 1.0, 1.0],
        active_obligations=[
            ("UFLPA", 1.0),
            ("EU_BATTERY_REG_2023", 1.0),
            ("IRA_DOMESTIC", 1.0),
        ],
    )
    assert score == 100.0


# ---------------------------------------------------------------------------
# financial_pressure
# ---------------------------------------------------------------------------

def test_financial_pressure_basic() -> None:
    score = score_financial_pressure(
        base_filing_signal=20.0,
        leverage_warning_bonus=15.0,
        liquidity_stress_bonus=10.0,
    )
    assert score == 45.0


def test_financial_pressure_sparse_evidence() -> None:
    # evidence_count=1 should halve the raw score
    # 11.4-Fin-B (2026-06-06): renamed filing_count → evidence_count.
    raw = 20.0 + 10.0 + 10.0  # = 40
    score = score_financial_pressure(
        base_filing_signal=20.0,
        leverage_warning_bonus=10.0,
        liquidity_stress_bonus=10.0,
        evidence_count=1,
    )
    assert abs(score - raw * (1 / 2.0)) < 0.01


def test_financial_pressure_zero_filings_zeroes_score() -> None:
    score = score_financial_pressure(
        base_filing_signal=40.0,
        leverage_warning_bonus=30.0,
        liquidity_stress_bonus=30.0,
        evidence_count=0,
    )
    assert score == 0.0


def test_financial_pressure_bad_input_raises() -> None:
    with pytest.raises(ValueError):
        score_financial_pressure(base_filing_signal=50.0, leverage_warning_bonus=0, liquidity_stress_bonus=0)


# ---------------------------------------------------------------------------
# geopolitical_risk
# ---------------------------------------------------------------------------

def test_geopolitical_trade_basic() -> None:
    score = score_geopolitical_trade(
        country_concentration=0.8,
        export_restriction_exposure=0.6,
        tariff_exposure=0.4,
    )
    # (0.40*0.8 + 0.35*0.6 + 0.25*0.4) * 100 = (0.32 + 0.21 + 0.10) * 100 = 63.0
    assert abs(score - 63.0) < 0.01


def test_geopolitical_trade_max() -> None:
    assert score_geopolitical_trade(1.0, 1.0, 1.0) == 100.0


# ---------------------------------------------------------------------------
# event_impact — effective confidence floor
# ---------------------------------------------------------------------------

def test_effective_confidence_floor() -> None:
    # severity=0.85 >= 0.80 threshold → floor applied, confidence 0.30 → 0.60
    assert compute_effective_confidence(severity=0.85, confidence=0.30) == 0.60


def test_effective_confidence_no_floor() -> None:
    # severity=0.70 < 0.80 → no floor, raw confidence returned
    assert compute_effective_confidence(severity=0.70, confidence=0.30) == 0.30


def test_effective_confidence_high_conf_unchanged() -> None:
    # confidence already above floor — floor has no effect
    assert compute_effective_confidence(severity=0.90, confidence=0.75) == 0.75


def test_compute_event_impact_nominal() -> None:
    impact = compute_event_impact(severity=0.8, confidence=0.7)
    # eff_conf = 0.7 (above floor); 0.8 * 0.7 * 1.0 * 1.0 = 0.56
    assert abs(impact - 0.56) < 1e-9


def test_compute_event_impact_floor_applied() -> None:
    impact = compute_event_impact(severity=0.85, confidence=0.30)
    # eff_conf = 0.60; 0.85 * 0.60 * 1.0 * 1.0 = 0.51
    assert abs(impact - 0.51) < 1e-9


def test_compute_event_impact_bad_severity_raises() -> None:
    with pytest.raises(ValueError):
        compute_event_impact(severity=1.5, confidence=0.5)


def test_compute_event_impact_bad_recency_raises() -> None:
    with pytest.raises(ValueError):
        compute_event_impact(severity=0.5, confidence=0.5, recency_multiplier=0.1)


# ---------------------------------------------------------------------------
# aggregate_supplier_risk — five-pillar contract
# ---------------------------------------------------------------------------

def test_supplier_aggregate_five_pillars_no_propagation() -> None:
    """When propagation is None the remaining five pillars are renormalised."""
    result = aggregate_supplier_risk(
        material_score=60.0,
        geopolitical_score=50.0,
        regulatory_score=40.0,
        operational_score=30.0,
        financial_score=20.0,
    )
    # v3.0 weights w/o propagation, renormalised:
    # base = 0.25/0.20/0.20/0.10/0.10 → sum 0.85
    # weighted_raw = 0.25*60 + 0.20*50 + 0.20*40 + 0.10*30 + 0.10*20 = 38.0
    # overall = 38.0 / 0.85 ≈ 44.7059
    assert abs(result["overall_risk_score"] - (38.0 / 0.85)) < 0.01
    assert result["scoring_version"] == SCORING_VERSION == "3.0"
    assert result["supply_chain_propagation_score"] is None


def test_supplier_aggregate_six_pillars_with_propagation() -> None:
    """Sixth pillar enters the weighted sum at its full 0.15 weight."""
    result = aggregate_supplier_risk(
        material_score=60.0,
        geopolitical_score=50.0,
        regulatory_score=40.0,
        operational_score=30.0,
        financial_score=20.0,
        propagation_score=70.0,
    )
    # 0.25*60 + 0.20*50 + 0.20*40 + 0.10*30 + 0.10*20 + 0.15*70
    # = 15 + 10 + 8 + 3 + 2 + 10.5 = 48.5
    assert abs(result["overall_risk_score"] - 48.5) < 0.01
    assert result["supply_chain_propagation_score"] == 70.0


def test_supplier_aggregate_all_keys_present() -> None:
    result = aggregate_supplier_risk(50, 50, 50, 50, 50)
    expected_keys = {
        "material_concentration_risk_score",
        "geopolitical_trade_risk_score",
        "regulatory_compliance_risk_score",
        "operational_risk_score",
        "financial_pressure_score",
        "supply_chain_propagation_score",
        "overall_risk_score",
        "scoring_version",
    }
    assert expected_keys == set(result.keys())


def test_supplier_aggregate_uniform_inputs() -> None:
    result = aggregate_supplier_risk(50, 50, 50, 50, 50, propagation_score=50)
    # All pillars = 50, weights sum to 1.0 → overall = 50.0
    assert abs(result["overall_risk_score"] - 50.0) < 0.01


def test_supplier_aggregate_uniform_inputs_no_propagation_renormalises() -> None:
    """Five identical pillars without propagation must still average to that value."""
    result = aggregate_supplier_risk(50, 50, 50, 50, 50)
    assert abs(result["overall_risk_score"] - 50.0) < 0.01
