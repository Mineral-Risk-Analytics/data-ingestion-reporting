"""Dashboard overview response schema."""

from __future__ import annotations

from pydantic import BaseModel, Field


class StageCount(BaseModel):
    stage: str
    count: int


class ConfidenceBucket(BaseModel):
    bucket: str
    count: int


class DashboardOverview(BaseModel):
    company_count_total: int
    company_count_by_stage: list[StageCount] = Field(default_factory=list)
    confidence_distribution: list[ConfidenceBucket] = Field(default_factory=list)
    companies_with_score: int
    recent_notes_count_7d: int
