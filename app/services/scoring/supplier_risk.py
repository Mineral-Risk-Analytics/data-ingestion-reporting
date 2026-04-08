"""Combine sub-scores into an overall supplier view."""

from __future__ import annotations

from app.services.scoring.types import ScoreResult


def aggregate_supplier_risk(
    *,
    material_score: float,
    regulatory_score: float,
    financial_pressure_score: float,
    operational_score: float,
) -> ScoreResult:
    """
    Default weights favor materials + regulatory for battery supply-chain use cases.
    """
    w_mat, w_reg, w_fin, w_ops = 0.35, 0.35, 0.15, 0.15
    overall = (
        material_score * w_mat
        + regulatory_score * w_reg
        + financial_pressure_score * w_fin
        + operational_score * w_ops
    )
    rationale = [
        f"Weighted blend (mat/reg/fin/ops): "
        f"{w_mat:.2f}/{w_reg:.2f}/{w_fin:.2f}/{w_ops:.2f}",
        f"Inputs — material={material_score:.1f}, regulatory={regulatory_score:.1f}, "
        f"financial={financial_pressure_score:.1f}, operational={operational_score:.1f}",
    ]
    return ScoreResult(score=overall, rationale=rationale)
