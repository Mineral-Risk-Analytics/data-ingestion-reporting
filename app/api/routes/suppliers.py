from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.models import Supplier
from app.schemas.supply import SupplierRead

router = APIRouter(prefix="/suppliers", tags=["suppliers"])


@router.get("", response_model=list[SupplierRead])
def list_suppliers(
    limit: int = 200,
    db: Session = Depends(get_db),
) -> list[Supplier]:
    q = select(Supplier).order_by(Supplier.canonical_name).limit(min(limit, 1000))
    return list(db.scalars(q).all())
