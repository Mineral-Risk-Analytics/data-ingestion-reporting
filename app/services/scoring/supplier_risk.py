"""Aggregate the five component scores into an overall supplier risk score (v2)."""

from __future__ import annotations

SCORING_VERSION = "2.0"

PILLAR_WEIGHTS = {
    "material":      0.30,
    "geopolitical":  0.20,
    "regulatory":    0.20,
    "operational":   0.15,
    "financial":     0.15,
}


def aggregate_supplier_risk(
    material_score: float,
    geopolitical_score: float,
    regulatory_score: float,
    operational_score: float,
    financial_score: float,
) -> dict:
    """
    Combine five pillar scores into an overall score dict.

    Weights: material 30%, geopolitical 20%, regulatory 20%, operational 15%,
    financial 15%.  Raw floats are stored unrounded; round only in the
    presentation/API layer to preserve precision for score-delta comparisons.

    Args:
        material_score:     Material Concentration Risk on [0, 100].
        geopolitical_score: Geopolitical / Trade Risk on [0, 100].
        regulatory_score:   Regulatory & Compliance Risk on [0, 100].
        operational_score:  Operational Risk on [0, 100].
        financial_score:    Financial Pressure on [0, 100].

    Returns:
        Dict with all component scores, weighted overall score, and scoring_version.
    """
    overall = (
        PILLAR_WEIGHTS["material"]     * material_score
        + PILLAR_WEIGHTS["geopolitical"] * geopolitical_score
        + PILLAR_WEIGHTS["regulatory"]   * regulatory_score
        + PILLAR_WEIGHTS["operational"]  * operational_score
        + PILLAR_WEIGHTS["financial"]    * financial_score
    )
    return {
        "material_concentration_risk_score": material_score,
        "geopolitical_trade_risk_score":     geopolitical_score,
        "regulatory_compliance_risk_score":  regulatory_score,
        "operational_risk_score":            operational_score,
        "financial_pressure_score":          financial_score,
        "overall_risk_score":                overall,
        "scoring_version":                   SCORING_VERSION,
    }
