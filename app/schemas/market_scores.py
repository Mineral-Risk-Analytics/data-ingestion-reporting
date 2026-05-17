"""Pydantic schemas for the market intelligence layer (material × geography).

Read models for ``MaterialGeographyRiskScore`` rows produced by
``app.services.scoring.market_aggregator``. These scores are company-agnostic
and feed both the Mineral Risk Analytics public hub (Phase 4) and the
internal admin browser.

The list view (``MaterialGeographyScoreRead``) deliberately omits
``rationale_json`` because it can be large (sub-input dicts, top-evidence IDs,
weight blocks). The detail view (``MaterialGeographyScoreDetail``) includes
the full rationale so the UI can render the evidence trail.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict


class MaterialGeographyScoreRead(BaseModel):
    """List/browse view — rationale omitted to keep payloads small."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    material_id: int
    geography_code: str
    as_of_date: date

    material_concentration_score: Optional[float] = None
    geopolitical_trade_score: Optional[float] = None
    regulatory_compliance_score: Optional[float] = None
    operational_score: Optional[float] = None
    financial_pressure_score: Optional[float] = None
    overall_risk_score: Optional[float] = None

    event_count: int = 0
    """UNION of material-anchored and geography-anchored events consumed by
    the pillar sub-input derivation.  Preserves the audit trail of what was
    actually fed into the score.  For 'this country × this material' reads,
    use ``event_count_geo_specific`` — see migration 042."""
    event_count_geo_specific: Optional[int] = None
    """INTERSECTION (RiskEventMaterial ∩ RiskEventGeography) — count of
    events tagged to BOTH this material AND this country, within each
    category's lookback window.  This is what the UI 'Events' column
    displays and what the Country-scores exposure filter keys off.  ``None``
    on rows produced before migration 042 — re-run ``POST /market/rescore``
    to populate."""
    scoring_version: str
    created_at: datetime

    # 2026-05-11 analyst-view enrichments — surfaced by the
    # /materials/{id}/market-scores route so the frontend can filter the
    # Country scores table to countries with material exposure rather
    # than rendering every scored geography (which is often 100+).
    production_share_pct: Optional[int] = None
    """Percentage share of global production for this material that
    comes from this country (latest reference_year).  None when the
    country isn't a tracked producer.  Drives the "Share" column and
    the ≥1% filter for the default Country Scores view."""
    facility_count: int = 0
    """Number of FacilityMaterialLink rows whose facility is located in
    this country AND links to this material.  Helps the analyst spot
    operational exposure (a country that doesn't produce but houses
    processing/refining facilities)."""


class MaterialGeographyScoreDetail(MaterialGeographyScoreRead):
    """Single-pair view — full rationale included for evidence display."""

    rationale_json: Optional[dict[str, Any]] = None


class MaterialGlobalScoreRead(BaseModel):
    """Trade-flow-weighted rollup across all geographies for one material.

    Mirrors ``material_global_risk_scores`` (one row per material per date).
    Used by the admin material-detail overview and the chemistry risk scorer.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    material_id: int
    as_of_date: date

    material_concentration_score: Optional[float] = None
    geopolitical_trade_score: Optional[float] = None
    regulatory_compliance_score: Optional[float] = None
    operational_score: Optional[float] = None
    financial_pressure_score: Optional[float] = None
    overall_risk_score: Optional[float] = None

    trade_weighted_geo_count: int = 0
    total_trade_value_usd: Optional[float] = None
    created_at: datetime


class RescoredResult(BaseModel):
    """Response payload for ``POST /market/rescore``."""

    scored: int
    as_of_date: date
    run_id: str
