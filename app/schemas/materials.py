"""Pydantic schemas for materials, HS-code mappings, and mismatch detection."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class HsMappingRead(BaseModel):
    """Serializes ``HsCodeMaterialMapping`` rows.

    Column names are aliased to match the spec's preferred API names so the
    frontend types stay stable even if internal column names drift.

    Stage / scope / digit_count / keywords (added May 2026) come straight from
    seed_hs_mappings — no aliasing needed.  Used by the frontend HS Codes &
    Stages tab to group rows by stage and to surface the partner-curated
    keyword aliases used for trade-event attribution.
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
    hs_mapping_count: int = 0
    mapping_mismatch_count: int = 0
    verified: bool = False
    # Top producing countries — sourced from the JSONB column on Material.
    primary_producing_countries: Optional[List[str]] = None
    # Latest global composite risk score (0–100). Populated by route handler via subquery.
    latest_overall_risk_score: Optional[float] = None


class CountryShareItem(BaseModel):
    """One country's share of world production for a material."""
    code: str
    share_pct: int  # rounded integer percentage, e.g. 47


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
