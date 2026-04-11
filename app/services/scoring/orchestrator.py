"""
Scoring orchestrator: single entry point for computing and persisting company scores.

Call rescore_company() after ingestion or from the manual rescore API endpoint.
Never call component scorers (material_risk, geopolitical_risk, etc.) directly from
outside this module — they must be invoked through this coordinator so that the
full evidence → aggregation → scoring → persistence pipeline runs as a unit.

Transaction contract:
  rescore_company() calls db.flush() to obtain the new row ID but does NOT commit.
  The caller owns the transaction and must call db.commit() (or db.rollback() on
  failure). This allows ingestion + scoring to share a single atomic commit.
"""

from __future__ import annotations

import uuid
import structlog
from datetime import date
from typing import Optional

from sqlalchemy.orm import Session

from app.constants import RiskCategory
from app.models.company import CompanyScore
from app.services.scoring import (
    financial_pressure as fp_module,
    geopolitical_risk,
    material_risk,
    regulatory_risk,
    supplier_risk,
)
from app.services.scoring.evidence_aggregator import (
    derive_financial_inputs,
    derive_geopolitical_inputs,
    derive_material_inputs,
    derive_operational_inputs,
    derive_regulatory_inputs,
)
from app.services.scoring.evidence_query import (
    EventWithRelevance,
    get_active_compliance_obligations,
    get_company_material_exposure,
    get_events_for_company,
    get_filing_signals,
)
from app.services.scoring.types import (
    ComponentScores,
    DecayParameters,
    ScoringInputs,
    SupplierScoreRationale,
)

log = structlog.get_logger(__name__)


def rescore_company(
    db: Session,
    company_id: uuid.UUID,
    run_id: str,
    as_of_date: Optional[date] = None,
) -> CompanyScore:
    """
    Compute and persist a new company_scores row for this company.

    Steps:
      1. Query evidence by category (via evidence_query.py).
      2. Aggregate evidence into component inputs (via evidence_aggregator.py).
      3. Score each component (via the five pillar modules).
      4. Aggregate into an overall score (via supplier_risk.py).
      5. Build SupplierScoreRationale for rationale_json.
      6. Persist a new CompanyScore row (append-only — never overwrite).

    Returns the persisted CompanyScore ORM object (ID populated after flush).
    Raises on DB write failure — caller handles the transaction.
    """
    if as_of_date is None:
        as_of_date = date.today()

    log.info(
        "orchestrator.rescore_company.start",
        company_id=str(company_id),
        as_of_date=as_of_date.isoformat(),
        run_id=run_id,
    )

    # --- STEP 1: Query evidence by category ---
    material_exposures  = get_company_material_exposure(db, company_id)
    trade_events        = get_events_for_company(db, company_id, RiskCategory.GEOPOLITICAL_TRADE, as_of_date)
    regulatory_events   = get_events_for_company(db, company_id, RiskCategory.REGULATORY_COMPLIANCE, as_of_date)
    operational_events  = get_events_for_company(db, company_id, RiskCategory.OPERATIONAL, as_of_date)
    filing_events       = get_filing_signals(db, company_id)
    active_obligations  = get_active_compliance_obligations(db, company_id)

    # --- STEP 2: Aggregate evidence into component inputs ---
    crit, conc, trade_vol          = derive_material_inputs(material_exposures, trade_events, as_of_date)
    ctry_conc, exp_rest, tariff    = derive_geopolitical_inputs(trade_events, material_exposures, as_of_date)
    top_impacts, obligations, prox = derive_regulatory_inputs(regulatory_events, active_obligations, as_of_date)
    struct_dep, op_impacts         = derive_operational_inputs(operational_events, as_of_date)
    base_sig, lev_bon, liq_bon, fc = derive_financial_inputs(filing_events)

    # --- STEP 3: Score each component ---
    mat_score = material_risk.score_material_exposure(crit, conc, trade_vol)
    geo_score = geopolitical_risk.score_geopolitical_trade(ctry_conc, exp_rest, tariff)
    reg_score = regulatory_risk.score_regulatory_profile(top_impacts, obligations, prox)
    op_score  = _score_operational(struct_dep, op_impacts)
    fin_score = fp_module.score_financial_pressure(base_sig, lev_bon, liq_bon, fc)

    # --- STEP 4: Aggregate ---
    result = supplier_risk.aggregate_supplier_risk(
        mat_score, geo_score, reg_score, op_score, fin_score
    )

    # --- STEP 5: Build rationale ---
    top_evidence = _collect_top_evidence(
        trade_events, regulatory_events, operational_events, filing_events, n=5
    )
    rationale = SupplierScoreRationale(
        inputs=ScoringInputs(
            supplier_id=str(company_id),
            evidence_window_days=730,
            scoring_version=supplier_risk.SCORING_VERSION,
            run_id=run_id,
        ),
        components=ComponentScores(
            material=mat_score,
            geopolitical=geo_score,
            regulatory=reg_score,
            operational=op_score,
            financial=fin_score,
            overall=result["overall_risk_score"],
        ),
        top_evidence=top_evidence,
        decay=DecayParameters(
            eval_date=as_of_date.isoformat(),
            material_window=365,
            geopolitical_window=730,
            regulatory_window=365,
            operational_half_life=90,
        ),
        notes=_generate_notes(result, mat_score, geo_score, reg_score, op_score, fin_score),
    )

    # --- STEP 6: Persist (append-only) ---
    score_row = CompanyScore(
        company_id=company_id,
        as_of_date=as_of_date,
        material_concentration_risk_score=mat_score,
        geopolitical_trade_risk_score=geo_score,
        regulatory_risk_score=reg_score,
        operational_risk_score=op_score,
        financial_pressure_score=fin_score,
        overall_risk_score=result["overall_risk_score"],
        scoring_version=result["scoring_version"],
        rationale_json=rationale.model_dump(),
    )
    db.add(score_row)
    db.flush()  # populates score_row.id — caller owns db.commit()

    log.info(
        "orchestrator.rescore_company.done",
        company_id=str(company_id),
        score_id=score_row.id,
        overall=round(result["overall_risk_score"], 2),
        scoring_version=result["scoring_version"],
    )
    return score_row


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _score_operational(struct_dep: float, op_impacts: list[float]) -> float:
    """
    Operational score: 40% structural dependency + 60% weighted event rollup.
    op_impacts are already recency- and relevance-weighted event_impact values.
    """
    event_component = sum(op_impacts) / len(op_impacts) if op_impacts else 0.0
    raw = (0.40 * struct_dep + 0.60 * event_component) * 100
    return min(100.0, raw)


def _collect_top_evidence(
    *event_lists: list[EventWithRelevance],
    n: int = 5,
) -> list[str]:
    """Collect IDs of the top N events by severity_score across all categories."""
    all_ew = [ew for lst in event_lists for ew in lst]
    sorted_ew = sorted(
        all_ew, key=lambda ew: ew.event.severity_score or 0.0, reverse=True
    )
    return [str(ew.event.id) for ew in sorted_ew[:n]]


def _generate_notes(
    result: dict,
    mat: float,
    geo: float,
    reg: float,
    op: float,
    fin: float,
) -> str:
    overall = result["overall_risk_score"]
    dominant = max(
        [
            ("material concentration", mat),
            ("geopolitical/trade", geo),
            ("regulatory/compliance", reg),
            ("operational", op),
            ("financial", fin),
        ],
        key=lambda x: x[1],
    )
    band = (
        "Critical" if overall >= 75
        else "High" if overall >= 55
        else "Moderate" if overall >= 35
        else "Low"
    )
    return (
        f"Overall score {overall:.1f} ({band}). "
        f"Dominant risk driver: {dominant[0]} ({dominant[1]:.1f}). "
        f"Scoring version {result['scoring_version']}."
    )
