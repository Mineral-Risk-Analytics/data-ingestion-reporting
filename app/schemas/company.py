"""Pydantic response schemas for the Phase 1 admin dashboard routes.

These mirror the database models but flatten joined fields (e.g. material
canonical_name alongside the exposure row) for a UI-friendly shape.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class CompanyAliasRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    alias: str
    alias_type: str


class CompanySummary(BaseModel):
    """Used for the ``parent`` field on the detail view and other lightweight refs."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    canonical_name: str
    supply_chain_stage: Optional[str] = None


class CompanyListItem(BaseModel):
    """Row shape returned by ``GET /companies``. Includes the latest overall
    risk score (if any) so the list can render a band badge per row."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    canonical_name: str
    legal_name: Optional[str] = None
    supply_chain_stage: Optional[str] = None
    headquarters_country: Optional[str] = None
    is_public: bool
    data_confidence: Optional[float] = None
    parent_company_id: Optional[uuid.UUID] = None
    parent_company_name: Optional[str] = None
    latest_overall_score: Optional[float] = None
    latest_risk_band: Optional[str] = None
    latest_score_as_of_date: Optional[date] = None


class CompanyDetail(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    canonical_name: str
    legal_name: Optional[str] = None
    supply_chain_stage: Optional[str] = None
    headquarters_country: Optional[str] = None
    headquarters_region: Optional[str] = None
    public_ticker: Optional[str] = None
    is_public: bool
    duns_number: Optional[str] = None
    lei: Optional[str] = None
    data_confidence: Optional[float] = None
    data_source: Optional[str] = None
    notes: Optional[str] = None
    parent_company_id: Optional[uuid.UUID] = None
    created_at: datetime
    updated_at: datetime
    aliases: list[CompanyAliasRead] = Field(default_factory=list)
    parent: Optional[CompanySummary] = None


# ---------------------------------------------------------------------------
# Exposures / relationships / regulations
# ---------------------------------------------------------------------------


class ExposureRead(BaseModel):
    id: int
    material_id: int
    material_name: str
    supply_chain_stage: str
    exposure_score: float
    source_geography: Optional[str] = None
    data_confidence: Optional[float] = None
    rationale: Optional[str] = None
    as_of_date: Optional[date] = None


class RelationshipCounterparty(BaseModel):
    id: uuid.UUID
    canonical_name: str
    supply_chain_stage: Optional[str] = None


class RelationshipRead(BaseModel):
    id: int
    relationship_type: str
    material_id: Optional[int] = None
    material_name: Optional[str] = None
    data_confidence: Optional[float] = None
    volume_share_pct: Optional[float] = None
    valid_from: Optional[date] = None
    valid_to: Optional[date] = None
    counterparty: RelationshipCounterparty


class RelationshipsResponse(BaseModel):
    as_buyer: list[RelationshipRead] = Field(default_factory=list)
    as_supplier: list[RelationshipRead] = Field(default_factory=list)


class RegulationExposureRead(BaseModel):
    id: int
    regulation_id: int
    regulation_title: Optional[str] = None
    regulation_key: str
    policy_theme: Optional[str] = None
    effective_date: Optional[date] = None
    compliance_status: str
    exposure_reason: Optional[str] = None
    assessed_at: Optional[date] = None


# ---------------------------------------------------------------------------
# Risk events linked to the company
# ---------------------------------------------------------------------------


class CompanyEventRead(BaseModel):
    id: int
    event_type: str
    event_date: Optional[datetime] = None
    title: str
    summary: Optional[str] = None
    severity_score: Optional[float] = None
    confidence_score: Optional[float] = None
    relevance_score: Optional[float] = None
    match_reason: Optional[str] = None


# ---------------------------------------------------------------------------
# Facilities
# ---------------------------------------------------------------------------


class FacilityRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    facility_type: str
    country: str
    region: Optional[str] = None
    city: Optional[str] = None
    status: str
    capacity_notes: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None


# ---------------------------------------------------------------------------
# Vehicle models + chemistries
# ---------------------------------------------------------------------------


class VehicleModelChemistryRead(BaseModel):
    id: int
    battery_chemistry_id: int
    chemistry_slug: str
    share_pct: float
    valid_from: date
    valid_to: Optional[date] = None
    notes: Optional[str] = None


class VehicleModelRead(BaseModel):
    id: int
    model_name: str
    model_year_start: Optional[int] = None
    model_year_end: Optional[int] = None
    production_volume_units: Optional[int] = None
    production_volume_year: Optional[int] = None
    is_active: bool
    data_source: Optional[str] = None
    chemistries: list[VehicleModelChemistryRead] = Field(default_factory=list)
