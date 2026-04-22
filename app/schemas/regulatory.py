from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, List, Optional, Union

from pydantic import BaseModel, ConfigDict


class RegulationMaterialScopeRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    material_id: int
    scope_type: str
    notes: Optional[str] = None


class RegulationGeographyScopeRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    country_code: str
    scope_type: str


class RegulationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    source_document_id: Optional[int] = None
    regulation_key: str
    title: Optional[str] = None
    issuing_body: Optional[str] = None
    geography: Optional[str] = None
    policy_theme: Optional[str] = None
    status: Optional[str] = None
    publication_date: Optional[date] = None
    effective_date: Optional[date] = None
    summary: Optional[str] = None
    metadata_json: Optional[Dict[str, Any]] = None
    verified: bool = False
    created_at: datetime
    updated_at: datetime


class RegulationDetail(RegulationRead):
    """Extends RegulationRead with joined scope tables."""

    material_scopes: List[RegulationMaterialScopeRead] = []
    geography_scopes: List[RegulationGeographyScopeRead] = []


class RiskEventRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    source_document_id: Optional[int] = None
    event_type: str
    event_date: Optional[datetime] = None
    title: str
    summary: Optional[str] = None
    severity_score: Optional[float] = None
    confidence_score: Optional[float] = None
    risk_categories_json: Optional[Union[List[Any], Dict[str, Any]]] = None
    geography_json: Optional[Union[Dict[str, Any], List[Any]]] = None
    metadata_json: Optional[Dict[str, Any]] = None
    verified: bool = False
    created_at: datetime
