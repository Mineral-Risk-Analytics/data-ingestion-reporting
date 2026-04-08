from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.models import Source
from app.schemas.source import SourceRead

router = APIRouter(prefix="/sources", tags=["sources"])


@router.get("", response_model=list[SourceRead])
def list_sources(
    db: Session = Depends(get_db),
) -> list[Source]:
    return list(db.scalars(select(Source).order_by(Source.name)).all())
