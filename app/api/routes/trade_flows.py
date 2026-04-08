from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.models import TradeFlow
from app.schemas.supply import TradeFlowRead

router = APIRouter(prefix="/trade-flows", tags=["trade-flows"])


@router.get("", response_model=list[TradeFlowRead])
def list_trade_flows(
    limit: int = 200,
    db: Session = Depends(get_db),
) -> list[TradeFlow]:
    q = select(TradeFlow).order_by(TradeFlow.created_at.desc()).limit(min(limit, 2000))
    return list(db.scalars(q).all())
