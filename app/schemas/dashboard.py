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


# ---------------------------------------------------------------------------
# New analyst-view KPI shapes (2026-05-11)
# ---------------------------------------------------------------------------
# The dashboard pivoted from generic platform metrics ("how many materials
# are tracked?") to launch-list-centric analyst metrics ("how many of the
# core 10 minerals are scored / have signal / need attention?").  These
# three shapes back the new KPI strip.


class CoreMineralsScored(BaseModel):
    """How many launch-list materials currently have a global risk score."""

    scored: int                       # count with a non-null overall_risk_score
    total: int                        # size of the launch list (10 today)
    unscored_names: list[str] = Field(default_factory=list)
    """Canonical names of launch-list materials that DON'T have a current
    global score.  Surfaced so the dashboard can drill into 'what's
    missing' rather than just showing a fraction."""


class SourceCount(BaseModel):
    """Per-source event count, used in :class:`RecentRiskEvents30d`."""

    source_name: str
    count: int


class RecentRiskEvents30d(BaseModel):
    """Volume of risk_events ingested in the last 30 days."""

    count: int
    prev_period_count: int            # same query for [60d, 30d) — comparison baseline
    top_sources: list[SourceCount] = Field(default_factory=list)
    """Top sources by event count in the trailing 30-day window.
    Capped at 5; the long tail isn't useful at-a-glance."""


class CoverageGapItem(BaseModel):
    """One launch-list mineral that fails the gap-detection bar."""

    material_id: int
    canonical_name: str
    reasons: list[str] = Field(default_factory=list)
    """Plain-English reasons this mineral is flagged.  Composed from any
    combination of: ``"no_global_score"`` (no MaterialGlobalRiskScore),
    ``"stale_score"`` (latest as_of_date > 30 days old), ``"thin_events"``
    (fewer than 5 risk events in last 90 days), ``"thin_pillars"`` (fewer
    than 3 pillars have non-fallback signal at latest score).  Multiple
    reasons can apply."""


class CoverageGaps(BaseModel):
    """Summary of launch-list coverage gaps, for the dashboard KPI card."""

    count: int                        # how many launch-list materials fail the bar
    materials: list[CoverageGapItem] = Field(default_factory=list)
    """Per-gap detail rows.  The KPI card uses the count for the headline
    number; click-through views render the full list."""


class DashboardOverview(BaseModel):
    company_count_total: int
    company_count_by_stage: list[StageCount] = Field(default_factory=list)
    confidence_distribution: list[ConfidenceBucket] = Field(default_factory=list)
    companies_with_score: int
    recent_notes_count_7d: int
    # Extended KPI fields (legacy — present for backwards-compat with any
    # readers still on the 2026-04 schema; new dashboards should consume
    # the launch-list-centric fields below instead)
    material_count_total: int = 0
    material_count_this_quarter: int = 0
    suspect_mappings_count: int = 0
    recent_notes_entity_count_7d: int = 0
    # New analyst-view KPI fields (2026-05-11)
    core_minerals_scored: Optional[CoreMineralsScored] = None
    recent_risk_events_30d: Optional[RecentRiskEvents30d] = None
    coverage_gaps: Optional[CoverageGaps] = None
    # Dashboard sections
    top_materials_by_risk: list[TopMaterialRisk] = Field(default_factory=list)
    score_run_progress: Optional[ScoreRunProgress] = None
    recent_activity: list[RecentNoteItem] = Field(default_factory=list)
