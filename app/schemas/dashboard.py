"""Dashboard overview response schema."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class StageCount(BaseModel):
    stage: str
    count: int


class ConfidenceBucket(BaseModel):
    bucket: str
    count: int


class ProductionCountryItem(BaseModel):
    code: str
    share_pct: int  # 0–100, rounded


class TopMaterialRisk(BaseModel):
    material_id: int
    canonical_name: str
    symbol_or_code: Optional[str] = None
    category: Optional[str] = None
    overall_risk_score: float
    top_countries: list[ProductionCountryItem] = Field(default_factory=list)
    trend: Optional[str] = None  # "rising" | "stable" | "declining"
    as_of_date: str  # ISO date string


class PillarProgress(BaseModel):
    name: str
    label: str
    # % of scored pairs where this pillar has score > 0 (actual data signal)
    signal_pct: int
    # % of scored pairs where this pillar column is non-null (computed, even if 0)
    computed_pct: int


class ScoreRunProgress(BaseModel):
    last_run_date: Optional[str] = None  # ISO date
    # Geographies and materials with at least one event driving their score
    valid_geographies: int = 0
    valid_materials: int = 0
    # All distinct geographies and materials that were scored (including floor-score pairs)
    scored_geographies: int = 0
    scored_materials: int = 0
    total_geographies: int = 0
    total_materials: int = 0
    pillars: list[PillarProgress] = Field(default_factory=list)


class RecentNoteItem(BaseModel):
    entity_type: str
    entity_id: str
    entity_name: Optional[str] = None  # material/company name for display
    note_type: str
    note_text: str
    created_at: str  # ISO datetime string


class DashboardOverview(BaseModel):
    company_count_total: int
    company_count_by_stage: list[StageCount] = Field(default_factory=list)
    confidence_distribution: list[ConfidenceBucket] = Field(default_factory=list)
    companies_with_score: int
    recent_notes_count_7d: int
    # Extended KPI fields
    material_count_total: int = 0
    material_count_this_quarter: int = 0
    suspect_mappings_count: int = 0
    recent_notes_entity_count_7d: int = 0
    # Dashboard sections
    top_materials_by_risk: list[TopMaterialRisk] = Field(default_factory=list)
    score_run_progress: Optional[ScoreRunProgress] = None
    recent_activity: list[RecentNoteItem] = Field(default_factory=list)
