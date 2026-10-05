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
    HsCodeProductionShare,
    Material,
    MaterialProductionShare,
    TradeFlow,
)
from app.services.scoring.market_aggregator import MARKET_PILLAR_WEIGHTS
from app.services.scoring.supplier_risk import SCORING_VERSION

log = structlog.get_logger(__name__)

ROLLUP_VERSION = "1.2"  # 1.1: concentration=MAX across geos. 1.2 (2026-07-20): geopolitical production-weights fall through the stage ladder when ore is absent (by-products/synthetic)

# Pillar column names on MaterialGeographyRiskScore — used for generic weighted
# averaging so adding a sixth pillar later only requires touching this list.
_PILLAR_COLS = [
    "material_concentration_score",
    "geopolitical_trade_score",
    "regulatory_compliance_score",
    "operational_score",
    "financial_pressure_score",
]

# Pillars that roll up to the global view as the MAX across geographies
# rather than a trade-weighted average.  Concentration is a *structural*
# risk — "how dominated is this supply chain?" — and a weighted mean of
# per-geo concentration scores dilutes exactly the signal it should
# surface: the dominant chokepoint gets averaged against many low/zero-
# concentration trade-hub geos.  Max = the worst chokepoint, weight-
# independent.  Geopolitical is deliberately NOT here: its risk is tied to
# *where the material is produced*, so a zero-production but politically
# unstable transit geo (Syria/Belarus/Iran for cobalt) must not define it —
# that pillar needs production-weighting, not max.  (2026-07-18, Nicole)
_MAX_ROLLUP_PILLARS = {"material_concentration_score"}

# Pillars weighted by MINE-stage production share (where the material is
# produced) rather than by trade-flow export value.  Supply-origin risk —
# geopolitical exposure — must track the jurisdictions that actually produce
# the material: a zero-production but politically unstable transit/re-export
# geo (Syria/Belarus/Iran for cobalt) must not move the number.  Trade-value
# weighting also underweights raw-material chokepoints that export a low-value
# crude form (DRC ships cobalt hydroxide, not refined metal).  Weights come
# from hs_code_production_shares ore stage — the clean, stage-aware table —
# NOT the legacy stage-less material_production_shares (which carries
# refined/smelter numbers for copper/titanium/aluminium).  (2026-07-18)
_PRODUCTION_WEIGHTED_PILLARS = {"geopolitical_trade_score"}


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


_STAGE_LADDER_ORDER = (
    "ore", "concentrate", "intermediate", "refined", "battery_grade",
)


def _supply_origin_weights(
    db: Session,
    material_id: int,
) -> tuple[dict[str, float], Optional[str]]:
    """Production shares for supply-origin weighting, with stage-ladder fallback.

    Prefers the most UPSTREAM stage that has global share rows: ore first
    (mined materials), else concentrate/intermediate/refined/battery_grade in
    order.  By-product metals (gallium, tellurium, indium, bismuth...) and
    synthetic graphite have no ore stage - their production IS the downstream
    stage, and weighting by it is the honest supply-origin signal.  Without
    this fallback the geopolitical pillar silently reverted to trade-weighted
    averaging for exactly these materials (gallium: CN geo 58.9 diluted to a
    global 16.0 across ~100 trade geos).

    Returns ({country: share}, stage_used) - ({}, None) when the material has
    no global stage shares at all (callers keep the trade-weighted average).
    Within the chosen stage: latest reference_year only, max share per country
    (parent/child mapping dedupe).  _ore_production_weights below keeps its
    original ore-only 1.1 behaviour for any external callers.
    """
    rows = db.execute(
        select(
            HsCodeMaterialMapping.supply_chain_stage,
            HsCodeProductionShare.country_code,
            HsCodeProductionShare.production_share,
            HsCodeProductionShare.reference_year,
        )
        .join(HsCodeMaterialMapping,
              HsCodeMaterialMapping.id == HsCodeProductionShare.hs_mapping_id)
        .where(
            HsCodeMaterialMapping.material_id == material_id,
            HsCodeProductionShare.market_scope == "global",
            HsCodeProductionShare.production_share > 0,
        )
    ).all()
    if not rows:
        return {}, None
    by_stage: dict[str, list] = {}
    for stage, cc, share, yr in rows:
        by_stage.setdefault(stage, []).append((cc, share, yr))
    stage_used = next(
        (st for st in _STAGE_LADDER_ORDER if st in by_stage), None
    )
    if stage_used is None:
        return {}, None
    stage_rows = by_stage[stage_used]
    latest = max(yr for _, _, yr in stage_rows)
    out: dict[str, float] = {}
    for cc, share, yr in stage_rows:
        if yr != latest or share is None:
            continue
        v = float(share)
        if cc not in out or v > out[cc]:
            out[cc] = v
    return out, stage_used


def _ore_production_weights(
    db: Session,
    material_id: int,
) -> dict[str, float]:
    """Mine-stage production shares from ``hs_code_production_shares`` keyed by
    country — the clean, stage-aware source for supply-origin weighting.

    Reads the ``ore`` stage at the latest reference year, ``market_scope=
    'global'``.  Parent/child HS duplicates (e.g. 2603 and 260300 both mapped
    to copper ore) and the usgs_mcs / usgs_mcs_propagated pair express the same
    underlying production, so they are de-duplicated by taking the MAX share
    per country (summing would double count).  Preferred over
    ``MaterialProductionShare``, which is stage-less and holds refined/smelter
    shares for a few materials (copper CN 33.7%% vs true mine CL 26.5%%).
    Returns {} when the material has no ore-stage global shares — callers then
    keep the trade-weighted average.
    """
    base = (
        select(HsCodeProductionShare.country_code, HsCodeProductionShare.production_share,
               HsCodeProductionShare.reference_year)
        .join(HsCodeMaterialMapping,
              HsCodeMaterialMapping.id == HsCodeProductionShare.hs_mapping_id)
        .where(
            HsCodeMaterialMapping.material_id == material_id,
            HsCodeMaterialMapping.supply_chain_stage == "ore",
            HsCodeProductionShare.market_scope == "global",
        )
    )
    latest_year = db.scalar(
        select(sqlfunc.max(HsCodeProductionShare.reference_year))
        .join(HsCodeMaterialMapping,
              HsCodeMaterialMapping.id == HsCodeProductionShare.hs_mapping_id)
        .where(
            HsCodeMaterialMapping.material_id == material_id,
            HsCodeMaterialMapping.supply_chain_stage == "ore",
            HsCodeProductionShare.market_scope == "global",
        )
    )
    if latest_year is None:
        return {}
    rows = db.execute(
        base.where(HsCodeProductionShare.reference_year == latest_year)
    ).all()
    out: dict[str, float] = {}
    for cc, share, _yr in rows:
        if share is None:
            continue
        v = float(share)
        if cc not in out or v > out[cc]:
            out[cc] = v
    return out


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


# ---------------------------------------------------------------------------
# Per-pillar weighted-average math (F-GR-1, 2026-06-10)
# ---------------------------------------------------------------------------

# F-GR-6 (2026-06-11): pillar scorers in market_aggregator always return a
# number (0.0 when no inputs are present), so non-None doesn't actually
# distinguish "scored and found nothing" from "no data to score against."
# The "meaningful signal" threshold lets the publishability gate
# distinguish "the world doesn't regulate this mineral that way" (small
# fraction of geos with non-zero) from "we have real coverage across
# producer geos" (most geos with non-zero on the signal-driven pillars).
MEANINGFUL_SIGNAL_THRESHOLD = 5.0   # out of 100


def compute_per_pillar_weighted_average(
    per_geo_pillars: list[tuple[str, float, dict[str, Optional[float]]]],
    pillar_cols: list[str],
) -> tuple[
    dict[str, Optional[float]],
    dict[str, float],
    dict[str, int],
    dict[str, int],   # F-GR-6: meaningful-signal geo counts
]:
    """Weighted average per pillar, normalised by contributing weights only.

    Each pillar is normalised by Σ(weights of geos that actually had a
    value for that pillar), not by the global weight sum.  This prevents
    silent under-weighting when a pillar is NULL for some geographies
    (common: regulatory NULL for non-SEC-jurisdictions, financial NULL
    for non-public-company regions).

    Args
    ----
    per_geo_pillars
        List of (geo_code, weight, {pillar_col: value_or_None}) tuples.
        Weight ≤ 0 entries are ignored.
    pillar_cols
        Ordered list of pillar column names (kept stable so callers can
        reason about which pillars are missing from the result).

    Returns
    -------
    (pillar_values, pillar_contributing_weight, pillar_contributing_geos,
     pillar_meaningful_geos)
        - pillar_values[col]: weighted average, or None if no geo had data
        - pillar_contributing_weight[col]: Σ weights that contributed
        - pillar_contributing_geos[col]: count of geos that contributed
        - pillar_meaningful_geos[col]: count of geos whose value was
          ≥ MEANINGFUL_SIGNAL_THRESHOLD (signals real coverage, not just
          a scorer return value of 0.0)
    """
    pillar_sums = {col: 0.0 for col in pillar_cols}
    pillar_contributing_weight = {col: 0.0 for col in pillar_cols}
    pillar_contributing_geos = {col: 0 for col in pillar_cols}
    pillar_meaningful_geos = {col: 0 for col in pillar_cols}

    for _geo, w, pillar_map in per_geo_pillars:
        if w <= 0:
            continue
        for col in pillar_cols:
            v = pillar_map.get(col)
            if v is not None:
                pillar_sums[col] += w * float(v)
                pillar_contributing_weight[col] += w
                pillar_contributing_geos[col] += 1
                if float(v) >= MEANINGFUL_SIGNAL_THRESHOLD:
                    pillar_meaningful_geos[col] += 1

    pillar_values: dict[str, Optional[float]] = {}
    for col in pillar_cols:
        if pillar_contributing_weight[col] > 0:
            pillar_values[col] = pillar_sums[col] / pillar_contributing_weight[col]
        else:
            pillar_values[col] = None
    return (
        pillar_values,
        pillar_contributing_weight,
        pillar_contributing_geos,
        pillar_meaningful_geos,
    )


def compute_production_weighted_pillar(
    ore_weights: dict[str, float],
    per_geo_value: dict[str, Optional[float]],
) -> Optional[float]:
    """Production-share-weighted mean of one pillar over PRODUCING geos.

    ``ore_weights``   : {country_code: mine-stage share} (need not sum to 1).
    ``per_geo_value`` : {country_code: pillar_value_or_None}.

    Only geos present in ``ore_weights`` with a positive weight AND a non-None
    pillar value contribute.  A politically unstable geo that produces nothing
    is absent from ``ore_weights`` and therefore contributes nothing.  Returns
    None when no producer contributed, so the caller keeps whatever value it
    already had (the trade-weighted average).
    """
    num = den = 0.0
    for cc, w in ore_weights.items():
        if w <= 0:
            continue
        v = per_geo_value.get(cc)
        if v is None:
            continue
        num += w * float(v)
        den += w
    return (num / den) if den > 0 else None


def compute_meaningful_pillar_count(
    pillar_meaningful_geos: dict[str, int],
    pillar_contributing_geos: dict[str, int],
    min_meaningful_fraction: float = 0.30,
) -> int:
    """Count pillars with "real" coverage signal (F-GR-6, 2026-06-11).

    A pillar counts as meaningful when at least ``min_meaningful_fraction``
    of its contributing geos had a value above MEANINGFUL_SIGNAL_THRESHOLD.

    This distinguishes pillars where the scoring chain is actually
    producing differentiated output (REE Reg should be high in many geos
    given EU CRMA / Section 232 / BIS export controls) from pillars where
    every geo got the same baseline number because the chain didn't
    surface mineral-specific events (REE actually shows Reg≥5 for only
    5/120 geos in the real data — a coverage gap, not low-risk signal).
    """
    n = 0
    for col, meaningful in pillar_meaningful_geos.items():
        contributing = pillar_contributing_geos.get(col, 0)
        if contributing <= 0:
            continue
        if (meaningful / contributing) >= min_meaningful_fraction:
            n += 1
    return n


def compute_overall_from_pillars(
    pillar_values: dict[str, Optional[float]],
    pillar_weights: dict[str, float],
) -> Optional[float]:
    """Combine pillar values via MARKET_PILLAR_WEIGHTS, rescaled.

    Missing pillars (None) are dropped and their weight share is
    redistributed proportionally to the remaining pillars.  Returns None
    if no pillars have data.
    """
    contributing_weight_sum = sum(
        w for col, w in pillar_weights.items() if pillar_values.get(col) is not None
    )
    if contributing_weight_sum <= 0:
        return None
    return sum(
        (pillar_weights[col] / contributing_weight_sum) * pillar_values[col]
        for col in pillar_weights
        if pillar_values.get(col) is not None
    )


def compute_data_quality_score(
    pillar_contributing_geos: dict[str, int],
    n_geos_total: int,
    pillar_weights: dict[str, float],
) -> float:
    """Fraction of (geo × pillar) cells populated, pillar-weight-weighted.

    A material with all five pillars populated for all geos = 1.0.
    Used by the publishability gate.
    """
    if n_geos_total <= 0:
        return 0.0
    weighted_filled = sum(
        pillar_weights[col] * pillar_contributing_geos.get(col, 0)
        for col in pillar_weights
    )
    weighted_total = sum(pillar_weights[col] for col in pillar_weights) * n_geos_total
    return weighted_filled / weighted_total if weighted_total > 0 else 0.0


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
            # F-GR-3 fix (2026-06-10): in a mixed run where some geos have
            # real trade weights (millions of USD), the equal weight of 1.0
            # normalises to ~0 and the geo is effectively dropped.  The
            # rationale_json now exposes which geos this affected so the
            # content site can surface the gap rather than silently hiding it.
            log.warning(
                "global_rollup.geography_no_weight_data",
                material_id=material_id,
                geography_code=geo,
                note=(
                    "No TradeFlow or ProductionShare data — assigned weight=1.0 "
                    "but will be silently dropped in any mixed rollup; see "
                    "rationale_json.weight_source_breakdown.dropped_geos"
                ),
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

    # Step 3: Per-pillar weighted average via pure helper.
    #
    # F-GR-1 fix (2026-06-10): each pillar is normalised by the sum of
    # weights that CONTRIBUTED to that pillar, not by the global weight
    # sum.  Pre-fix, sparse pillars (regulatory often NULL outside SEC-
    # equivalents, financial often NULL for non-public-co regions) were
    # systematically under-weighted because their NULL contributions
    # consumed `w_norm` without adding to the numerator.  See
    # `compute_per_pillar_weighted_average` for the corrected math.
    per_geo_pillars: list[tuple[str, float, dict[str, Optional[float]]]] = []
    geo_detail: list[dict] = []
    geos_with_any_data: set[str] = set()

    for gs in geo_scores:
        w = weights.get(gs.geography_code, 0.0)
        if w <= 0:
            continue
        w_norm = w / weight_sum   # for geo_detail breakdown only
        pillar_map: dict[str, Optional[float]] = {
            col: (float(getattr(gs, col)) if getattr(gs, col) is not None else None)
            for col in _PILLAR_COLS
        }
        per_geo_pillars.append((gs.geography_code, w, pillar_map))
        if any(v is not None for v in pillar_map.values()):
            geos_with_any_data.add(gs.geography_code)
        geo_detail.append({
            "geography_code":   gs.geography_code,
            "weight":           round(w, 4),
            "weight_normalised": round(w_norm, 4),
            "geo_score_date":   gs.as_of_date.isoformat(),
            "pillars":          pillar_map,
            "overall":          gs.overall_risk_score,
        })

    (
        pillar_values,
        pillar_contributing_weight,
        pillar_contributing_geos,
        pillar_meaningful_geos,   # F-GR-6
    ) = compute_per_pillar_weighted_average(per_geo_pillars, _PILLAR_COLS)

    # Track which operator actually produced each pillar's global value, so
    # the rationale reflects what happened (a production-weighted pillar that
    # fell back to trade-weighting is labelled honestly).
    pillar_operators = {col: "trade_weighted_avg" for col in _PILLAR_COLS}

    # Override A — MAX-rollup pillars (see _MAX_ROLLUP_PILLARS) take the worst
    # chokepoint across geographies instead of the trade-weighted average.
    # Weight-independent and includes every geo that has a value (a producer
    # that does not export is still a concentration chokepoint); geos whose
    # value is 0.0 simply do not affect the max.
    for _mcol in _MAX_ROLLUP_PILLARS:
        _mvals = [
            float(getattr(gs, _mcol))
            for gs in geo_scores
            if getattr(gs, _mcol) is not None
        ]
        pillar_values[_mcol] = max(_mvals) if _mvals else None
        pillar_operators[_mcol] = "max"

    # Override B — production-weighted pillars (see _PRODUCTION_WEIGHTED_PILLARS)
    # are weighted by mine-stage production share instead of trade value, so
    # supply-origin risk tracks producing jurisdictions.  Non-producers carry
    # no weight and drop out.  Falls back to the trade-weighted average already
    # in pillar_values when the material has no ore-stage shares.
    _ore_weights, _origin_stage = _supply_origin_weights(db, material_id)
    if _ore_weights:
        for _pcol in _PRODUCTION_WEIGHTED_PILLARS:
            _val_by_code = {
                gs.geography_code: (
                    float(getattr(gs, _pcol))
                    if getattr(gs, _pcol) is not None else None
                )
                for gs in geo_scores
            }
            _pw = compute_production_weighted_pillar(_ore_weights, _val_by_code)
            if _pw is not None:
                pillar_values[_pcol] = _pw
                pillar_operators[_pcol] = "production_weighted_avg"

    # Step 4: Overall score using MARKET_PILLAR_WEIGHTS, rescaled across
    # pillars that have data.  If material_concentration is None, its
    # weight is redistributed proportionally to the remaining pillars.
    _pillar_to_weight = {
        "material_concentration_score": MARKET_PILLAR_WEIGHTS["material"],
        "geopolitical_trade_score":     MARKET_PILLAR_WEIGHTS["geopolitical"],
        "regulatory_compliance_score":  MARKET_PILLAR_WEIGHTS["regulatory"],
        "operational_score":            MARKET_PILLAR_WEIGHTS["operational"],
        "financial_pressure_score":     MARKET_PILLAR_WEIGHTS["financial"],
    }
    overall = compute_overall_from_pillars(pillar_values, _pillar_to_weight)

    # Step 5: Weight-source breakdown for diagnostic clarity (F-GR-4 fix).
    # Previously `trade_weighted_geo_count` counted every nonzero weight
    # regardless of source — including production-share and equal-weight
    # fallbacks.  Now broken out so the content site can show "X geographies
    # weighted by trade flow, Y by production share, Z dropped (no data)".
    n_trade_weighted = 0
    n_production_share_weighted = 0
    n_equal_weighted = 0
    dropped_geos: list[str] = []
    # Re-resolve which source each geo came from by re-querying the helpers.
    # Lightweight — both queries are already cached at the SQL plan level
    # within a single rollup call.
    _trade_w, _ = _trade_weights(db, material_id, [g.geography_code for g in geo_scores])
    _prod_w = _production_share_weights(db, material_id, [g.geography_code for g in geo_scores])
    for gs in geo_scores:
        g = gs.geography_code
        if g in _trade_w:
            n_trade_weighted += 1
        elif g in _prod_w:
            n_production_share_weighted += 1
        else:
            # Fell to equal-weight (=1.0) — but in mixed runs the normalised
            # share is effectively zero, so this geo is "silently dropped".
            n_equal_weighted += 1
            # Flag the drop only when the geo's normalised contribution is
            # below 1e-4 (i.e. some other geo's trade value dominated).
            w = weights.get(g, 0.0)
            if weight_sum > 0 and (w / weight_sum) < 1e-4:
                dropped_geos.append(g)

    # Back-compat: keep trade_weighted_geo_count meaning "geos with nonzero
    # weight" so the existing column behaviour doesn't change for callers
    # that already read it.  New, narrower counters live in rationale_json.
    trade_weighted_geo_count = sum(1 for w in weights.values() if w > 0)

    # ── Publishability gate (F-GR-2 fix, 2026-06-10; re-tuned 2026-06-10) ────
    # Distinguishes "1-geo material" from "30-geo material" for the
    # public content site.  Stored in rationale_json (no schema migration
    # required); the content-site API can read these fields when deciding
    # which materials to surface on public pages.
    #
    # Threshold history:
    #   2026-06-10 (initial):  ≥3 geos AND data_quality ≥ 0.40.
    #                          Simulation against launch-list 10 minerals
    #                          showed 5 of 10 publishable — the math was
    #                          conflating "no events because the world
    #                          doesn't regulate this mineral that way"
    #                          (true zero) with "no data because we haven't
    #                          ingested enough" (coverage gap).  Copper
    #                          scored 17 producer geos but failed because
    #                          there is no Copper-specific UFLPA/CRMA regime.
    #
    #   2026-06-10 (current):  ≥3 geos AND data_quality ≥ 0.30 AND ≥3 of 5
    #                          pillars populated.  Relaxes the weighted-score
    #                          constraint (was punishing the Material-pillar-
    #                          heavy weighting unfairly) while adding an
    #                          explicit breadth constraint so a single-pillar
    #                          material doesn't sneak through.
    #
    # data_quality_score in [0, 1]:
    #     Fraction of (geo × pillar) cells populated, weighted by each
    #     pillar's MARKET_PILLAR_WEIGHTS.  A material with all five
    #     pillars populated for all geos = 1.0.  Missing pillars reduce
    #     the score proportionally.
    n_geographies_with_data = len(geos_with_any_data)
    n_geos_total = len(geo_scores)
    data_quality_score = compute_data_quality_score(
        pillar_contributing_geos, n_geos_total, _pillar_to_weight,
    )
    n_pillars_populated = sum(
        1 for col in _PILLAR_COLS if pillar_contributing_geos.get(col, 0) > 0
    )
    # F-GR-6 (2026-06-11): count pillars where ≥30% of contributing geos
    # had a meaningful (≥5/100) value.  This is the publishability-relevant
    # counter because pillar scorers always return 0.0 (never None) when
    # no signal exists, so n_pillars_populated is trivially 5 for every
    # material and doesn't filter anything.
    n_pillars_with_meaningful_signal = compute_meaningful_pillar_count(
        pillar_meaningful_geos, pillar_contributing_geos,
    )

    PUBLISHABILITY_MIN_GEOS = 3
    PUBLISHABILITY_MIN_QUALITY = 0.30
    PUBLISHABILITY_MIN_PILLARS = 3  # F-GR-6: pillars with meaningful signal
    is_publishable = (
        n_geographies_with_data >= PUBLISHABILITY_MIN_GEOS
        and data_quality_score >= PUBLISHABILITY_MIN_QUALITY
        and n_pillars_with_meaningful_signal >= PUBLISHABILITY_MIN_PILLARS
        and overall is not None
    )
    if not is_publishable:
        reasons = []
        if n_geographies_with_data < PUBLISHABILITY_MIN_GEOS:
            reasons.append(
                f"only {n_geographies_with_data} geo(s) with data "
                f"(threshold ≥{PUBLISHABILITY_MIN_GEOS})"
            )
        if data_quality_score < PUBLISHABILITY_MIN_QUALITY:
            reasons.append(
                f"data_quality {data_quality_score:.2f} "
                f"(threshold ≥{PUBLISHABILITY_MIN_QUALITY:.2f})"
            )
        if n_pillars_with_meaningful_signal < PUBLISHABILITY_MIN_PILLARS:
            reasons.append(
                f"only {n_pillars_with_meaningful_signal} pillar(s) with "
                f"meaningful signal (threshold ≥{PUBLISHABILITY_MIN_PILLARS} of 5; "
                f"meaningful = ≥30% of geos with value ≥{MEANINGFUL_SIGNAL_THRESHOLD:.0f}/100)"
            )
        if overall is None:
            reasons.append("no pillars populated — overall is NULL")
        publishability_reason = "; ".join(reasons)
    else:
        publishability_reason = (
            f"meets minimum thresholds (geos={n_geographies_with_data}, "
            f"quality={data_quality_score:.2f}, "
            f"meaningful_pillars={n_pillars_with_meaningful_signal}/5)"
        )

    # ── Staleness diagnostic (F-GR-5 fix) ────────────────────────────
    geo_score_dates = [gs.as_of_date for gs in geo_scores]
    oldest_geo_score_date = min(geo_score_dates) if geo_score_dates else None
    newest_geo_score_date = max(geo_score_dates) if geo_score_dates else None
    stale_geo_threshold_days = 365
    n_stale_geos = (
        sum(
            1 for d in geo_score_dates
            if (as_of_date - d).days > stale_geo_threshold_days
        ) if geo_score_dates else 0
    )

    rationale = {
        "run_id": run_id,
        "scoring_version": ROLLUP_VERSION,
        "as_of_date": as_of_date.isoformat(),
        "weight_source": weight_source,
        "total_trade_value_usd": total_trade_value,
        "trade_period_metadata": trade_period_metadata,    # G8 audit fix (2026-05-06)
        "geography_count": len(geo_scores),
        # F-GR-1 + F-GR-2 additions
        "pillar_weighted_averages": {
            col: (round(v, 2) if v is not None else None)
            for col, v in pillar_values.items()
        },
        # Which operator produced each pillar's global value (2026-07-18).
        "pillar_rollup_operators": dict(pillar_operators),
        # 1.2: which stage's shares weighted the production-weighted pillars
        # (ore for mined materials; refined/battery_grade for by-products and
        # synthetic graphite; None = no shares -> trade-weighted fallback).
        "production_weight_stage": _origin_stage,
        "pillar_contributing_geos": dict(pillar_contributing_geos),  # how many geos populated each pillar
        "data_quality": {
            "n_geographies_total":              n_geos_total,
            "n_geographies_with_data":          n_geographies_with_data,
            "n_pillars_populated":              n_pillars_populated,           # any non-None
            "n_pillars_with_meaningful_signal": n_pillars_with_meaningful_signal,  # F-GR-6
            "pillar_meaningful_geos":           dict(pillar_meaningful_geos),  # per-pillar counts
            "meaningful_signal_threshold":      MEANINGFUL_SIGNAL_THRESHOLD,
            "data_quality_score":               round(data_quality_score, 3),
            "is_publishable":                   is_publishable,
            "publishability_reason":            publishability_reason,
            "thresholds": {
                "min_geos":          PUBLISHABILITY_MIN_GEOS,
                "min_quality":       PUBLISHABILITY_MIN_QUALITY,
                "min_pillars":       PUBLISHABILITY_MIN_PILLARS,
            },
        },
        # F-GR-3 + F-GR-4 additions
        "weight_source_breakdown": {
            "n_trade_weighted":            n_trade_weighted,
            "n_production_share_weighted": n_production_share_weighted,
            "n_equal_weighted":            n_equal_weighted,
            "dropped_geos":                dropped_geos,
        },
        # F-GR-5 additions
        "staleness": {
            "oldest_geo_score_date": oldest_geo_score_date.isoformat() if oldest_geo_score_date else None,
            "newest_geo_score_date": newest_geo_score_date.isoformat() if newest_geo_score_date else None,
            "n_stale_geos":          n_stale_geos,
            "stale_threshold_days":  stale_geo_threshold_days,
        },
        "geographies": geo_detail,
        "notes": (
            f"Rollup across {len(geo_scores)} geographies "
            f"(concentration=max, geopolitical=production-weighted, "
            f"others trade-weighted; weight_source={weight_source}). "
            f"Overall {overall:.1f}." if overall is not None else
            f"Rollup across {len(geo_scores)} geographies produced no overall "
            f"score — no pillars had data."
        ),
    }

    if n_stale_geos > 0:
        log.warning(
            "global_rollup.stale_geo_scores",
            material_id=material_id,
            n_stale_geos=n_stale_geos,
            threshold_days=stale_geo_threshold_days,
            note="Some per-geography scores are >365 days old; consider refresh",
        )

    # Helper: rounded pillar value or None pass-through
    def _r(v: Optional[float]) -> Optional[float]:
        return round(v, 2) if v is not None else None

    score_row = MaterialGlobalRiskScore(
        material_id=material_id,
        as_of_date=as_of_date,
        material_concentration_score=_r(pillar_values["material_concentration_score"]),
        geopolitical_trade_score=_r(pillar_values["geopolitical_trade_score"]),
        regulatory_compliance_score=_r(pillar_values["regulatory_compliance_score"]),
        operational_score=_r(pillar_values["operational_score"]),
        financial_pressure_score=_r(pillar_values["financial_pressure_score"]),
        overall_risk_score=_r(overall),
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
            "material_concentration_score": _r(pillar_values["material_concentration_score"]),
            "geopolitical_trade_score":     _r(pillar_values["geopolitical_trade_score"]),
            "regulatory_compliance_score":  _r(pillar_values["regulatory_compliance_score"]),
            "operational_score":            _r(pillar_values["operational_score"]),
            "financial_pressure_score":     _r(pillar_values["financial_pressure_score"]),
            "overall_risk_score":           _r(overall),
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
                    "geopolitical_trade_score":     upsert_vals["geopolitical_trade_score"],
                    "regulatory_compliance_score":  upsert_vals["regulatory_compliance_score"],
                    "operational_score":            upsert_vals["operational_score"],
                    "financial_pressure_score":     upsert_vals["financial_pressure_score"],
                    "overall_risk_score":           upsert_vals["overall_risk_score"],
                    "trade_weighted_geo_count":     trade_weighted_geo_count,
                    "total_trade_value_usd":        total_trade_value,
                    "rationale_json":               rationale,
                    "scoring_version":              ROLLUP_VERSION,
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

    # 2026-06-12: Batch commits with per-material savepoints + defensive
    # connection-drop recovery — same pattern as hs_node_scorer and
    # market_aggregator.  See hs_node_scorer.score_all_hs_nodes for the
    # full rationale.  Global rollup has only ~50-75 materials so the
    # speedup is smaller (~5-15 min saved on cold Neon), but the recovery
    # logic is needed because one connection drop mid-rollup would
    # otherwise stall the loop.
    from sqlalchemy.exc import (
        DBAPIError,
        OperationalError,
        PendingRollbackError,
        InvalidRequestError,
    )
    _CONN_ERRORS: tuple = (
        DBAPIError, OperationalError, PendingRollbackError, InvalidRequestError,
    )

    _COMMIT_BATCH_SIZE = 10
    results: list[MaterialGlobalRiskScore] = []
    for material_id in material_ids:
        try:
            savepoint = db.begin_nested()
        except _CONN_ERRORS as e:
            log.warning(
                "global_rollup.batch.connection_reset",
                reason=f"begin_nested failed: {type(e).__name__}",
            )
            try:
                db.rollback()
            except Exception:
                pass
            continue

        try:
            score_row = score_material_global_rollup(
                db,
                material_id,
                as_of_date,
                run_id=f"{run_id}-{material_id}",
                persist=True,
            )
            results.append(score_row)
            savepoint.commit()
        except _CONN_ERRORS as e:
            log.warning(
                "global_rollup.batch.connection_reset",
                reason=f"score call failed: {type(e).__name__}",
                material_id=material_id,
            )
            try:
                db.rollback()
            except Exception:
                pass
            continue
        except Exception:
            try:
                savepoint.rollback()
            except Exception:
                try:
                    db.rollback()
                except Exception:
                    pass
            log.exception(
                "global_rollup.batch.error",
                material_id=material_id,
            )

        if (len(results) % _COMMIT_BATCH_SIZE) == 0 and len(results) > 0:
            try:
                db.commit()
            except _CONN_ERRORS as e:
                log.warning(
                    "global_rollup.batch.commit_failed",
                    reason=type(e).__name__,
                )
                try:
                    db.rollback()
                except Exception:
                    pass

    # Flush the trailing partial batch.
    try:
        db.commit()
    except Exception:
        log.exception("global_rollup.batch.final_commit_failed")
        try:
            db.rollback()
        except Exception:
            pass

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
