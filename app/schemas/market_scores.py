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


# ---------------------------------------------------------------------------
# Market-score evidence drill-down
# ---------------------------------------------------------------------------
#
# Schemas powering ``GET /materials/{material_id}/market-scores/{geography_code}
# /evidence``.  Frontend renders this in the expanded country-scores dropdown
# instead of the raw sub-input numbers it used to show — same data the scoring
# engine consumed, surfaced for analyst trust.
#
# Membership rule: each list is the strict INTERSECTION of (this material)
# and (this country) — not the union the scoring engine consumes.  Surfacing
# only the intersection prevents misattribution when an analyst sees "10 events"
# under Phosphate × Morocco that actually came from "Phosphate ANY country" +
# "ANY material in Morocco".  Tradeoff: list lengths may look small compared
# to the table's pillar scores, which IS the right behaviour — a high
# Geopolitical score driven by global material events shouldn't lie about
# being country-specific.


class EvidenceRegulationItem(BaseModel):
    """One regulation that scopes BOTH this material and this country."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    regulation_key: str
    title: Optional[str] = None
    issuing_body: Optional[str] = None
    status: Optional[str] = None
    effective_date: Optional[date] = None
    summary: Optional[str] = None

    material_scope_type: str
    """``covered`` | ``restricted`` | ``banned`` | ``disclosure_required``.
    Copied from RegulationMaterialScope.scope_type for this material."""

    geography_scope_type: str
    """``jurisdiction`` | ``origin_country`` | ``targeted_country``.
    Copied from RegulationGeographyScope.scope_type for this country."""

    geography_compliance_weight: Optional[float] = None
    """Per-geography curation from Regulation.geography_compliance_weights
    (0.0–1.0); None when no curation exists (universal 0.50 default applies)."""


class EvidenceFacilityItem(BaseModel):
    """One facility located in this country and linked to this material."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    """UUID — serialised as string for the JSON response."""
    name: Optional[str] = None
    facility_type: str
    """mine | refinery | cell_factory | pack_plant | recycling | r_and_d | hq"""
    status: str
    """operating | planned | under_construction | mothballed | closed"""
    region: Optional[str] = None
    city: Optional[str] = None
    capacity_notes: Optional[str] = None
    is_primary_product: bool
    """From FacilityMaterialLink.is_primary_product — True when this material
    is the facility's primary commodity (vs. a co-product)."""
    annual_capacity_tpy: Optional[float] = None
    """Nameplate capacity in tonnes/year, when published.  Null when MRDS
    did not surface a figure."""
    supply_chain_stage: Optional[str] = None
    """ore | concentrate | intermediate | refined | battery_grade | fabricated
    | scrap.  Null on rows pre-dating migration 029."""


class EvidenceRiskEventItem(BaseModel):
    """One risk event tagged to BOTH this material AND this country."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    event_type: str
    event_subtype: Optional[str] = None
    severity_score: Optional[float] = None
    confidence_score: Optional[float] = None
    event_date: Optional[date] = None
    summary: Optional[str] = None
    source_system: Optional[str] = None
    """Which ingester produced the event — gta | federal_register | eurlex |
    iea | opensanctions | sec_edgar — derived from the source document."""


class MarketScoreEvidence(BaseModel):
    """Aggregated evidence for one (material, geography) pair.

    Single round-trip response so the dropdown doesn't have to orchestrate
    three concurrent requests + their loading states.
    """

    material_id: int
    geography_code: str
    regulations: list[EvidenceRegulationItem]
    facilities: list[EvidenceFacilityItem]
    risk_events: list[EvidenceRiskEventItem]

    regulation_total: int
    """How many regulations matched the (material × country) intersection
    before truncation.  ``len(regulations)`` may be smaller — partner can
    compare to know how much was hidden."""
    facility_total: int
    risk_event_total: int
    # 056: broad multi-material measures excluded from the risk_events list
    # (scoring consumed them at breadth-discounted relevance).  Frontend
    # renders "+ N broad measures" when > 0.
    risk_event_broad_total: int = 0

    risk_event_window_days: int
    """How many days back risk events were drawn from.  Matches the longest
    scoring lookback window so the analyst sees the same evidence the
    score consumed."""
