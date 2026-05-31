"""Regulations routes — Phase 2 reference-data browser."""

from __future__ import annotations

from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.api.deps import get_current_user, get_db
from app.models.regulatory import Regulation
from app.models.reporting import AnalystNote
from app.schemas.common import PaginatedResponse, VerifiedResponse, VerifiedUpdate
from app.schemas.note import AnalystNoteCreate, AnalystNoteRead
from app.schemas.regulatory import RegulationDetail, RegulationRead

router = APIRouter(prefix="/regulations", tags=["regulations"])


def _get_regulation_or_404(db: Session, regulation_id: int) -> Regulation:
    reg = db.get(Regulation, regulation_id)
    if reg is None:
        raise HTTPException(status_code=404, detail="Regulation not found")
    return reg


# ---------------------------------------------------------------------------
# GET /regulations
# ---------------------------------------------------------------------------

@router.get("", response_model=PaginatedResponse[RegulationRead])
def list_regulations(
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
    search: Optional[str] = Query(None),
    status_filter: Optional[str] = Query(None, alias="status"),
    policy_theme: Optional[str] = Query(None),
    issuing_body: Optional[str] = Query(None),
    effective_after: Optional[date] = Query(None),
    _user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> PaginatedResponse[RegulationRead]:
    q = select(Regulation)

    if search:
        q = q.where(
            or_(
                Regulation.title.ilike(f"%{search}%"),
                Regulation.regulation_key.ilike(f"%{search}%"),
                Regulation.summary.ilike(f"%{search}%"),
            )
        )
    if status_filter:
        q = q.where(Regulation.status == status_filter)
    if policy_theme:
        q = q.where(Regulation.policy_theme.ilike(f"%{policy_theme}%"))
    if issuing_body:
        q = q.where(Regulation.issuing_body.ilike(f"%{issuing_body}%"))
    if effective_after:
        q = q.where(Regulation.effective_date >= effective_after)

    total = db.scalar(select(func.count()).select_from(q.subquery()))
    rows = db.scalars(
        q.order_by(Regulation.effective_date.desc().nulls_last(), Regulation.id.desc())
        .offset((page - 1) * limit)
        .limit(limit)
    ).all()

    return PaginatedResponse(
        data=[RegulationRead.model_validate(r) for r in rows],
        total=total or 0,
        page=page,
        limit=limit,
    )


# ---------------------------------------------------------------------------
# GET /regulations/{id}
# ---------------------------------------------------------------------------

@router.get("/{regulation_id}", response_model=RegulationDetail)
def get_regulation(
    regulation_id: int,
    _user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> RegulationDetail:
    reg = db.scalars(
        select(Regulation)
        .where(Regulation.id == regulation_id)
        .options(
            selectinload(Regulation.material_scopes),
            selectinload(Regulation.geography_scopes),
        )
    ).first()
    if reg is None:
        raise HTTPException(status_code=404, detail="Regulation not found")
    return RegulationDetail.model_validate(reg)


# ---------------------------------------------------------------------------
# Notes — GET/POST /regulations/{id}/notes
# ---------------------------------------------------------------------------

@router.get("/{regulation_id}/notes", response_model=list[AnalystNoteRead])
def list_regulation_notes(
    regulation_id: int,
    _user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[AnalystNoteRead]:
    _get_regulation_or_404(db, regulation_id)
    rows = (
        db.execute(
            select(AnalystNote)
            .where(
                AnalystNote.entity_type == "regulation",
                AnalystNote.entity_id == str(regulation_id),
            )
            .order_by(AnalystNote.created_at.desc())
        )
        .scalars()
        .all()
    )
    return [AnalystNoteRead.model_validate(n) for n in rows]


@router.post(
    "/{regulation_id}/notes",
    response_model=AnalystNoteRead,
    status_code=status.HTTP_201_CREATED,
)
def create_regulation_note(
    regulation_id: int,
    body: AnalystNoteCreate,
    _user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> AnalystNoteRead:
    _get_regulation_or_404(db, regulation_id)
    note = AnalystNote(
        entity_type="regulation",
        entity_id=str(regulation_id),
        note_type=body.note_type,
        note_text=body.note_text,
    )
    db.add(note)
    db.commit()
    db.refresh(note)
    return AnalystNoteRead.model_validate(note)


@router.patch("/{regulation_id}/verified", response_model=VerifiedResponse)
def set_regulation_verified(
    regulation_id: int,
    body: VerifiedUpdate,
    _user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> VerifiedResponse:
    reg = _get_regulation_or_404(db, regulation_id)
    reg.verified = body.verified
    db.commit()
    return VerifiedResponse(verified=reg.verified)
