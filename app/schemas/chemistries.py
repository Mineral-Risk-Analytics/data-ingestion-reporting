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
    metadata_json: Optional[dict[str, Any]] = None


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


class ChemistryMaterialRead(BaseModel):
    """One active row from ``battery_chemistry_materials`` joined to its material."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    material_id: int
    material_canonical_name: str
    role: str
    intensity: float
    is_substitutable: bool
    valid_from: date
    valid_to: Optional[date] = None
    notes: Optional[str] = None


class ChemistryDetailRead(BatteryChemistryRead):
    """Single-chemistry detail view: full record + latest risk score + active composition."""

    active_materials: list[ChemistryMaterialRead] = []
