"""Aggregate the component scores into an overall supplier risk score (v3).

Six pillars at v3.0 (default weights below). The new
``supply_chain_propagation`` pillar captures *second-party* risk via
``CompanySupplyRelationship`` — the rollup of *other* companies' persisted
overall risk scores reachable up to ``propagation_max_depth`` tiers from
the buyer.

When ``propagation_score`` is ``None`` (no reachable suppliers, or scoped
runs that disable propagation), the remaining five weights are renormalised
to sum to 1.0 so single-tier companies (and the existing test fixtures)
aren't artificially deflated.
"""

from __future__ import annotations

from typing import Optional

# 4.0 (2026-07-17): V1 scoring reset — docs/design/scoring_v1_spec.md.
# Material Concentration = stage-max structural sub-scores from the share
# tables (stage_concentration.py); non-producers score 0; §7b freshness
# gate on share vintages.  L0 node composites, the stage-weighted rollup,
# the material-HHI lift, and the legacy material_fallback no longer feed
# the pillar.  (3.1 history: Fix A share>0 gating + Fix B hhi×sqrt(share);
# see docs/design/concentration_share_weighting.md.)  Chemistry rollup
# keeps its own methodology_version=2.0 tag (unrelated axis).
SCORING_VERSION = "4.3"  # 4.3 (2026-07-26): obligation uplift soft-cap 40×(1−e^(−raw/35)) — no more pinning at 40. 4.2 (2026-07-23): export/tariff/subsidy sub-inputs avg→max. 4.1 (2026-07-20): governance amplifier in concentration

PILLAR_WEIGHTS: dict[str, float] = {
    "material":                  0.25,
    "geopolitical":              0.20,
    "regulatory":                0.20,
    "operational":               0.10,
    "financial":                 0.10,
    "supply_chain_propagation":  0.15,
}


def aggregate_supplier_risk(
    material_score: float,
    geopolitical_score: float,
    regulatory_score: float,
    operational_score: float,
    financial_score: float,
    propagation_score: Optional[float] = None,
) -> dict:
    """
    Combine pillar scores into an overall score dict.

    Default weights at v3.0:
      material 25%, geopolitical 20%, regulatory 20%,
      operational 10%, financial 10%, supply_chain_propagation 15%.

    When ``propagation_score`` is ``None``, the remaining five weights are
    renormalised so the overall is not biased downward by missing propagation
    signal. ``supply_chain_propagation_score`` in the returned dict is
    ``None`` in that case so the persistence layer can write NULL to the
    ``company_scores.supply_chain_propagation_score`` column.

    Raw floats are stored unrounded; round only in the presentation/API
    layer to preserve precision for score-delta comparisons.
    """
    if propagation_score is None:
        active_keys = (
            "material",
            "geopolitical",
            "regulatory",
            "operational",
            "financial",
        )
        weight_sum = sum(PILLAR_WEIGHTS[k] for k in active_keys)
        scores = {
            "material":     material_score,
            "geopolitical": geopolitical_score,
            "regulatory":   regulatory_score,
            "operational":  operational_score,
            "financial":    financial_score,
        }
        overall = sum(
            (PILLAR_WEIGHTS[k] / weight_sum) * scores[k] for k in active_keys
        )
    else:
        overall = (
            PILLAR_WEIGHTS["material"]                 * material_score
            + PILLAR_WEIGHTS["geopolitical"]             * geopolitical_score
            + PILLAR_WEIGHTS["regulatory"]               * regulatory_score
            + PILLAR_WEIGHTS["operational"]              * operational_score
            + PILLAR_WEIGHTS["financial"]                * financial_score
            + PILLAR_WEIGHTS["supply_chain_propagation"] * propagation_score
        )

    return {
        "material_concentration_risk_score":  material_score,
        "geopolitical_trade_risk_score":      geopolitical_score,
        "regulatory_compliance_risk_score":   regulatory_score,
        "operational_risk_score":             operational_score,
        "financial_pressure_score":           financial_score,
        "supply_chain_propagation_score":     propagation_score,
        "overall_risk_score":                 overall,
        "scoring_version":                    SCORING_VERSION,
    }
