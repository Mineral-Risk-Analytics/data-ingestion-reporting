from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict


class CompanyRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    canonical_name: str
    supply_chain_stage: Optional[str] = None
    headquarters_country: Optional[str] = None
    public_ticker: Optional[str] = None
    is_public: bool
    employee_count: Optional[int] = None
    revenue_usd_millions: Optional[float] = None
    notes: Optional[str] = None
    created_at: datetime
    updated_at: datetime
