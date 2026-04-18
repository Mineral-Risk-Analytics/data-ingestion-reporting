"""
Evidence aggregation layer: convert EventWithRelevance records into the float
inputs each component scoring function expects.

All outputs are on the 0-1.0 scale unless the function explicitly uses a
different range (financial_pressure sub-components use 0-40/0-30/0-30).

No DB reads live here — all inputs arrive as pre-fetched ORM objects from
evidence_query.py.  No scoring results are persisted here — that is the
orchestrator's job.
"""

from __future__ import annotations

import structlog
from datetime import date
from typing import Optional

from app.models.company import CompanyMaterialExposure
from app.services.scoring.decay import RiskCategory, compute_recency_multiplier
from app.services.scoring.event_impact import compute_event_impact
from app.services.scoring.evidence_query import EventWithRelevance

log = structlog.get_logger(__name__)

# Geographies considered high-concentration for EV battery supply chains.
_HIGH_CONCENTRATION_GEOS = frozenset({"CN", "CD", "RU"})

# Maximum theoretical event_impact (1.0 × 1.0 × 1.20 recency × 1.30 relevance).
# Used to normalise averaged impact values back to 0-1.0.
_MAX_EVENT_IMPACT = 1.56


def _event_date_as_date(ew: EventWithRelevance, fallback: date) -> date:
    """Extract the event's date as a plain date, defaulting to fallback if None."""
    if ew.event.event_date is None:
        return fallback
    return ew.event.event_date.date()


def _safe_severity(ew: EventWithRelevance) -> float:
    """Return severity_score, defaulting to 0.5 if missing."""
    v = ew.event.severity_score
    if v is None:
        log.warning("evidence_aggregator.missing_severity", event_id=ew.event.id)
        return 0.5
    return float(v)


def _safe_confidence(ew: EventWithRelevance) -> float:
    """Return confidence_score, defaulting to 0.5 if missing."""
    v = ew.event.confidence_score
    if v is None:
        log.warning("evidence_aggregator.missing_confidence", event_id=ew.event.id)
        return 0.5
    return float(v)


def _normalise_exposure_score(raw: float) -> float:
    """
    Normalise CompanyMaterialExposure.exposure_score to [0, 1.0].
    Scores are stored on a 0-1.0 scale (e.g. 0.85 = high exposure).
    Clamp to guard against any out-of-range legacy values.
    """
    return min(1.0, max(0.0, raw))


def _impact(ew: EventWithRelevance, category: RiskCategory, as_of_date: date,
            effective_date: Optional[date] = None) -> float:
    """
    Compute event_impact for a single EventWithRelevance using:
      - recency_multiplier from decay.py (category-specific)
      - relevance_multiplier from ew.relevance_score (junction table)
    """
    ev_date = _event_date_as_date(ew, as_of_date)
    recency = compute_recency_multiplier(
        category, ev_date, as_of_date, effective_date=effective_date
    )
    return compute_event_impact(
        severity=_safe_severity(ew),
        confidence=_safe_confidence(ew),
        recency_multiplier=recency,
        relevance_multiplier=ew.relevance_score,
    )


# ---------------------------------------------------------------------------
# Public aggregation functions
# ---------------------------------------------------------------------------

def derive_material_inputs(
    material_exposures: list[CompanyMaterialExposure],
    trade_events: list[EventWithRelevance],
    as_of_date: date,
) -> tuple[float, float, float]:
    """
    Returns (criticality, concentration, trade_volatility) each on [0, 1.0].

    criticality:
        Average normalised exposure_score across all materials.
        Conservative default 0.5 if no exposure records exist.

    concentration:
        Proportion of exposures with source_geography in HIGH_CONCENTRATION_GEOS.
        Scaled from 0.0 (no HCG supply) to 1.0 (all supply from HCG countries).
        Defaults to 0.5 if no exposures have a source_geography set (data gap —
        never assume zero risk).

    trade_volatility:
        Average event_impact of GEOPOLITICAL_TRADE events, normalised to [0, 1.0]
        by dividing by the theoretical max impact (1.56).  Default 0.3 if no events.
    """
    # --- criticality ---
    if not material_exposures:
        log.warning("evidence_aggregator.no_material_exposures")
        criticality = 0.5
    else:
        criticality = sum(
            _normalise_exposure_score(e.exposure_score) for e in material_exposures
        ) / len(material_exposures)

    # --- concentration ---
    geo_tagged = [e for e in material_exposures if e.source_geography]
    if not geo_tagged:
        # No geography data yet — default to moderate assumed concentration
        concentration = 0.5
    else:
        hcg_count = sum(
            1 for e in geo_tagged if e.source_geography in _HIGH_CONCENTRATION_GEOS
        )
        concentration = hcg_count / len(geo_tagged)

    # --- trade_volatility ---
    if not trade_events:
        trade_volatility = 0.3
    else:
        impacts = [
            _impact(ew, RiskCategory.GEOPOLITICAL_TRADE, as_of_date)
            for ew in trade_events
        ]
        trade_volatility = min(1.0, sum(impacts) / len(impacts) / _MAX_EVENT_IMPACT)

    return criticality, concentration, trade_volatility


def derive_geopolitical_inputs(
    trade_events: list[EventWithRelevance],
    material_exposures: list[CompanyMaterialExposure],
    as_of_date: date,
) -> tuple[float, float, float]:
    """
    Returns (country_concentration, export_restriction_exposure, tariff_exposure)
    each on [0, 1.0].

    country_concentration:
        Proportion of exposures sourced from HIGH_CONCENTRATION_GEOS (CN, CD, RU).
        Mirrors the concentration calculation in derive_material_inputs so both
        pillars reflect the same geography signal.  Defaults to 0.5 if no
        source_geography data is present.

    export_restriction_exposure:
        Average event_impact of events whose title or metadata_json indicates an
        export restriction signal.  Default 0.0 if no matching events.

    tariff_exposure:
        Average event_impact of events with tariff or trade-policy signals.
        Default 0.0 if no matching events.
    """
    geo_tagged = [e for e in material_exposures if e.source_geography]
    if not geo_tagged:
        country_concentration = 0.5
    else:
        hcg_count = sum(
            1 for e in geo_tagged if e.source_geography in _HIGH_CONCENTRATION_GEOS
        )
        country_concentration = hcg_count / len(geo_tagged)

    # Classify trade events by subtype keywords
    export_events: list[EventWithRelevance] = []
    tariff_events: list[EventWithRelevance] = []

    for ew in trade_events:
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

    def _avg_impact(events: list[EventWithRelevance]) -> float:
        if not events:
            return 0.0
        impacts = [_impact(ew, RiskCategory.GEOPOLITICAL_TRADE, as_of_date) for ew in events]
        return min(1.0, sum(impacts) / len(impacts) / _MAX_EVENT_IMPACT)

    return country_concentration, _avg_impact(export_events), _avg_impact(tariff_events)


def derive_regulatory_inputs(
    regulatory_events: list[EventWithRelevance],
    active_obligations: list[tuple[str, float]],
    as_of_date: date,
) -> tuple[list[float], list[tuple[str, float]], float]:
    """
    Returns (top_event_impacts, active_obligations, policy_proximity_adjustment).

    top_event_impacts:
        Computed event_impact for each regulatory event, applying effective_confidence
        floor, category-specific recency decay, and per-supplier relevance_multiplier.
        regulatory_risk.py consumes the top-3 internally.

    active_obligations:
        Passed through unchanged — list of (regulation_key, weight_multiplier) tuples
        from get_active_compliance_obligations().

    policy_proximity_adjustment:
        1.15 if any event has an effective_date within 90 days of as_of_date.
        1.0 otherwise.
    """
    top_event_impacts: list[float] = []
    policy_proximity_adjustment = 1.0

    for ew in regulatory_events:
        # Extract effective_date from metadata if present (for regulatory step-up)
        effective_date: Optional[date] = None
        meta = ew.event.metadata_json or {}
        raw_eff = meta.get("effective_date")
        if raw_eff and isinstance(raw_eff, str):
            try:
                from datetime import datetime as _dt
                effective_date = _dt.fromisoformat(raw_eff).date()
            except ValueError:
                pass

        if effective_date is not None:
            days_to_effective = (effective_date - as_of_date).days
            if 0 <= days_to_effective <= 90:
                policy_proximity_adjustment = 1.15

        impact = _impact(ew, RiskCategory.REGULATORY_COMPLIANCE, as_of_date, effective_date)
        top_event_impacts.append(impact)

    return top_event_impacts, active_obligations, policy_proximity_adjustment


def derive_operational_inputs(
    operational_events: list[EventWithRelevance],
    as_of_date: date,
) -> tuple[float, list[float]]:
    """
    Returns (structural_dependency, weighted_event_impacts).

    structural_dependency:
        Proxy derived from events signalling single-source or capacity constraints.
        Default 0.3 (moderate assumed dependency) when no such events exist —
        never assume 0 since that would understate real supply-chain risk.

    weighted_event_impacts:
        event_impact for each operational event with 90-day half-life recency decay.
    """
    struct_events: list[EventWithRelevance] = []
    for ew in operational_events:
        subtype = (ew.event.metadata_json or {}).get("event_subtype", "")
        text = (ew.event.title or "").lower()
        if subtype in ("SINGLE_SOURCE", "CAPACITY_CONSTRAINT") or (
            "single source" in text or "single-source" in text or "capacity constraint" in text
        ):
            struct_events.append(ew)

    if struct_events:
        structural_dependency = sum(
            _safe_severity(ew) for ew in struct_events
        ) / len(struct_events)
    else:
        structural_dependency = 0.3

    weighted_event_impacts = [
        _impact(ew, RiskCategory.OPERATIONAL, as_of_date)
        for ew in operational_events
    ]

    return structural_dependency, weighted_event_impacts


def derive_financial_inputs(
    filing_events: list[EventWithRelevance],
) -> tuple[float, float, float, int]:
    """
    Returns (base_filing_signal, leverage_warning_bonus, liquidity_stress_bonus,
    filing_count).

    base_filing_signal (0-40):
        Average severity across all filing events × 40.

    leverage_warning_bonus (0-30):
        Sum of severity_scores from events with leverage/covenant language, capped at 30.

    liquidity_stress_bonus (0-30):
        Sum of severity_scores from events with going-concern/cash-runway language,
        capped at 30.

    filing_count:
        Number of distinct filing events provided.  Passed to score_financial_pressure
        for sparse-evidence cap logic.
    """
    filing_count = len(filing_events)
    if not filing_events:
        return 0.0, 0.0, 0.0, 0

    # base_filing_signal: average severity × 40
    avg_severity = sum(_safe_severity(ew) for ew in filing_events) / filing_count
    base_filing_signal = min(40.0, avg_severity * 40.0)

    # leverage_warning_bonus
    leverage_sum = 0.0
    for ew in filing_events:
        subtype = (ew.event.metadata_json or {}).get("event_subtype", "")
        text = (ew.event.title or "").lower()
        if subtype in ("LEVERAGE_WARNING", "COVENANT_STRESS") or (
            "leverage" in text or "covenant" in text or "debt" in text
        ):
            leverage_sum += _safe_severity(ew)
    leverage_warning_bonus = min(30.0, leverage_sum)

    # liquidity_stress_bonus
    liquidity_sum = 0.0
    for ew in filing_events:
        subtype = (ew.event.metadata_json or {}).get("event_subtype", "")
        text = (ew.event.title or "").lower()
        if subtype in ("GOING_CONCERN", "CASH_RUNWAY", "CAPEX_CUT") or (
            "going concern" in text or "cash runway" in text
            or "suspended" in text or "capex" in text
        ):
            liquidity_sum += _safe_severity(ew)
    liquidity_stress_bonus = min(30.0, liquidity_sum)

    return base_filing_signal, leverage_warning_bonus, liquidity_stress_bonus, filing_count
