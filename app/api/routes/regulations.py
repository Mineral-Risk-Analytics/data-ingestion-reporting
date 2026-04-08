from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.models import Regulation
from app.schemas.regulatory import RegulationRead

router = APIRouter(prefix="/regulations", tags=["regulations"])


@router.get("", response_model=list[RegulationRead])
def list_regulations(
    limit: int = 100,
    db: Session = Depends(get_db),
) -> list[Regulation]:
    q = select(Regulation).order_by(Regulation.updated_at.desc()).limit(min(limit, 500))
    return list(db.scalars(q).all())
