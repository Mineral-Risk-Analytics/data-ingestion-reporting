"""Pydantic schemas for materials, HS-code mappings, and mismatch detection."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class HsMappingGeographyRead(BaseModel):
    """Per-country breakdown for one HS mapping node.

    Combines the most recent ``hs_code_production_shares`` row (Geography +
    production_share) with any matching ``hs_code_geography_risk_scores``
    row (TARIFF / EXPORT / SCORE) and counted ``risk_event_hs_mappings``
    events.  Populated by the route handler from a bulk pre-load so the
    frontend HS Codes & Stages tab can render per-row geography + scoring
    without per-row roundtrips.

    All score fields are Optional — they're ``None`` until
    ``rescore-hs-nodes`` populates ``hs_code_geography_risk_scores``.
    Production_share alone (no scores) is the steady state between MCS
    ingest and the next scoring run.
    """

    model_config = ConfigDict(from_attributes=True)

    country_code: str
    # ── From hs_code_production_shares (latest reference_year, market_scope='global')
    production_share: Optional[float] = None     # 0–1
    production_volume: Optional[float] = None
    reference_year: Optional[int] = None
    # ── From hs_code_geography_risk_scores (latest as_of_date, market_scope='global')
    hhi: Optional[float] = None                  # 0–1, hhi_at_stage
    tariff_exposure: Optional[float] = None      # 0–1
    export_restriction: Optional[float] = None   # 0–1
    score: Optional[float] = None                # 0–100, composite_node_score
    scored_at: Optional[datetime] = None
    # ── ``score_method`` plucked from ``HsCodeGeographyRiskScore.metadata_json``
    # ``"hhi_anchored"`` = canonical 0.50×HHI + 0.25×tariff + 0.25×export
    # ``"event_only_no_hhi"`` = fallback 0.5×tariff + 0.5×export when no
    # production-share data exists at this stage (Option 1, 2026-05-06).
    # Frontend uses this to show a "no production base" badge so partner
    # understands why HHI is blank on a scored row.
    score_method: Optional[str] = None
    # ── Event counts joined from risk_event_hs_mappings
    events_open: int = 0


class HsMappingRead(BaseModel):
    """Serializes ``HsCodeMaterialMapping`` rows.

    Column names are aliased to match the spec's preferred API names so the
    frontend types stay stable even if internal column names drift.

    Stage / scope / digit_count / keywords (added May 2026) come straight from
    seed_hs_mappings — no aliasing needed.  Used by the frontend HS Codes &
    Stages tab to group rows by stage and to surface the partner-curated
    keyword aliases used for trade-event attribution.

    Aggregate fields (``geographies``, ``hhi``, ``node_score``,
    ``events_open``, ``scored_at``) are populated by the route handler from
    a bulk pre-load against ``hs_code_production_shares`` +
    ``hs_code_geography_risk_scores`` + ``risk_event_hs_mappings``.  All are
    Optional / default-empty so callers that don't enrich (e.g. the global
    mismatches view) still serialize cleanly.
    """

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: int
    hs_code: str = Field(alias="hs_code_prefix")
    material_id: int
    hs_description: Optional[str] = Field(None, alias="description")
    mapping_confidence: float = Field(alias="confidence")
    created_at: datetime

    # Stage + scope metadata from seed_hs_mappings.
    supply_chain_stage: Optional[str] = None
    stage_sequence: Optional[int] = None
    digit_count: int = 4
    market_scope: str = "global"
    keywords: Optional[List[str]] = None

    # Computed mismatch flags — populated by the route handler, not from ORM.
    is_low_confidence: bool = False
    is_missing_description: bool = False
    is_chapter_mismatch: bool = False
    is_cross_mapped: bool = False

    # ── Aggregate / breakdown fields (added 2026-05-06) ─────────────────
    # Populated by the route handler from a bulk pre-load.  Optional so
    # callers that don't enrich (mismatches view, global lists) still
    # serialize cleanly.
    geographies: List[HsMappingGeographyRead] = []
    hhi: Optional[float] = None                  # weighted avg across geographies
    node_score: Optional[float] = None           # max composite_node_score across geos
    events_open: int = 0                         # total RiskEventHsMapping count
    scored_at: Optional[datetime] = None         # most recent scoring as_of_date
    # Node-level method rollup (2026-05-06). ``"hhi_anchored"`` if any
    # geography on the node was scored with production data, else
    # ``"event_only_no_hhi"`` if the only scores came from the fallback,
    # else None.  Frontend keys the "no production base" badge off this.
    score_method: Optional[str] = None

    @property
    def is_mismatched(self) -> bool:
        return (
            self.is_low_confidence
            or self.is_missing_description
            or self.is_chapter_mismatch
            or self.is_cross_mapped
        )


class MappingHealth(BaseModel):
    total: int
    mismatched: int
    low_confidence: int
    missing_description: int
    chapter_mismatch: int
    cross_mapped: int


class CriticalitySignalRead(BaseModel):
    """Serializes a row from ``material_criticality_signals``.

    All eight signal columns are exposed (May 2026) so the Overview tab
    can render the supply-side and price-trend stats the parsers
    populate.  ``metadata_json`` carries source-specific extras such as
    ``us_net_import_reliance_pct`` (from MCS Salient stats),
    ``us_apparent_consumption`` (same), ``mcs_publication_year``, and
    ``fig10_source_rows`` (the verbatim Fig 10 commodity strings that
    were averaged into this material's price values).
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    source: str
    reference_year: int
    criticality_score: Optional[float] = None
    trend_direction: Optional[str] = None
    hhi_score: Optional[float] = None
    # ── Supply metrics added migration 019 ────────────────────────────────
    reserve_hhi_score: Optional[float] = None
    reserve_life_index: Optional[float] = None
    production_yoy_pct: Optional[float] = None
    capacity_utilization: Optional[float] = None
    # ── Price-trend metrics added migration 036 ──────────────────────────
    price_yoy_pct: Optional[float] = None
    price_cagr_5yr_pct: Optional[float] = None
    # ── US-dependency metrics promoted from metadata_json (migration 038) ─
    us_net_import_reliance_pct: Optional[float] = None
    us_apparent_consumption: Optional[float] = None
    # ── Source-specific extras ────────────────────────────────────────────
    metadata_json: Optional[Dict[str, Any]] = None
    created_at: datetime


class ChemistryUseRead(BaseModel):
    """Junction row shown on material detail — enriched with chemistry name/slug."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    battery_chemistry_id: int
    # Populated by the route handler via a join — not a direct ORM field.
    chemistry_slug: Optional[str] = None
    chemistry_name: Optional[str] = None
    role: str
    intensity: float
    is_substitutable: bool


class CountryShareItem(BaseModel):
    """One country's share of world production for a material."""
    code: str
    share_pct: int  # rounded integer percentage, e.g. 47


class CountryMaterialRelevanceItem(BaseModel):
    """One row from country_material_relevance — a per-(material, country, role)
    relevance flag with partner-curation surface.

    Producer rows are auto-derived from material_production_shares; consumer
    rows are initial placeholders or partner-curated.
    """
    model_config = ConfigDict(from_attributes=True)

    country_code: str               # ISO-3166-1 alpha-2
    country_name: Optional[str] = None  # joined from countries.name when available
    role: str                       # 'producer' | 'consumer'
    tier: Optional[str] = None      # 'top' | 'mid' | 'minor' | 'emerging'
    is_hcg: bool
    source: str                     # 'mcs_share' | 'partner_curated' | 'derived'
    derived_share: Optional[float] = None
    reference_year: Optional[int] = None
    notes: Optional[str] = None


class MaterialListPillarScore(BaseModel):
    """One pillar's score for the Materials list row.

    Drives the 5-dot pillar coverage indicator on the Materials page.  A
    dot is colored when ``has_signal`` is True (score > 0); rendered gray
    when the score is 0 (fallback) or null (no score row).
    """
    name: str                # e.g. "material_concentration_score"
    label: str               # e.g. "Material Concentration"
    score: Optional[float] = None
    has_signal: bool = False


class MaterialListItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    canonical_name: str
    category: Optional[str] = None
    symbol_or_code: Optional[str] = None
    criticality_score: Optional[float] = None
    is_ira_critical_mineral: bool
    is_eu_crma_critical: bool
    data_availability: Optional[str] = None
    # ``hs_mapping_count`` and ``mapping_mismatch_count`` retained for
    # backwards-compat with any external consumers; the Materials page
    # no longer renders them (mismatch UI retired 2026-05-11).  Backend
    # still populates ``hs_mapping_count`` but reports
    # ``mapping_mismatch_count = 0`` everywhere.
    hs_mapping_count: int = 0
    mapping_mismatch_count: int = 0
    verified: bool = False
    # Top producing countries — legacy ISO-code-only list, retained for
    # backwards-compat with any other consumers of this schema.
    primary_producing_countries: Optional[List[str]] = None
    # Latest global composite risk score (0–100). Populated by route handler via subquery.
    latest_overall_risk_score: Optional[float] = None

    # 2026-05-11 analyst-view extensions
    is_launch_list: bool = False
    """Whether the material is in the launch-list (the core 10 minerals
    the v1 product focuses on).  Drives the leading ★ marker on the
    Materials page and the default filter scope."""
    top_producer_shares: List["CountryShareItem"] = Field(default_factory=list)
    """Top producing countries with their share % of global production.
    Up to 3 entries, sorted by share descending.  Empty if no
    MaterialProductionShare data exists for this material."""
    pillar_scores: List[MaterialListPillarScore] = Field(default_factory=list)
    """Latest per-pillar scores for the 5 risk pillars, in canonical order.
    Drives the 5-dot pillar coverage indicator.  Empty list if no
    MaterialGlobalRiskScore row exists at all."""
    recent_event_count_90d: int = 0
    """Count of RiskEvent rows created in the last 90 days that map to
    this material via RiskEventMaterial.  Matches the coverage matrix
    and Coverage Gaps KPI window for consistency."""


class MaterialDetail(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    canonical_name: str
    category: Optional[str] = None
    symbol_or_code: Optional[str] = None
    hs_codes: Optional[Any] = None
    criticality_score: Optional[float] = None
    primary_producing_countries: Optional[Any] = None
    # Production share breakdown — populated by route handler from material_production_shares.
    # Sorted by share descending; only latest reference_year. Empty list if no data.
    country_production_shares: List[CountryShareItem] = []
    price_unit: Optional[str] = None
    is_ira_critical_mineral: bool
    is_eu_crma_critical: bool
    patent_occurrence_trend: Optional[str] = None
    data_availability: Optional[str] = None
    notes: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    criticality_signals: List[CriticalitySignalRead] = []
    chemistry_uses: List[ChemistryUseRead] = []
    hs_mappings: List[HsMappingRead] = []
    mapping_health: MappingHealth = Field(
        default_factory=lambda: MappingHealth(
            total=0, mismatched=0, low_confidence=0,
            missing_description=0, chapter_mismatch=0, cross_mapped=0,
        )
    )

    # 2026-05-11 analyst-view extensions for the Materials detail Overview
    # tab.  Both are computed by the route handler.  Defaults preserve
    # backwards-compat for older clients reading this schema shape.
    recent_event_count_90d: int = 0
    """Count of RiskEvent rows created in the last 90 days that map to
    this material via RiskEventMaterial.  Matches the same window used
    on the dashboard's coverage matrix + Coverage Gaps KPI."""
    facility_count: int = 0
    """Count of FacilityMaterialLink rows (any stage, any capacity) for
    this material.  Tells the analyst how much structural data backs the
    operational pillar.  0 means launch-blocker for that pillar."""
    score_trend_7d: Optional[str] = None
    """``"rising"`` | ``"stable"`` | ``"declining"`` | None.

    Trend in the material's overall composite risk score — comparing the
    latest MaterialGlobalRiskScore.overall_risk_score to the most recent
    snapshot at least 7 days older.  Rising = score went UP (risk
    increased); declining = score went DOWN (risk improved); stable =
    moved by ≤5 points on the 0–100 scale.  None when there's no prior
    snapshot or the prior is more than 30 days stale.

    Sourced from actual scoring output rather than event count so the
    indicator answers "is risk going up?" semantically — not "are there
    more events flowing through?" which could include constructive
    signals (IEA investment pledges).  Calibration owned by
    ``_compute_score_trend`` in ``app/api/routes/materials.py``."""


class HsMismatchItem(BaseModel):
    """One row in the global mismatches list."""

    id: int
    hs_code: str
    material_id: int
    material_canonical_name: str
    hs_description: Optional[str] = None
    mapping_confidence: float
    is_low_confidence: bool
    is_missing_description: bool
    is_chapter_mismatch: bool
    is_cross_mapped: bool

    # Link back to the material's HS Mappings tab
    material_url: str = ""
