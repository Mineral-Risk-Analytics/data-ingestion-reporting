"""Facilities routes — standalone Phase 2 reference-data browser.

The company-scoped ``GET /companies/{id}/facilities`` endpoint (Phase 1) is
unchanged.  These routes give analysts a global view across all companies.
"""

from __future__ import annotations

import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.models.facility import Facility
from app.models.reporting import AnalystNote
from app.schemas.common import PaginatedResponse, VerifiedResponse, VerifiedUpdate
from app.schemas.company import FacilityRead
from app.schemas.note import AnalystNoteCreate, AnalystNoteRead

router = APIRouter(prefix="/facilities", tags=["facilities"])


def _get_facility_or_404(db: Session, facility_id: uuid.UUID) -> Facility:
    f = db.get(Facility, facility_id)
    if f is None:
        raise HTTPException(status_code=404, detail="Facility not found")
    return f


# ---------------------------------------------------------------------------
# GET /facilities
# ---------------------------------------------------------------------------

@router.get("", response_model=PaginatedResponse[FacilityRead])
def list_facilities(
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
    search: Optional[str] = Query(None),
    company_id: Optional[uuid.UUID] = Query(None),
    country: Optional[str] = Query(None),
    facility_type: Optional[str] = Query(None),
    facility_status: Optional[str] = Query(None, alias="status"),
    _user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> PaginatedResponse[FacilityRead]:
    q = select(Facility)

    if search:
        q = q.where(
            or_(
                Facility.city.ilike(f"%{search}%"),
                Facility.region.ilike(f"%{search}%"),
                Facility.capacity_notes.ilike(f"%{search}%"),
            )
        )
    if company_id:
        q = q.where(Facility.company_id == company_id)
    if country:
        q = q.where(Facility.country == country.upper())
    if facility_type:
        q = q.where(Facility.facility_type == facility_type)
    if facility_status:
        q = q.where(Facility.status == facility_status)

    total = db.scalar(select(func.count()).select_from(q.subquery()))
    rows = db.scalars(
        q.order_by(Facility.country, Facility.facility_type)
        .offset((page - 1) * limit)
        .limit(limit)
    ).all()

    return PaginatedResponse(
        data=[FacilityRead.model_validate(f) for f in rows],
        total=total or 0,
        page=page,
        limit=limit,
    )


# ---------------------------------------------------------------------------
# Notes — GET/POST /facilities/{id}/notes
# ---------------------------------------------------------------------------

@router.get("/{facility_id}/notes", response_model=list[AnalystNoteRead])
def list_facility_notes(
    facility_id: uuid.UUID,
    _user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[AnalystNoteRead]:
    _get_facility_or_404(db, facility_id)
    rows = (
        db.execute(
            select(AnalystNote)
            .where(
                AnalystNote.entity_type == "facility",
                AnalystNote.entity_id == str(facility_id),
            )
            .order_by(AnalystNote.created_at.desc())
        )
        .scalars()
        .all()
    )
    return [AnalystNoteRead.model_validate(n) for n in rows]


@router.post(
    "/{facility_id}/notes",
    response_model=AnalystNoteRead,
    status_code=status.HTTP_201_CREATED,
)
def create_facility_note(
    facility_id: uuid.UUID,
    body: AnalystNoteCreate,
    _user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> AnalystNoteRead:
    _get_facility_or_404(db, facility_id)
    note = AnalystNote(
        entity_type="facility",
        entity_id=str(facility_id),
        note_type=body.note_type,
        note_text=body.note_text,
    )
    db.add(note)
    db.commit()
    db.refresh(note)
    return AnalystNoteRead.model_validate(note)


@router.patch("/{facility_id}/verified", response_model=VerifiedResponse)
def set_facility_verified(
    facility_id: uuid.UUID,
    body: VerifiedUpdate,
    _user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> VerifiedResponse:
    facility = _get_facility_or_404(db, facility_id)
    facility.verified = body.verified
    db.commit()
    return VerifiedResponse(verified=facility.verified)
