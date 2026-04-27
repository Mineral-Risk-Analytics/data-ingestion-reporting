"""Material global risk score rollup.

Aggregates MaterialGeographyRiskScore rows (one per geography) into a single
global risk view per material, weighted by trade-flow export value.

Pipeline position:

    score_all_active_materials()        → material_geography_risk_scores
              ↓  THIS MODULE
    score_all_material_global_rollups() → material_global_risk_scores
              ↓  chemistry_risk.score_all_chemistries_from_rollup()
    chemistry_risk_scores

Weight resolution (three-tier, in order):
    1. TradeFlow export value — most direct signal: how much did this geography
       export of this material in the most recent period?
    2. MaterialProductionShare — used when TradeFlow has no coverage (i.e. trade
       data not yet ingested or HS codes not mapped). Already populated from
       USGS MCS.
    3. Equal weight (1.0 per geography) — last resort when neither source has
       data for a geography. Logged at WARNING.

Transaction contract:
    score_material_global_rollup() flushes but does NOT commit. The caller owns
    the transaction, consistent with market_aggregator.score_material_geography().
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Optional

import structlog
from sqlalchemy import func as sqlfunc
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.scoring import MaterialGeographyRiskScore, MaterialGlobalRiskScore
from app.models.supply import Material, MaterialProductionShare, TradeFlow
from app.services.scoring.market_aggregator import MARKET_PILLAR_WEIGHTS
from app.services.scoring.supplier_risk import SCORING_VERSION

log = structlog.get_logger(__name__)

ROLLUP_VERSION = "1.0"

# Pillar column names on MaterialGeographyRiskScore — used for generic weighted
# averaging so adding a sixth pillar later only requires touching this list.
_PILLAR_COLS = [
    "material_concentration_score",
    "geopolitical_trade_score",
    "regulatory_compliance_score",
    "operational_score",
    "financial_pressure_score",
]


# ---------------------------------------------------------------------------
# Weight resolution helpers
# ---------------------------------------------------------------------------

def _trade_weights(
    db: Session,
    material_id: int,
    geo_codes: list[str],
) -> dict[str, float]:
    """Return {country_code: trade_value_usd} from TradeFlow for these geos.

    Uses the most recent period with export data for this material.
    Returns an empty dict if no TradeFlow rows exist.
    """
    latest_period = db.scalar(
        select(sqlfunc.max(TradeFlow.period))
        .where(
            TradeFlow.material_id == material_id,
            TradeFlow.import_export_flag == "export",
        )
    )
    if latest_period is None:
        return {}

    rows = db.execute(
        select(
            TradeFlow.reporter_country,
            sqlfunc.sum(TradeFlow.trade_value_usd).label("total"),
        )
        .where(
            TradeFlow.material_id == material_id,
            TradeFlow.import_export_flag == "export",
            TradeFlow.period == latest_period,
            TradeFlow.reporter_country.in_(geo_codes),
            TradeFlow.trade_value_usd.is_not(None),
        )
        .group_by(TradeFlow.reporter_country)
    ).all()

    return {row.reporter_country: float(row.total or 0.0) for row in rows if (row.total or 0.0) > 0}


def _production_share_weights(
    db: Session,
    material_id: int,
    geo_codes: list[str],
) -> dict[str, float]:
    """Return {country_code: production_share} from MaterialProductionShare."""
    latest_year = db.scalar(
        select(sqlfunc.max(MaterialProductionShare.reference_year))
        .where(MaterialProductionShare.material_id == material_id)
    )
    if latest_year is None:
        return {}

    rows = db.execute(
        select(
            MaterialProductionShare.country_code,
            MaterialProductionShare.production_share,
        )
        .where(
            MaterialProductionShare.material_id == material_id,
            MaterialProductionShare.reference_year == latest_year,
            MaterialProductionShare.country_code.in_(geo_codes),
            MaterialProductionShare.production_share > 0,
        )
    ).all()

    return {row.country_code: float(row.production_share) for row in rows}


def _resolve_weights(
    db: Session,
    material_id: int,
    geo_scores: list[MaterialGeographyRiskScore],
) -> tuple[dict[str, float], str, Optional[float]]:
    """
    Resolve a weight for every geography in geo_scores.

    Returns (weights, weight_source, total_trade_value_usd).

    weight_source is one of:
        "trade_flow"       — TradeFlow export values (preferred)
        "production_share" — MaterialProductionShare fallback
        "equal"            — equal weight (1.0 each), last resort
        "mixed"            — some geos from trade, remaining from production share

    total_trade_value_usd is the sum of trade values used (None when not applicable).
    """
    geo_codes = [s.geography_code for s in geo_scores]
    trade = _trade_weights(db, material_id, geo_codes)
    prod = _production_share_weights(db, material_id, geo_codes)

    weights: dict[str, float] = {}
    sources_used: set[str] = set()

    for geo in geo_codes:
        if geo in trade:
            weights[geo] = trade[geo]
            sources_used.add("trade_flow")
        elif geo in prod:
            weights[geo] = prod[geo]
            sources_used.add("production_share")
        else:
            weights[geo] = 1.0
            sources_used.add("equal")
            log.warning(
                "global_rollup.equal_weight_fallback",
                material_id=material_id,
                geography_code=geo,
                note="No TradeFlow or ProductionShare data — using equal weight",
            )

    total_trade_value = sum(trade.values()) if trade else None

    if len(sources_used) == 1:
        weight_source = sources_used.pop()
    else:
        weight_source = "mixed"

    return weights, weight_source, total_trade_value


# ---------------------------------------------------------------------------
# Core scorer
# ---------------------------------------------------------------------------

def score_material_global_rollup(
    db: Session,
    material_id: int,
    as_of_date: date,
    *,
    run_id: Optional[str] = None,
    persist: bool = True,
) -> MaterialGlobalRiskScore:
    """
    Compute (and optionally persist) a trade-flow-weighted global risk score for
    a material by rolling up its MaterialGeographyRiskScore rows.

    For each geography that has a scored row ≤ as_of_date (most recent per geo),
    the five pillar scores are weighted by that geography's export trade value
    (or production share / equal weight as fallback). The weighted average of
    each pillar is stored alongside the overall score.

    Returns the MaterialGlobalRiskScore ORM object. ``id`` is populated after
    db.flush() when ``persist=True``; caller owns db.commit().

    Raises ValueError if no MaterialGeographyRiskScore rows exist for this
    material — run score_all_active_materials() first.
    """
    if run_id is None:
        run_id = f"global-{uuid.uuid4()}"

    log.info(
        "global_rollup.start",
        material_id=material_id,
        as_of_date=as_of_date.isoformat(),
        run_id=run_id,
    )

    # Step 1: Most recent geo score per geography for this material, ≤ as_of_date
    latest_geo_subq = (
        select(
            MaterialGeographyRiskScore.geography_code,
            sqlfunc.max(MaterialGeographyRiskScore.as_of_date).label("max_date"),
        )
        .where(
            MaterialGeographyRiskScore.material_id == material_id,
            MaterialGeographyRiskScore.as_of_date <= as_of_date,
        )
        .group_by(MaterialGeographyRiskScore.geography_code)
        .subquery()
    )
    geo_scores: list[MaterialGeographyRiskScore] = list(
        db.scalars(
            select(MaterialGeographyRiskScore)
            .join(
                latest_geo_subq,
                (MaterialGeographyRiskScore.geography_code == latest_geo_subq.c.geography_code)
                & (MaterialGeographyRiskScore.as_of_date == latest_geo_subq.c.max_date),
            )
            .where(MaterialGeographyRiskScore.material_id == material_id)
        ).all()
    )

    if not geo_scores:
        raise ValueError(
            f"No MaterialGeographyRiskScore rows found for material_id={material_id} "
            f"on or before {as_of_date}. Run score_all_active_materials() first."
        )

    # Step 2: Resolve weights
    weights, weight_source, total_trade_value = _resolve_weights(db, material_id, geo_scores)

    weight_sum = sum(weights.values())
    if weight_sum <= 0:
        raise ValueError(
            f"All weights are zero for material_id={material_id} — cannot compute rollup."
        )

    # Step 3: Weighted average per pillar
    pillar_sums: dict[str, float] = {col: 0.0 for col in _PILLAR_COLS}
    geo_detail: list[dict] = []

    for gs in geo_scores:
        w = weights.get(gs.geography_code, 0.0)
        if w <= 0:
            continue
        w_norm = w / weight_sum  # normalised weight [0, 1]

        for col in _PILLAR_COLS:
            val = getattr(gs, col)
            if val is not None:
                pillar_sums[col] += w_norm * float(val)

        geo_detail.append({
            "geography_code": gs.geography_code,
            "weight": round(w, 4),
            "weight_normalised": round(w_norm, 4),
            "geo_score_date": gs.as_of_date.isoformat(),
            "pillars": {col: getattr(gs, col) for col in _PILLAR_COLS},
            "overall": gs.overall_risk_score,
        })

    # Step 4: Overall score using MARKET_PILLAR_WEIGHTS
    overall = (
        MARKET_PILLAR_WEIGHTS["material"]     * pillar_sums["material_concentration_score"]
        + MARKET_PILLAR_WEIGHTS["geopolitical"] * pillar_sums["geopolitical_trade_score"]
        + MARKET_PILLAR_WEIGHTS["regulatory"]   * pillar_sums["regulatory_compliance_score"]
        + MARKET_PILLAR_WEIGHTS["operational"]  * pillar_sums["operational_score"]
        + MARKET_PILLAR_WEIGHTS["financial"]    * pillar_sums["financial_pressure_score"]
    )

    # Step 5: Count geographies that had a meaningful (non-zero) weight
    trade_weighted_geo_count = sum(1 for w in weights.values() if w > 0)

    rationale = {
        "run_id": run_id,
        "scoring_version": ROLLUP_VERSION,
        "as_of_date": as_of_date.isoformat(),
        "weight_source": weight_source,
        "total_trade_value_usd": total_trade_value,
        "geography_count": len(geo_scores),
        "pillar_weighted_averages": {col: round(v, 2) for col, v in pillar_sums.items()},
        "geographies": geo_detail,
        "notes": (
            f"Trade-flow-weighted rollup across {len(geo_scores)} geographies "
            f"(weight_source={weight_source}). "
            f"Overall {overall:.1f}."
        ),
    }

    score_row = MaterialGlobalRiskScore(
        material_id=material_id,
        as_of_date=as_of_date,
        material_concentration_score=round(pillar_sums["material_concentration_score"], 2),
        geopolitical_trade_score=round(pillar_sums["geopolitical_trade_score"], 2),
        regulatory_compliance_score=round(pillar_sums["regulatory_compliance_score"], 2),
        operational_score=round(pillar_sums["operational_score"], 2),
        financial_pressure_score=round(pillar_sums["financial_pressure_score"], 2),
        overall_risk_score=round(overall, 2),
        trade_weighted_geo_count=trade_weighted_geo_count,
        total_trade_value_usd=total_trade_value,
        rationale_json=rationale,
        scoring_version=ROLLUP_VERSION,
    )

    if persist:
        db.add(score_row)
        db.flush()

    log.info(
        "global_rollup.done",
        material_id=material_id,
        overall=round(overall, 2),
        geo_count=len(geo_scores),
        weight_source=weight_source,
        persisted=persist,
    )
    return score_row


# ---------------------------------------------------------------------------
# Batch entry point
# ---------------------------------------------------------------------------

def score_all_material_global_rollups(
    db: Session,
    as_of_date: Optional[date] = None,
    *,
    run_id: Optional[str] = None,
) -> list[MaterialGlobalRiskScore]:
    """
    Roll up every material that has at least one MaterialGeographyRiskScore.

    Each material is committed individually so a failure on one material does
    not roll back the entire batch. Caller does not need to commit after.

    Returns list of persisted MaterialGlobalRiskScore objects.
    """
    from datetime import date as date_cls
    if as_of_date is None:
        as_of_date = date_cls.today()

    if run_id is None:
        run_id = f"global-batch-{uuid.uuid4()}"

    # Find all material_ids that have at least one geo score
    material_ids: list[int] = [
        row[0]
        for row in db.execute(
            select(MaterialGeographyRiskScore.material_id)
            .where(MaterialGeographyRiskScore.as_of_date <= as_of_date)
            .distinct()
        ).all()
    ]

    log.info(
        "global_rollup.batch.start",
        material_count=len(material_ids),
        as_of_date=as_of_date.isoformat(),
        run_id=run_id,
    )

    results: list[MaterialGlobalRiskScore] = []
    for material_id in material_ids:
        try:
            score_row = score_material_global_rollup(
                db,
                material_id,
                as_of_date,
                run_id=f"{run_id}-{material_id}",
                persist=True,
            )
            results.append(score_row)
            db.commit()
        except Exception:
            db.rollback()
            log.exception(
                "global_rollup.batch.error",
                material_id=material_id,
            )

    log.info(
        "global_rollup.batch.done",
        scored=len(results),
        total=len(material_ids),
        run_id=run_id,
    )
    return results


__all__ = [
    "score_material_global_rollup",
    "score_all_material_global_rollups",
]
