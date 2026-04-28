"""Battery chemistries routes — Phase 2 reference-data browser.

Foundation Phase 1 additions:
  * GET    /chemistries/{id}                — composition + latest risk score
  * GET    /chemistries/{id}/risk/history   — risk score history (default 12 runs)
  * POST   /chemistries/rescore             — rescore every active chemistry
  * POST   /chemistries/{id}/rescore        — rescore a single chemistry
"""

from __future__ import annotations

from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.api.deps import get_current_user, get_db
from app.models.battery_chemistry import (
    BatteryChemistry,
    BatteryChemistryMaterial,
    ChemistryRiskScore,
)
from app.models.reporting import AnalystNote
from app.models.supply import Material
from app.schemas.chemistries import (
    BatteryChemistryRead,
    ChemistryDetailRead,
    ChemistryMaterialRead,
    ChemistryRiskScoreRead,
)
from app.schemas.common import PaginatedResponse, VerifiedResponse, VerifiedUpdate
from app.schemas.note import AnalystNoteCreate, AnalystNoteRead
from app.services.scoring.chemistry_risk import (
    rescore_all_chemistries,
    rescore_one_chemistry,
)

router = APIRouter(prefix="/chemistries", tags=["chemistries"])


def _get_chemistry_or_404(db: Session, chemistry_id: int) -> BatteryChemistry:
    chem = db.get(BatteryChemistry, chemistry_id)
    if chem is None:
        raise HTTPException(status_code=404, detail="Chemistry not found")
    return chem


def _latest_risk_score(
    db: Session, chemistry_id: int
) -> Optional[ChemistryRiskScoreRead]:
    # Prefer the most recent date, then methodology_version="2.0" (rollup) over
    # "1.0" (simple), then most recently inserted id as final tiebreaker.
    row = db.scalars(
        select(ChemistryRiskScore)
        .where(ChemistryRiskScore.battery_chemistry_id == chemistry_id)
        .order_by(
            ChemistryRiskScore.as_of_date.desc(),
            ChemistryRiskScore.methodology_version.desc(),  # "2.0" > "1.0" lexicographically
            ChemistryRiskScore.id.desc(),
        )
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
# POST /chemistries/rescore  (must be defined BEFORE /{chemistry_id} routes
# so FastAPI doesn't try to coerce "rescore" to int.)
# ---------------------------------------------------------------------------

@router.post("/rescore", response_model=list[dict])
def rescore_all(
    _user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict]:
    """Rescore every active chemistry against today's date.

    Returns one ``{slug, composite_risk_score, score_confidence}`` dict per
    successfully scored chemistry. Failures are logged inside
    ``rescore_all_chemistries`` and skipped — partial results still commit.
    """
    return rescore_all_chemistries(db, date.today())


# ---------------------------------------------------------------------------
# GET /chemistries/{id}
# ---------------------------------------------------------------------------

@router.get("/{chemistry_id}", response_model=ChemistryDetailRead)
def get_chemistry(
    chemistry_id: int,
    _user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ChemistryDetailRead:
    chem = _get_chemistry_or_404(db, chemistry_id)

    today = date.today()
    junction_rows = db.scalars(
        select(BatteryChemistryMaterial)
        .where(
            BatteryChemistryMaterial.battery_chemistry_id == chemistry_id,
            BatteryChemistryMaterial.valid_from <= today,
            (
                (BatteryChemistryMaterial.valid_to.is_(None))
                | (BatteryChemistryMaterial.valid_to >= today)
            ),
        )
        .order_by(BatteryChemistryMaterial.material_id)
    ).all()

    material_ids = [r.material_id for r in junction_rows]
    name_by_id: dict[int, str] = {}
    if material_ids:
        name_by_id = {
            mid: name
            for mid, name in db.execute(
                select(Material.id, Material.canonical_name).where(
                    Material.id.in_(material_ids)
                )
            ).all()
        }

    active_materials: list[ChemistryMaterialRead] = []
    for row in junction_rows:
        active_materials.append(
            ChemistryMaterialRead(
                id=row.id,
                material_id=row.material_id,
                material_canonical_name=name_by_id.get(row.material_id, "(unknown)"),
                role=row.role,
                intensity=row.intensity,
                is_substitutable=row.is_substitutable,
                valid_from=row.valid_from,
                valid_to=row.valid_to,
                notes=row.notes,
            )
        )

    detail = ChemistryDetailRead.model_validate(chem)
    detail.latest_risk_score = _latest_risk_score(db, chem.id)
    detail.active_materials = active_materials
    return detail


# ---------------------------------------------------------------------------
# GET /chemistries/{id}/risk/history
# ---------------------------------------------------------------------------

@router.get(
    "/{chemistry_id}/risk/history",
    response_model=list[ChemistryRiskScoreRead],
)
def get_chemistry_risk_history(
    chemistry_id: int,
    limit: int = Query(12, ge=1, le=200),
    _user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[ChemistryRiskScoreRead]:
    _get_chemistry_or_404(db, chemistry_id)
    rows = db.scalars(
        select(ChemistryRiskScore)
        .where(ChemistryRiskScore.battery_chemistry_id == chemistry_id)
        .order_by(ChemistryRiskScore.as_of_date.desc(), ChemistryRiskScore.id.desc())
        .limit(limit)
    ).all()
    return [ChemistryRiskScoreRead.model_validate(r) for r in rows]


# ---------------------------------------------------------------------------
# POST /chemistries/{id}/rescore
# ---------------------------------------------------------------------------

@router.post(
    "/{chemistry_id}/rescore",
    response_model=ChemistryRiskScoreRead,
    status_code=status.HTTP_201_CREATED,
)
def rescore_chemistry(
    chemistry_id: int,
    _user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ChemistryRiskScoreRead:
    _get_chemistry_or_404(db, chemistry_id)
    score = rescore_one_chemistry(db, chemistry_id, date.today())
    return ChemistryRiskScoreRead.model_validate(score)


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
