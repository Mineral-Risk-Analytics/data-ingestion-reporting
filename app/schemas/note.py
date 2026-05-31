"""Analyst-note schemas — generalized for all flaggable entity types."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


NoteType = Literal["data_error", "missing_data", "outdated", "other"]

FlaggableEntityType = Literal[
    "company",
    "company_material_exposure",
    "company_supply_relationship",
    "company_regulation_exposure",
    "company_vehicle_model",
    "material",
    "hs_code_material_mappings",
    "regulation",
    "risk_event",
    "facility",
    "battery_chemistry",
]


class AnalystNoteBase(BaseModel):
    note_type: NoteType
    note_text: str = Field(min_length=3, max_length=4000)


class AnalystNoteRead(AnalystNoteBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    entity_type: FlaggableEntityType
    entity_id: str
    created_at: datetime
    updated_at: datetime


class AnalystNoteCreate(AnalystNoteBase):
    """Request body for per-entity POST …/notes endpoints.

    Clients send only ``note_type`` and ``note_text``.  The route handler
    injects ``entity_type`` and ``entity_id`` from the URL path before writing
    to ``analyst_notes``.
    """

    pass
