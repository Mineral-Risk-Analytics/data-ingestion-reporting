"""Analyst-note schemas (company flag-issue dialog in Phase 1)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


NoteType = Literal["data_error", "missing_data", "outdated", "other"]


class AnalystNoteRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    entity_type: str
    entity_id: str
    note_type: str
    note_text: str
    created_at: datetime
    updated_at: datetime


class AnalystNoteCreate(BaseModel):
    """Body for ``POST /companies/{id}/notes``. ``entity_type`` defaults to
    ``company`` since the Phase 1 dialog always targets companies."""

    note_type: NoteType
    note_text: str = Field(min_length=3, max_length=4000)
    entity_type: Literal["company"] = "company"
