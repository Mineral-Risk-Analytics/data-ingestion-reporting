"""Risk-events routes — Phase 2 reference-data browser."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.models.regulatory import RiskEvent
from app.models.reporting import AnalystNote
from app.schemas.common import PaginatedResponse, VerifiedResponse, VerifiedUpdate
from app.schemas.note import AnalystNoteCreate, AnalystNoteRead
from app.schemas.regulatory import RiskEventRead

router = APIRouter(prefix="/risk-events", tags=["risk-events"])


def _get_event_or_404(db: Session, event_id: int) -> RiskEvent:
    ev = db.get(RiskEvent, event_id)
    if ev is None:
        raise HTTPException(status_code=404, detail="Risk event not found")
    return ev


# ---------------------------------------------------------------------------
# GET /risk-events
# ---------------------------------------------------------------------------

@router.get("", response_model=PaginatedResponse[RiskEventRead])
def list_risk_events(
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
    search: Optional[str] = Query(None),
    event_type: Optional[str] = Query(None),
    severity_min: Optional[float] = Query(None, ge=0.0, le=1.0),
    date_from: Optional[datetime] = Query(None),
    date_to: Optional[datetime] = Query(None),
    _user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> PaginatedResponse[RiskEventRead]:
    # 055: confirmed cross-source duplicates are hidden from the list —
    # the canonical row carries the event.
    q = select(RiskEvent).where(RiskEvent.duplicate_of_id.is_(None))

    if search:
        q = q.where(
            or_(
                RiskEvent.title.ilike(f"%{search}%"),
                RiskEvent.summary.ilike(f"%{search}%"),
            )
        )
    if event_type:
        q = q.where(RiskEvent.event_type == event_type)
    if severity_min is not None:
        q = q.where(RiskEvent.severity_score >= severity_min)
    if date_from:
        q = q.where(RiskEvent.event_date >= date_from)
    if date_to:
        q = q.where(RiskEvent.event_date <= date_to)

    total = db.scalar(select(func.count()).select_from(q.subquery()))
    rows = db.scalars(
        q.order_by(RiskEvent.event_date.desc().nulls_last(), RiskEvent.id.desc())
        .offset((page - 1) * limit)
        .limit(limit)
    ).all()

    return PaginatedResponse(
        data=[RiskEventRead.model_validate(r) for r in rows],
        total=total or 0,
        page=page,
        limit=limit,
    )


# ---------------------------------------------------------------------------
# Notes — GET/POST /risk-events/{id}/notes
# ---------------------------------------------------------------------------

@router.get("/{event_id}/notes", response_model=list[AnalystNoteRead])
def list_risk_event_notes(
    event_id: int,
    _user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[AnalystNoteRead]:
    _get_event_or_404(db, event_id)
    rows = (
        db.execute(
            select(AnalystNote)
            .where(
                AnalystNote.entity_type == "risk_event",
                AnalystNote.entity_id == str(event_id),
            )
            .order_by(AnalystNote.created_at.desc())
        )
        .scalars()
        .all()
    )
    return [AnalystNoteRead.model_validate(n) for n in rows]


@router.post(
    "/{event_id}/notes",
    response_model=AnalystNoteRead,
    status_code=status.HTTP_201_CREATED,
)
def create_risk_event_note(
    event_id: int,
    body: AnalystNoteCreate,
    _user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> AnalystNoteRead:
    _get_event_or_404(db, event_id)
    note = AnalystNote(
        entity_type="risk_event",
        entity_id=str(event_id),
        note_type=body.note_type,
        note_text=body.note_text,
    )
    db.add(note)
    db.commit()
    db.refresh(note)
    return AnalystNoteRead.model_validate(note)


@router.patch("/{event_id}/verified", response_model=VerifiedResponse)
def set_risk_event_verified(
    event_id: int,
    body: VerifiedUpdate,
    _user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> VerifiedResponse:
    event = _get_event_or_404(db, event_id)
    event.verified = body.verified
    db.commit()
    return VerifiedResponse(verified=event.verified)
