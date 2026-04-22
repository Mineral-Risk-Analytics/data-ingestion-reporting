"""Battery chemistries routes — Phase 2 reference-data browser."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.api.deps import get_current_user, get_db
from app.models.battery_chemistry import BatteryChemistry, ChemistryRiskScore
from app.models.reporting import AnalystNote
from app.schemas.chemistries import BatteryChemistryRead, ChemistryRiskScoreRead
from app.schemas.common import PaginatedResponse, VerifiedResponse, VerifiedUpdate
from app.schemas.note import AnalystNoteCreate, AnalystNoteRead

router = APIRouter(prefix="/chemistries", tags=["chemistries"])


def _get_chemistry_or_404(db: Session, chemistry_id: int) -> BatteryChemistry:
    chem = db.get(BatteryChemistry, chemistry_id)
    if chem is None:
        raise HTTPException(status_code=404, detail="Chemistry not found")
    return chem


def _latest_risk_score(
    db: Session, chemistry_id: int
) -> Optional[ChemistryRiskScoreRead]:
    row = db.scalars(
        select(ChemistryRiskScore)
        .where(ChemistryRiskScore.battery_chemistry_id == chemistry_id)
        .order_by(ChemistryRiskScore.as_of_date.desc())
        .limit(1)
    ).first()
    return ChemistryRiskScoreRead.model_validate(row) if row else None


# ---------------------------------------------------------------------------
# GET /chemistries
# ---------------------------------------------------------------------------

@router.get("", response_model=PaginatedResponse[BatteryChemistryRead])
def list_chemistries(
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
    search: Optional[str] = Query(None),
    _user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> PaginatedResponse[BatteryChemistryRead]:
    q = select(BatteryChemistry)

    if search:
        q = q.where(
            or_(
                BatteryChemistry.name.ilike(f"%{search}%"),
                BatteryChemistry.slug.ilike(f"%{search}%"),
                BatteryChemistry.description.ilike(f"%{search}%"),
            )
        )

    total = db.scalar(select(func.count()).select_from(q.subquery()))
    rows = db.scalars(
        q.order_by(BatteryChemistry.name)
        .offset((page - 1) * limit)
        .limit(limit)
    ).all()

    items: list[BatteryChemistryRead] = []
    for chem in rows:
        item = BatteryChemistryRead.model_validate(chem)
        item.latest_risk_score = _latest_risk_score(db, chem.id)
        items.append(item)

    return PaginatedResponse(data=items, total=total or 0, page=page, limit=limit)


# ---------------------------------------------------------------------------
# Notes — GET/POST /chemistries/{id}/notes
# ---------------------------------------------------------------------------

@router.get("/{chemistry_id}/notes", response_model=list[AnalystNoteRead])
def list_chemistry_notes(
    chemistry_id: int,
    _user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[AnalystNoteRead]:
    _get_chemistry_or_404(db, chemistry_id)
    rows = (
        db.execute(
            select(AnalystNote)
            .where(
                AnalystNote.entity_type == "battery_chemistry",
                AnalystNote.entity_id == str(chemistry_id),
            )
            .order_by(AnalystNote.created_at.desc())
        )
        .scalars()
        .all()
    )
    return [AnalystNoteRead.model_validate(n) for n in rows]


@router.post(
    "/{chemistry_id}/notes",
    response_model=AnalystNoteRead,
    status_code=status.HTTP_201_CREATED,
)
def create_chemistry_note(
    chemistry_id: int,
    body: AnalystNoteCreate,
    _user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> AnalystNoteRead:
    _get_chemistry_or_404(db, chemistry_id)
    note = AnalystNote(
        entity_type="battery_chemistry",
        entity_id=str(chemistry_id),
        note_type=body.note_type,
        note_text=body.note_text,
    )
    db.add(note)
    db.commit()
    db.refresh(note)
    return AnalystNoteRead.model_validate(note)


@router.patch("/{chemistry_id}/verified", response_model=VerifiedResponse)
def set_chemistry_verified(
    chemistry_id: int,
    body: VerifiedUpdate,
    _user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> VerifiedResponse:
    chem = _get_chemistry_or_404(db, chemistry_id)
    chem.verified = body.verified
    db.commit()
    return VerifiedResponse(verified=chem.verified)
