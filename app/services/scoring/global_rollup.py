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
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models.scoring import MaterialGeographyRiskScore, MaterialGlobalRiskScore
from app.models.supply import (
    HsCodeMaterialMapping,
    Material,
    MaterialProductionShare,
    TradeFlow,
)
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

# ---------------------------------------------------------------------------
# Period averaging window (G8 audit fix, 2026-05-06)
# ---------------------------------------------------------------------------
# TradeFlow rows arrive with two distinct period formats from two ingesters:
#
#   ``"YYYY"``       — UN Comtrade annual exports (comtrade.py)
#   ``"YYYY-MM"`` /  — US Census Bureau monthly exports (pipeline.py)
#   ``"YYYYMM"``       — reporter_country is hardcoded to ``"US"``
#
# Pre-fix: ``_trade_weights`` did ``MAX(period)`` over the mixed format,
# letting the lexicographically-larger Census monthly periods displace
# Comtrade annual rows.  Symptom: any material with Census coverage saw
# only US weight in the global rollup, dropping every other producer to
# zero and silently breaking cross-country comparison.
#
# Post-fix: annual and monthly are queried as separate sources.  Annual
# is the canonical global cross-country source (Comtrade reports every
# country's exports).  Monthly is US-only by construction and useful
# only for US-specific scoring contexts.  ``_trade_weights`` prefers
# annual when it covers the requested geos and falls back to monthly
# only when no annual coverage exists for those geos.
#
# Multi-period averaging absorbs single-year shocks (pandemic, embargo
# years) that previously distorted the per-material global score.  A
# 3-year rolling window is industry standard for trade analysis
# (IEA/OECD use 3-year; USGS uses 5-year for less responsive metrics).
# A 12-month window is the monthly-data equivalent.

_ANNUAL_PERIODS_TO_AVERAGE = 3
_MONTHLY_PERIODS_TO_AVERAGE = 12


def _is_annual_period(period: str) -> bool:
    """``True`` for ``"YYYY"`` (Comtrade); ``False`` for ``YYYY-MM``/``YYYYMM`` (Census)."""
    if not period:
        return False
    return len(period) == 4 and period.isdigit()


def _trade_weights(
    db: Session,
    material_id: int,
    geo_codes: list[str],
) -> tuple[dict[str, float], dict]:
    """Return ({country_code: avg_trade_value_usd}, period_metadata).

    Averages export values across the most recent N periods (3 years for
    annual data, 12 months for monthly).  This smooths single-period
    shocks (pandemic year, embargo year) that previously distorted
    per-material global scoring.

    Annual (Comtrade) is preferred for cross-country weighting because
    Comtrade reports every country's exports.  Monthly (Census) is US-
    only by construction and is used only as a fallback when no annual
    coverage exists for the requested geos.

    Returns an empty dict + empty metadata if no usable TradeFlow rows
    exist.

    period_metadata shape::

        {
            "granularity": "annual" | "monthly" | None,
            "periods_averaged": ["2022", "2023", "2024"],  # or YYYY-MMs
            "n_periods": int,
            "fallback_used": bool,    # True if monthly used because annual didn't cover
        }
    """
    annual_weights, annual_periods = _trade_weights_for_granularity(
        db, material_id, geo_codes, annual=True
    )
    if annual_weights:
        return annual_weights, {
            "granularity":      "annual",
            "periods_averaged": annual_periods,
            "n_periods":        len(annual_periods),
            "fallback_used":    False,
        }

    # Annual returned nothing covering these geos — fall back to monthly.
    # This path is dominated by US-only data (Census) but it's better than
    # zero coverage; the caller's equal-weight fallback would otherwise
    # kick in for everyone, which is even less informative.
    monthly_weights, monthly_periods = _trade_weights_for_granularity(
        db, material_id, geo_codes, annual=False
    )
    return monthly_weights, {
        "granularity":      "monthly" if monthly_weights else None,
        "periods_averaged": monthly_periods,
        "n_periods":        len(monthly_periods),
        "fallback_used":    bool(monthly_weights),
    }


def _trade_weights_for_granularity(
    db: Session,
    material_id: int,
    geo_codes: list[str],
    *,
    annual: bool,
) -> tuple[dict[str, float], list[str]]:
    """Inner helper used by ``_trade_weights`` for one granularity.

    Selects the most recent N distinct periods of the requested granularity
    that have any export data for this material, then averages
    ``trade_value_usd`` per reporter across those periods.

    Returns ({country_code: average_trade_value_usd}, list_of_periods_used).
    """
    target_window = (
        _ANNUAL_PERIODS_TO_AVERAGE if annual else _MONTHLY_PERIODS_TO_AVERAGE
    )

    # Step 1: gather all distinct periods with data for this material at
    # this granularity.  Period is a String column with mixed formats, so
    # we pull them all and filter in Python — cheap; ~10-20 distinct values.
    all_periods: list[str] = list(
        db.scalars(
            select(TradeFlow.period.distinct()).where(
                TradeFlow.material_id == material_id,
                TradeFlow.import_export_flag == "export",
            )
        ).all()
    )
    matching = [p for p in all_periods if _is_annual_period(p) is annual]
    if not matching:
        return {}, []

    # Step 2: pick the N most recent (lexicographic sort works for both
    # ``YYYY`` and ``YYYY-MM`` / ``YYYYMM`` formats since both are
    # zero-padded left-to-right).
    matching.sort(reverse=True)
    periods_to_use = matching[:target_window]

    # Step 3: per reporter, average trade_value_usd across those periods.
    # SUM divided by COUNT(DISTINCT period) gives the cross-period average
    # weighted by per-period sub-aggregation (Comtrade rows can have
    # multiple HS subheadings per material per period; summing all and
    # dividing by period count is the right semantic).
    #
    # Confidence weighting (2026-05-09 audit extension):
    #   Each row's trade_value_usd is multiplied by the HS→material mapping
    #   confidence so ambiguous prefix matches contribute proportionally less
    #   to a country's share weight.  Rows ingested before hs_mapping_id was
    #   tracked (NULL) fall through at confidence 1.0 — backwards-compatible.
    confidence_expr = sqlfunc.coalesce(HsCodeMaterialMapping.confidence, 1.0)
    weighted_value = TradeFlow.trade_value_usd * confidence_expr

    rows = db.execute(
        select(
            TradeFlow.reporter_country,
            sqlfunc.sum(weighted_value).label("total"),
            sqlfunc.count(sqlfunc.distinct(TradeFlow.period)).label("n_periods"),
        )
        .outerjoin(
            HsCodeMaterialMapping,
            HsCodeMaterialMapping.id == TradeFlow.hs_mapping_id,
        )
        .where(
            TradeFlow.material_id == material_id,
            TradeFlow.import_export_flag == "export",
            TradeFlow.period.in_(periods_to_use),
            TradeFlow.reporter_country.in_(geo_codes),
            TradeFlow.trade_value_usd.is_not(None),
        )
        .group_by(TradeFlow.reporter_country)
    ).all()

    out: dict[str, float] = {}
    for row in rows:
        total = float(row.total or 0.0)
        n = int(row.n_periods or 0)
        if total > 0 and n > 0:
            # Average over the periods this reporter actually appears in.
            # If a reporter is missing some periods (e.g. Comtrade gap year),
            # we don't penalise them by dividing by the full window — that
            # would understate their typical trade.  Use n distinct periods
            # the reporter actually has data for.
            out[row.reporter_country] = total / n
    return out, periods_to_use


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
) -> tuple[dict[str, float], str, Optional[float], dict]:
    """
    Resolve a weight for every geography in geo_scores.

    Returns (weights, weight_source, total_trade_value_usd, trade_period_metadata).

    weight_source is one of:
        "trade_flow"       — TradeFlow export values (preferred)
        "production_share" — MaterialProductionShare fallback
        "equal"            — equal weight (1.0 each), last resort
        "mixed"            — some geos from trade, remaining from production share

    total_trade_value_usd is the sum of trade values used (None when not applicable).

    trade_period_metadata describes the periods averaged for the trade-flow path:
        {"granularity": "annual"|"monthly"|None, "periods_averaged": [...],
         "n_periods": int, "fallback_used": bool}
    Empty dict when no trade data was used.  Surfaced into rationale_json so
    consumers can verify which window of trade data drove the score.
    """
    geo_codes = [s.geography_code for s in geo_scores]
    trade, trade_period_metadata = _trade_weights(db, material_id, geo_codes)
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

    return weights, weight_source, total_trade_value, trade_period_metadata


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
    weights, weight_source, total_trade_value, trade_period_metadata = _resolve_weights(
        db, material_id, geo_scores
    )

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
        "trade_period_metadata": trade_period_metadata,    # G8 audit fix (2026-05-06)
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
        # Upsert: re-running global rollups on the same date should refresh
        # material_global_risk_scores rather than failing on the unique
        # constraint (material_id, as_of_date).
        upsert_vals = {
            "material_id": material_id,
            "as_of_date": as_of_date,
            "material_concentration_score": round(pillar_sums["material_concentration_score"], 2),
            "geopolitical_trade_score": round(pillar_sums["geopolitical_trade_score"], 2),
            "regulatory_compliance_score": round(pillar_sums["regulatory_compliance_score"], 2),
            "operational_score": round(pillar_sums["operational_score"], 2),
            "financial_pressure_score": round(pillar_sums["financial_pressure_score"], 2),
            "overall_risk_score": round(overall, 2),
            "trade_weighted_geo_count": trade_weighted_geo_count,
            "total_trade_value_usd": total_trade_value,
            "rationale_json": rationale,
            "scoring_version": ROLLUP_VERSION,
        }
        stmt = (
            pg_insert(MaterialGlobalRiskScore)
            .values(**upsert_vals)
            .on_conflict_do_update(
                constraint="uq_material_global_risk_score",
                set_={
                    "material_concentration_score": upsert_vals["material_concentration_score"],
                    "geopolitical_trade_score": upsert_vals["geopolitical_trade_score"],
                    "regulatory_compliance_score": upsert_vals["regulatory_compliance_score"],
                    "operational_score": upsert_vals["operational_score"],
                    "financial_pressure_score": upsert_vals["financial_pressure_score"],
                    "overall_risk_score": upsert_vals["overall_risk_score"],
                    "trade_weighted_geo_count": trade_weighted_geo_count,
                    "total_trade_value_usd": total_trade_value,
                    "rationale_json": rationale,
                    "scoring_version": ROLLUP_VERSION,
                },
            )
            .returning(MaterialGlobalRiskScore.id)
        )
        row_id = db.execute(stmt).scalar_one()
        score_row.id = row_id

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
