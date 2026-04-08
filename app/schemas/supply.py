from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from pydantic import BaseModel, ConfigDict


class SupplierRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    canonical_name: str
    supplier_type: str
    headquarters_country: Optional[str] = None
    headquarters_region: Optional[str] = None
    public_ticker: Optional[str] = None
    is_public: bool
    notes: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class TradeFlowRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    source_document_id: int
    period: str
    reporter_country: str
    partner_country: str
    hs_code: Optional[str] = None
    hs_description: Optional[str] = None
    material_id: Optional[int] = None
    import_export_flag: str
    quantity: Optional[float] = None
    quantity_unit: Optional[str] = None
    trade_value_usd: Optional[float] = None
    metadata_json: Optional[Dict[str, Any]] = None
    created_at: datetime
