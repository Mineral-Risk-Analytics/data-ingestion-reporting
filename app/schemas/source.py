from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from pydantic import BaseModel, ConfigDict


class SourceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    source_type: str
    phase: str
    is_active: bool
    config_json: Optional[Dict[str, Any]] = None
    created_at: datetime
    updated_at: datetime
