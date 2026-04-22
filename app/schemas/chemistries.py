"""Pydantic schemas for battery chemistries."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict


class ChemistryRiskScoreRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    as_of_date: date
    methodology_version: str
    material_concentration_score: Optional[float] = None
    geopolitical_score: Optional[float] = None
    composite_risk_score: Optional[float] = None
    score_confidence: Optional[float] = None
    computed_at: datetime


class BatteryChemistryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    slug: str
    name: str
    description: Optional[str] = None
    status: str
    current_market_share_pct: Optional[float] = None
    market_share_as_of_date: Optional[date] = None
    is_active: bool
    verified: bool = False
    created_at: datetime
    updated_at: datetime

    latest_risk_score: Optional[ChemistryRiskScoreRead] = None
