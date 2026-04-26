"""
Market-level (company-agnostic) evidence aggregation and scoring.

Computes risk scores at the material × geography intersection without requiring
any company data. This is the primary intelligence layer:

    material × geography  →  market_aggregator  →  MaterialGeographyRiskScore

Company overlay scoring (CompanyScore in orchestrator.py) builds ON TOP of this
layer by blending the customer's configured exposure profile with market scores.

Architecture relationship
--------------------------
evidence_query.py   — DB read layer (shared by both this module and orchestrator.py)
evidence_aggregator.py — company-anchored evidence → float inputs
market_aggregator.py   — market-anchored evidence → float inputs  ← THIS FILE
orchestrator.py        — company scoring entry point

Key differences from the company layer
---------------------------------------
* No CompanyMaterialExposure. Criticality comes from MaterialCriticalitySignal.
* Concentration is derived from the target geography's HCG status + the
  material's HHI score from the criticality signal.
* Financial pressure and supply-chain propagation are excluded — both require
  company-specific data. Pillar weights are renormalised across the four active
  pillars; see MARKET_PILLAR_WEIGHTS.
* ``score_material_geography()`` is the single public entry point for a
  specific (material, geography) pair.
* ``score_all_active_materials()`` batch-scores every active material against
  every geography with sufficient event coverage.

Transaction contract
---------------------
``score_material_geography()`` calls db.flush() but does NOT commit. The caller
owns the transaction, consistent with rescore_company() in orchestrator.py.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Optional

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.constants import RiskCategory
from app.models.criticality_signal import MaterialCriticalitySignal
from app.models.scoring import MaterialGeographyRiskScore
from app.models.supply import Material
from app.services.scoring import (
    financial_pressure as fp_module,
    geopolitical_risk,
    material_risk,
    regulatory_risk,
)
from app.services.scoring.decay import compute_recency_multiplier
from app.services.scoring.event_impact import compute_event_impact
from app.services.scoring.evidence_query import (
    EventWithRelevance,
    HIGH_CONCENTRATION_GEOS,
    get_events_for_geographies,
    get_events_for_material,
    get_events_for_materials,
    get_events_for_regulations,
)
from app.services.scoring.supplier_risk import SCORING_VERSION

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Market-level pillar weights
# Supply-chain propagation is excluded (requires a company graph). Financial
# pressure is KEPT but reframed with market-level inputs — commodity price
# volatility + producer stress events instead of company filing signals.
# Remaining five weights are renormalised from PILLAR_WEIGHTS:
#   material 0.25, geopolitical 0.20, regulatory 0.20, operational 0.10,
#   financial 0.10  →  sum = 0.85  →  renormalise each by /0.85
# ---------------------------------------------------------------------------
MARKET_PILLAR_WEIGHTS: dict[str, float] = {
    "material":      0.25 / 0.85,   # ≈ 0.294
    "geopolitical":  0.20 / 0.85,   # ≈ 0.235
    "regulatory":    0.20 / 0.85,   # ≈ 0.235
    "operational":   0.10 / 0.85,   # ≈ 0.118
    "financial":     0.10 / 0.85,   # ≈ 0.118
}

# Commodity price volatility thresholds for market financial pressure inputs.
# CV (coefficient of variation = std/mean) mapped to base_filing_signal (0-40).
_PRICE_CV_MAX = 0.50   # CV >= 0.50 → full signal (40)
# Price change threshold for directional stress signals.
_PRICE_SPIKE_PCT  = 0.20   # +20% in window → buyer leverage stress
_PRICE_CRASH_PCT  = 0.20   # -20% in window → producer liquidity stress
# Look-back window for price trend signals (days).
_PRICE_WINDOW_DAYS = 180

_MAX_EVENT_IMPACT = 1.56

# Source priority for MaterialCriticalitySignal — higher index = lower priority.
_CRITICALITY_SOURCE_PRIORITY: list[str] = [
    "eu_crma",
    "iea_report",
    "usgs_mcs",
    "manual",
    "patstat",
]


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _get_best_criticality_signal(
    db: Session,
    material_id: int,
    as_of_date: date,
) -> Optional[MaterialCriticalitySignal]:
    """Return the most authoritative MaterialCriticalitySignal for this material.

    Selects the latest reference_year row for the highest-priority source that
    has a non-null criticality_score. Falls back through the source hierarchy
    defined in ``_CRITICALITY_SOURCE_PRIORITY``.
    """
    stmt = (
        select(MaterialCriticalitySignal)
        .where(
            MaterialCriticalitySignal.material_id == material_id,
            MaterialCriticalitySignal.criticality_score.is_not(None),
            MaterialCriticalitySignal.reference_year <= as_of_date.year,
        )
        .order_by(MaterialCriticalitySignal.reference_year.desc())
    )
    rows = list(db.scalars(stmt).all())
    if not rows:
        return None

    # Select highest-priority source among rows
    for preferred_source in _CRITICALITY_SOURCE_PRIORITY:
        for row in rows:
            if row.source == preferred_source:
                return row

    # Fallback: return most recent row regardless of source
    return rows[0]


def _event_impact(
    ew: EventWithRelevance,
    category: RiskCategory,
    as_of_date: date,
) -> float:
    ev_date = ew.event.event_date.date() if ew.event.event_date else as_of_date
    recency = compute_recency_multiplier(category, ev_date, as_of_date)
    return compute_event_impact(
        severity=float(ew.event.severity_score or 0.5),
        confidence=float(ew.event.confidence_score or 0.5),
        recency_multiplier=recency,
        relevance_multiplier=ew.relevance_score,
    )


def _avg_impact_normalised(
    events: list[EventWithRelevance],
    category: RiskCategory,
    as_of_date: date,
) -> float:
    if not events:
        return 0.0
    impacts = [_event_impact(ew, category, as_of_date) for ew in events]
    return min(1.0, sum(impacts) / len(impacts) / _MAX_EVENT_IMPACT)


def _classify_geo_events(
    events: list[EventWithRelevance],
) -> tuple[list[EventWithRelevance], list[EventWithRelevance]]:
    """Split events into (export_restriction_events, tariff_events)."""
    export_events: list[EventWithRelevance] = []
    tariff_events: list[EventWithRelevance] = []
    for ew in events:
        subtype = (ew.event.metadata_json or {}).get("event_subtype", "")
        text = (ew.event.title or "").lower()
        if subtype == "EXPORT_RESTRICTION" or (
            "export" in text and ("restrict" in text or "ban" in text or "control" in text)
        ):
            export_events.append(ew)
        elif subtype in ("TARIFF", "TRADE_POLICY") or (
            "tariff" in text or "trade policy" in text or "section 301" in text
        ):
            tariff_events.append(ew)
    return export_events, tariff_events


def _dedup_events(*event_lists: list[EventWithRelevance]) -> list[EventWithRelevance]:
    by_id: dict[int, EventWithRelevance] = {}
    for lst in event_lists:
        for ew in lst:
            existing = by_id.get(ew.event.id)
            if existing is None or ew.relevance_score > existing.relevance_score:
                by_id[ew.event.id] = ew
    return list(by_id.values())


# ---------------------------------------------------------------------------
# Market-level input derivation functions
# ---------------------------------------------------------------------------

def _derive_market_material_inputs(
    criticality_signal: Optional[MaterialCriticalitySignal],
    geography_code: str,
    trade_events: list[EventWithRelevance],
    as_of_date: date,
) -> tuple[float, float, float]:
    """
    Returns (criticality, concentration, trade_volatility) each on [0, 1.0].

    criticality:
        Normalised criticality_score from the best available
        MaterialCriticalitySignal. Default 0.5 when no signal exists (data gap —
        caller should log a warning; don't assume zero risk).

    concentration:
        Composite of HHI from the criticality signal (supply concentration signal)
        + binary uplift when the target geography is a High-Concentration Geography
        (CN, CD, RU). Weighted 60% HHI / 40% HCG uplift.
        When HHI is absent, falls back to 0.5 baseline + 0.5 HCG uplift.

    trade_volatility:
        Average normalised event_impact for GEOPOLITICAL_TRADE events for this
        (material, geography) pair. Default 0.3 when no events.
    """
    # criticality
    if criticality_signal and criticality_signal.criticality_score is not None:
        criticality = float(criticality_signal.criticality_score)
    else:
        log.warning(
            "market_aggregator.no_criticality_signal",
            geography_code=geography_code,
        )
        criticality = 0.5

    # concentration
    is_hcg = geography_code in HIGH_CONCENTRATION_GEOS
    hcg_component = 1.0 if is_hcg else 0.0

    if criticality_signal and criticality_signal.hhi_score is not None:
        hhi = float(criticality_signal.hhi_score)
        concentration = 0.60 * hhi + 0.40 * hcg_component
    else:
        # No HHI data — use 0.5 baseline shifted by HCG status
        concentration = 0.5 + 0.5 * hcg_component * 0.5  # max 0.75 when HCG, no HHI
        concentration = min(1.0, concentration)

    # trade_volatility
    if not trade_events:
        trade_volatility = 0.3
    else:
        trade_volatility = _avg_impact_normalised(
            trade_events, RiskCategory.GEOPOLITICAL_TRADE, as_of_date
        )

    return criticality, concentration, trade_volatility


def _derive_market_geopolitical_inputs(
    geography_code: str,
    geo_trade_events: list[EventWithRelevance],
    as_of_date: date,
) -> tuple[float, float, float]:
    """
    Returns (country_concentration, export_restriction_exposure, tariff_exposure)
    each on [0, 1.0].

    country_concentration:
        1.0 for HCG countries (CN, CD, RU), 0.0 otherwise. At the single-
        geography level this is binary — no blending with facility data (no
        facilities at market level).

    export_restriction_exposure / tariff_exposure:
        Average normalised event_impact for events classified by subtype/keyword.
    """
    country_concentration = 1.0 if geography_code in HIGH_CONCENTRATION_GEOS else 0.0

    export_events, tariff_events = _classify_geo_events(geo_trade_events)
    export_exposure = _avg_impact_normalised(
        export_events, RiskCategory.GEOPOLITICAL_TRADE, as_of_date
    )
    tariff_exposure = _avg_impact_normalised(
        tariff_events, RiskCategory.GEOPOLITICAL_TRADE, as_of_date
    )

    return country_concentration, export_exposure, tariff_exposure


def _derive_market_regulatory_inputs(
    db: Session,
    material_id: int,
    geography_code: str,
    as_of_date: date,
) -> tuple[list[float], list[tuple[str, float]], float]:
    """
    Returns (top_event_impacts, scope_obligations, policy_proximity_adjustment).

    scope_obligations:
        Regulations linked via RegulationMaterialScope to this material OR via
        RegulationGeographyScope to this geography. All arrive at weight 0.50
        (unknown compliance status — no company to assess against).

    top_event_impacts:
        Normalised event_impact values for regulatory events scoped to this
        material + geography. Passed to regulatory_risk.score_regulatory_profile().

    policy_proximity_adjustment:
        1.15 if any event has an effective_date within 90 days of as_of_date.
    """
    from app.models.regulatory import (
        Regulation,
        RegulationGeographyScope,
        RegulationMaterialScope,
        RiskEventRegulation,
    )
    from datetime import datetime as _dt

    # Scope-derived regulations (material + geography)
    weights: dict[str, float] = {}

    mat_stmt = (
        select(Regulation.regulation_key)
        .join(RegulationMaterialScope, RegulationMaterialScope.regulation_id == Regulation.id)
        .where(RegulationMaterialScope.material_id == material_id)
    )
    for (key,) in db.execute(mat_stmt).all():
        weights[key] = 0.50

    geo_stmt = (
        select(Regulation.regulation_key)
        .join(RegulationGeographyScope, RegulationGeographyScope.regulation_id == Regulation.id)
        .where(RegulationGeographyScope.country_code == geography_code)
    )
    for (key,) in db.execute(geo_stmt).all():
        weights[key] = max(weights.get(key, 0.0), 0.50)

    scope_obligations = sorted(weights.items())

    # Events for scoped regulations
    reg_keys = set(weights.keys())
    reg_events = (
        get_events_for_regulations(db, reg_keys, as_of_date)
        if reg_keys
        else []
    )

    top_event_impacts: list[float] = []
    policy_proximity_adjustment = 1.0

    for ew in reg_events:
        effective_date: Optional[date] = None
        meta = ew.event.metadata_json or {}
        raw_eff = meta.get("effective_date")
        if raw_eff and isinstance(raw_eff, str):
            try:
                effective_date = _dt.fromisoformat(raw_eff).date()
            except ValueError:
                pass

        if effective_date is not None:
            days_to_effective = (effective_date - as_of_date).days
            if 0 <= days_to_effective <= 90:
                policy_proximity_adjustment = 1.15

        impact = _event_impact(ew, RiskCategory.REGULATORY_COMPLIANCE, as_of_date)
        top_event_impacts.append(impact)

    return top_event_impacts, scope_obligations, policy_proximity_adjustment


def _derive_market_operational_inputs(
    operational_events: list[EventWithRelevance],
    as_of_date: date,
) -> tuple[float, list[float]]:
    """
    Returns (structural_dependency, weighted_event_impacts).

    structural_dependency:
        At the market level there are no facility records to assess planned
        vs. operational capacity, so this defaults to 0.3 (the same
        conservative baseline used for companies with no facility data).
        Events with SINGLE_SOURCE or CAPACITY_CONSTRAINT subtypes override
        the baseline when present.

    weighted_event_impacts:
        event_impact for each operational event.
    """
    struct_events = [
        ew for ew in operational_events
        if (ew.event.metadata_json or {}).get("event_subtype", "") in (
            "SINGLE_SOURCE", "CAPACITY_CONSTRAINT"
        ) or any(
            kw in (ew.event.title or "").lower()
            for kw in ("single source", "single-source", "capacity constraint")
        )
    ]

    if struct_events:
        structural_dependency = sum(
            float(ew.event.severity_score or 0.5) for ew in struct_events
        ) / len(struct_events)
    else:
        structural_dependency = 0.3

    weighted_event_impacts = [
        _event_impact(ew, RiskCategory.OPERATIONAL, as_of_date)
        for ew in operational_events
    ]

    return structural_dependency, weighted_event_impacts


def _score_operational_market(
    struct_dep: float,
    op_impacts: list[float],
) -> float:
    """40% structural dependency + 60% weighted event rollup, capped at 100."""
    event_component = sum(op_impacts) / len(op_impacts) if op_impacts else 0.0
    raw = (0.40 * struct_dep + 0.60 * event_component) * 100
    return min(100.0, raw)


def _derive_market_financial_inputs(
    db: Session,
    material_id: int,
    as_of_date: date,
) -> tuple[float, float, float, int]:
    """
    Market-level financial pressure sub-inputs. Same tuple signature as
    ``derive_financial_inputs()`` so ``fp_module.score_financial_pressure()``
    can be called unchanged.

    Mapping (company layer → market reframe):
    ─────────────────────────────────────────────────────────────────────
    base_filing_signal (0-40)   ← commodity price VOLATILITY
        CV = std(prices) / mean(prices) over _PRICE_WINDOW_DAYS.
        Scaled: min(CV / _PRICE_CV_MAX, 1.0) × 40.
        Rationale: high price volatility signals supply-demand instability
        and financial uncertainty for both buyers and producers.

    leverage_warning_bonus (0-30) ← commodity price SPIKE
        If price rose > _PRICE_SPIKE_PCT over the window, buyers face
        increased procurement costs → financial stress proxy.
        Bonus = min(pct_change / _PRICE_SPIKE_PCT, 1.0) × 30 when positive.
        Also accumulates from FINANCIAL_PRESSURE events with "price_surge"
        or "market_squeeze" subtypes tagged to this material.

    liquidity_stress_bonus (0-30) ← commodity price CRASH + producer stress
        If price fell > _PRICE_CRASH_PCT, producers face margin pressure →
        potential supply cuts.
        Bonus = min(abs(pct_change) / _PRICE_CRASH_PCT, 1.0) × 30 when negative.
        Also accumulates from FINANCIAL_PRESSURE events with "producer_exit",
        "mine_closure", "bankruptcy" subtypes.

    filing_count:
        Number of price data points available in the window. When < 2,
        ``score_financial_pressure()`` applies its sparse-evidence cap —
        which cleanly handles the case where we have no price history yet.
    ─────────────────────────────────────────────────────────────────────
    Falls back gracefully when no price data exists: returns (0, 0, 0, 0)
    which scores to 0.0 rather than a false mid-point. Upstream rationale
    records this as a data gap, not a risk signal.
    """
    from datetime import timedelta

    from sqlalchemy import and_

    from app.models.supply import CommodityPrice

    window_start = as_of_date - timedelta(days=_PRICE_WINDOW_DAYS)

    price_stmt = (
        select(CommodityPrice.price_date, CommodityPrice.price_usd)
        .where(
            and_(
                CommodityPrice.material_id == material_id,
                CommodityPrice.price_date >= window_start,
                CommodityPrice.price_date <= as_of_date,
            )
        )
        .order_by(CommodityPrice.price_date.asc())
    )
    price_rows = db.execute(price_stmt).all()

    base_filing_signal: float = 0.0
    leverage_warning_bonus: float = 0.0
    liquidity_stress_bonus: float = 0.0
    filing_count = len(price_rows)

    if filing_count >= 2:
        prices = [float(row.price_usd) for row in price_rows]
        mean_price = sum(prices) / len(prices)

        if mean_price > 0:
            import statistics
            std_price = statistics.stdev(prices)
            cv = std_price / mean_price
            base_filing_signal = min(1.0, cv / _PRICE_CV_MAX) * 40.0

        # Directional trend: compare last price vs. first price
        pct_change = (prices[-1] - prices[0]) / prices[0] if prices[0] > 0 else 0.0

        if pct_change > 0:
            # Price spike → buyer leverage stress
            leverage_warning_bonus = min(1.0, pct_change / _PRICE_SPIKE_PCT) * 30.0
        else:
            # Price crash → producer liquidity stress
            liquidity_stress_bonus = min(1.0, abs(pct_change) / _PRICE_CRASH_PCT) * 30.0

    # Supplement with FINANCIAL_PRESSURE events tagged to this material
    fin_events = get_events_for_material(
        db, material_id, RiskCategory.FINANCIAL_PRESSURE, as_of_date
    )
    for ew in fin_events:
        subtype = (ew.event.metadata_json or {}).get("event_subtype", "")
        text = (ew.event.title or "").lower()
        severity = float(ew.event.severity_score or 0.5)

        if subtype in ("PRICE_SURGE", "MARKET_SQUEEZE") or (
            "price surge" in text or "market squeeze" in text
        ):
            leverage_warning_bonus = min(30.0, leverage_warning_bonus + severity * 10.0)
        elif subtype in ("PRODUCER_EXIT", "MINE_CLOSURE", "BANKRUPTCY") or (
            "producer exit" in text
            or "mine closure" in text
            or "mine shut" in text
            or "bankruptcy" in text
        ):
            liquidity_stress_bonus = min(30.0, liquidity_stress_bonus + severity * 10.0)
        else:
            # Generic financial pressure event — contributes to base signal
            base_filing_signal = min(40.0, base_filing_signal + severity * 5.0)

    # Bump filing_count to include events so the sparse-evidence cap reflects
    # the full evidence pool (prices + events).
    filing_count = filing_count + len(fin_events)

    log.debug(
        "market_aggregator.financial_inputs",
        material_id=material_id,
        price_points=len(price_rows),
        fin_events=len(fin_events),
        base_signal=round(base_filing_signal, 2),
        leverage_bonus=round(leverage_warning_bonus, 2),
        liquidity_bonus=round(liquidity_stress_bonus, 2),
    )
    return base_filing_signal, leverage_warning_bonus, liquidity_stress_bonus, filing_count


def _aggregate_market_score(
    mat_score: float,
    geo_score: float,
    reg_score: float,
    op_score: float,
    fin_score: float,
) -> float:
    """Weighted average of five active pillars using MARKET_PILLAR_WEIGHTS."""
    return (
        MARKET_PILLAR_WEIGHTS["material"]     * mat_score
        + MARKET_PILLAR_WEIGHTS["geopolitical"] * geo_score
        + MARKET_PILLAR_WEIGHTS["regulatory"]   * reg_score
        + MARKET_PILLAR_WEIGHTS["operational"]  * op_score
        + MARKET_PILLAR_WEIGHTS["financial"]    * fin_score
    )


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

def score_material_geography(
    db: Session,
    material_id: int,
    geography_code: str,
    as_of_date: Optional[date] = None,
    *,
    run_id: Optional[str] = None,
    persist: bool = True,
) -> MaterialGeographyRiskScore:
    """
    Compute (and optionally persist) a market risk score for a material × geography pair.

    Steps:
      1. Read MaterialCriticalitySignal for the material (best available source).
      2. Pull risk events: trade, operational, and regulatory, tagged to this
         material and/or geography via the junction tables.
      3. Derive four sets of pillar sub-inputs.
      4. Score each pillar using the existing scorer modules.
      5. Aggregate with MARKET_PILLAR_WEIGHTS (financial + propagation excluded).
      6. Persist to material_geography_risk_scores (upsert-safe via unique constraint).
         Skipped when ``persist=False``.

    Returns the MaterialGeographyRiskScore ORM object. ``id`` is populated
    after db.flush() when ``persist=True``; caller owns db.commit().

    This function is safe to call in batch (score_all_active_materials) and
    from individual API endpoints for on-demand scoring.
    """
    if as_of_date is None:
        as_of_date = date.today()
    if run_id is None:
        run_id = f"market-{uuid.uuid4()}"

    geography_code = geography_code.upper()

    log.info(
        "market_aggregator.score.start",
        material_id=material_id,
        geography_code=geography_code,
        as_of_date=as_of_date.isoformat(),
        run_id=run_id,
    )

    # --- STEP 1: Criticality signal ---
    criticality_signal = _get_best_criticality_signal(db, material_id, as_of_date)

    # --- STEP 2: Evidence ---
    # Trade events: tagged to this material OR this geography
    material_trade_events = get_events_for_material(
        db, material_id, RiskCategory.GEOPOLITICAL_TRADE, as_of_date
    )
    geo_trade_events = get_events_for_geographies(
        db, {geography_code}, RiskCategory.GEOPOLITICAL_TRADE, as_of_date
    )
    all_trade_events = _dedup_events(material_trade_events, geo_trade_events)

    # Operational events
    material_op_events = get_events_for_material(
        db, material_id, RiskCategory.OPERATIONAL, as_of_date
    )
    geo_op_events = get_events_for_geographies(
        db, {geography_code}, RiskCategory.OPERATIONAL, as_of_date
    )
    all_op_events = _dedup_events(material_op_events, geo_op_events)

    # Regulatory inputs derived inline (includes regulation query)
    top_reg_impacts, scope_obligations, prox_adj = _derive_market_regulatory_inputs(
        db, material_id, geography_code, as_of_date
    )

    # Financial pressure: reframed inputs from commodity prices + producer events
    base_sig, lev_bon, liq_bon, fin_count = _derive_market_financial_inputs(
        db, material_id, as_of_date
    )

    total_event_count = len({
        ew.event.id
        for lst in [all_trade_events, all_op_events]
        for ew in lst
    })

    # --- STEP 3: Derive sub-inputs ---
    crit, conc, trade_vol = _derive_market_material_inputs(
        criticality_signal, geography_code, all_trade_events, as_of_date
    )
    ctry_conc, exp_rest, tariff = _derive_market_geopolitical_inputs(
        geography_code, geo_trade_events, as_of_date
    )
    struct_dep, op_impacts = _derive_market_operational_inputs(all_op_events, as_of_date)

    # --- STEP 4: Score each pillar ---
    mat_score = material_risk.score_material_exposure(crit, conc, trade_vol)
    geo_score = geopolitical_risk.score_geopolitical_trade(ctry_conc, exp_rest, tariff)
    reg_score = regulatory_risk.score_regulatory_profile(
        top_reg_impacts, scope_obligations, prox_adj
    )
    op_score = _score_operational_market(struct_dep, op_impacts)
    fin_score = fp_module.score_financial_pressure(base_sig, lev_bon, liq_bon, fin_count)

    # --- STEP 5: Aggregate ---
    overall = _aggregate_market_score(mat_score, geo_score, reg_score, op_score, fin_score)

    # --- Build rationale ---
    rationale = {
        "run_id": run_id,
        "scoring_version": SCORING_VERSION,
        "as_of_date": as_of_date.isoformat(),
        "criticality_signal": {
            "source": criticality_signal.source if criticality_signal else None,
            "reference_year": criticality_signal.reference_year if criticality_signal else None,
            "criticality_score": (
                float(criticality_signal.criticality_score)
                if criticality_signal and criticality_signal.criticality_score is not None
                else None
            ),
            "hhi_score": (
                float(criticality_signal.hhi_score)
                if criticality_signal and criticality_signal.hhi_score is not None
                else None
            ),
        },
        "sub_inputs": {
            "material": {"criticality": crit, "concentration": conc, "trade_volatility": trade_vol},
            "geopolitical": {
                "country_concentration": ctry_conc,
                "export_restriction_exposure": exp_rest,
                "tariff_exposure": tariff,
            },
            "regulatory": {
                "top_event_count": len(top_reg_impacts),
                "scope_obligations": scope_obligations,
                "policy_proximity_adjustment": prox_adj,
            },
            "operational": {
                "structural_dependency": struct_dep,
                "event_impact_count": len(op_impacts),
            },
            "financial_pressure": {
                "note": "Reframed for market level: price volatility + directional trend + producer stress events",
                "base_filing_signal": base_sig,
                "leverage_warning_bonus": lev_bon,
                "liquidity_stress_bonus": liq_bon,
                "evidence_count": fin_count,
            },
        },
        "pillar_scores": {
            "material_concentration": mat_score,
            "geopolitical_trade": geo_score,
            "regulatory_compliance": reg_score,
            "operational": op_score,
            "financial_pressure": fin_score,
        },
        "weights_used": MARKET_PILLAR_WEIGHTS,
        "event_counts": {
            "trade_events": len(all_trade_events),
            "operational_events": len(all_op_events),
        },
        "notes": (
            f"Market score for {geography_code} (material_id={material_id}). "
            f"Overall {overall:.1f}. "
            f"Dominant: {max(('material', mat_score), ('geopolitical', geo_score), ('regulatory', reg_score), ('operational', op_score), ('financial', fin_score), key=lambda x: x[1])[0]} "
            f"({max(mat_score, geo_score, reg_score, op_score, fin_score):.1f}). "
            f"Financial pressure reframed: price volatility + directional trend + producer stress events. "
            f"Supply-chain propagation excluded (requires company graph). "
            f"Scoring version {SCORING_VERSION}."
        ),
    }

    score_row = MaterialGeographyRiskScore(
        material_id=material_id,
        geography_code=geography_code,
        as_of_date=as_of_date,
        material_concentration_score=mat_score,
        geopolitical_trade_score=geo_score,
        regulatory_compliance_score=reg_score,
        operational_score=op_score,
        financial_pressure_score=fin_score,
        overall_risk_score=overall,
        event_count=total_event_count,
        rationale_json=rationale,
        scoring_version=SCORING_VERSION,
    )

    if persist:
        db.add(score_row)
        db.flush()  # populates score_row.id — caller owns db.commit()

    log.info(
        "market_aggregator.score.done",
        material_id=material_id,
        geography_code=geography_code,
        overall=round(overall, 2),
        scoring_version=SCORING_VERSION,
        persisted=persist,
    )
    return score_row


def score_all_active_materials(
    db: Session,
    as_of_date: Optional[date] = None,
    *,
    geography_codes: Optional[list[str]] = None,
    min_event_count: int = 0,
    run_id: Optional[str] = None,
) -> list[MaterialGeographyRiskScore]:
    """
    Batch-score every active material against each supplied geography.

    If ``geography_codes`` is None, derives the geography list from the union of:
      - primary_producing_countries on active materials
      - country codes in risk_event_geographies for recent events

    This is the function a scheduled job (Inngest / Modal cron) should call.
    Each scored pair is committed individually to avoid one bad pair rolling back
    the entire batch — callers are expected to commit inside a loop or use
    ``db.commit()`` per row.

    ``min_event_count`` filters out (material, geography) pairs with fewer than
    N events — useful to avoid noise scores for very sparse pairs.

    ``run_id`` lets callers (e.g. the ``POST /market/rescore`` endpoint) supply
    a stable identifier so the response can be cross-referenced with the
    per-pair run ids stored in ``rationale_json["run_id"]`` (formatted as
    ``"{run_id}-{material.id}-{geo}"``). Defaults to a fresh ``batch-<uuid>``.

    Returns list of MaterialGeographyRiskScore objects (persisted, IDs populated).
    """
    if as_of_date is None:
        as_of_date = date.today()

    if run_id is None:
        run_id = f"batch-{uuid.uuid4()}"

    # Load active materials
    materials = list(db.scalars(select(Material)).all())
    log.info(
        "market_aggregator.batch.start",
        material_count=len(materials),
        as_of_date=as_of_date.isoformat(),
        run_id=run_id,
        geography_filter=geography_codes,
    )

    results: list[MaterialGeographyRiskScore] = []

    for material in materials:
        # Determine geographies for this material
        if geography_codes is not None:
            geos = [g.upper() for g in geography_codes]
        else:
            # Derive from material's primary_producing_countries seed data
            geos: list[str] = []
            if material.primary_producing_countries:
                geos = [g.upper() for g in material.primary_producing_countries]

            # Supplement with any geography that has events for this material
            from app.models.regulatory import RiskEventGeography, RiskEventMaterial
            event_geo_stmt = (
                select(RiskEventGeography.country_code)
                .join(
                    RiskEventMaterial,
                    RiskEventMaterial.risk_event_id == RiskEventGeography.risk_event_id,
                )
                .where(RiskEventMaterial.material_id == material.id)
                .distinct()
            )
            event_geos = [row[0] for row in db.execute(event_geo_stmt).all()]
            geos = list({*geos, *event_geos})

        if not geos:
            log.debug(
                "market_aggregator.batch.skip_no_geographies",
                material_id=material.id,
                material_name=material.canonical_name,
            )
            continue

        for geo in geos:
            try:
                score_row = score_material_geography(
                    db,
                    material.id,
                    geo,
                    as_of_date,
                    run_id=f"{run_id}-{material.id}-{geo}",
                    persist=True,
                )
                results.append(score_row)
                db.commit()
            except Exception:
                db.rollback()
                log.exception(
                    "market_aggregator.batch.error",
                    material_id=material.id,
                    geography_code=geo,
                )

    log.info(
        "market_aggregator.batch.done",
        scored=len(results),
        run_id=run_id,
    )
    return results


__all__ = [
    "MARKET_PILLAR_WEIGHTS",
    "score_material_geography",
    "score_all_active_materials",
]
