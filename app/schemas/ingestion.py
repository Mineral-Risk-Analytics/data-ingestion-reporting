from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from pydantic import BaseModel, ConfigDict, Field


class IngestionTriggerRequest(BaseModel):
    """Optional parameters passed to a source-specific ingestion run."""

    extra: Dict[str, Any] = Field(default_factory=dict)


class IngestionRunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    source_id: int
    status: str
    started_at: datetime
    completed_at: Optional[datetime] = None
    parameters_json: Optional[Dict[str, Any]] = None
    error_message: Optional[str] = None
    stats_json: Optional[Dict[str, Any]] = None


class IngestionRunCreate(BaseModel):
    source_id: int
    status: str = "running"
    parameters_json: Optional[Dict[str, Any]] = None
