from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.models import RiskEvent
from app.schemas.regulatory import RiskEventRead

router = APIRouter(prefix="/risk-events", tags=["risk-events"])


@router.get("", response_model=list[RiskEventRead])
def list_risk_events(
    limit: int = 100,
    db: Session = Depends(get_db),
) -> list[RiskEvent]:
    q = select(RiskEvent).order_by(RiskEvent.created_at.desc()).limit(min(limit, 500))
    return list(db.scalars(q).all())
