"""Materials routes — Phase 2 reference-data browser.

Includes HS-code mapping inspection and the global mismatches view that lets
analysts spot bad seed data before it corrupts trade-flow scores.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import Select, and_, func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.api.deps import get_current_user, get_db
from app.models.battery_chemistry import BatteryChemistry
from app.models.criticality_signal import MaterialCriticalitySignal
from app.models.reporting import AnalystNote
from app.models.scoring import MaterialGlobalRiskScore
from app.models.supply import HsCodeMaterialMapping, Material, MaterialProductionShare
from app.schemas.common import PaginatedResponse, VerifiedResponse, VerifiedUpdate
from app.schemas.materials import (
    CountryShareItem,
    HsMappingRead,
    HsMismatchItem,
    MappingHealth,
    MaterialDetail,
    MaterialListItem,
)
from app.schemas.note import AnalystNoteCreate, AnalystNoteRead

router = APIRouter(tags=["materials"])


# ---------------------------------------------------------------------------
# Mismatch helpers
# ---------------------------------------------------------------------------

_LOW_CONFIDENCE_THRESHOLD = 0.6


def _cross_mapped_prefixes(db: Session) -> set[str]:
    """Return hs_code_prefixes that map to more than one material."""
    rows = (
        db.execute(
            select(HsCodeMaterialMapping.hs_code_prefix)
            .group_by(HsCodeMaterialMapping.hs_code_prefix)
            .having(func.count(HsCodeMaterialMapping.id) > 1)
        )
        .scalars()
        .all()
    )
    return set(rows)


def _is_chapter_mismatch(mapping: HsCodeMaterialMapping, material: Material) -> bool:
    """True when the mapping HS chapter conflicts with the material's own hs_codes."""
    if not material.hs_codes:
        return False
    mapping_chapter = mapping.hs_code_prefix[:2]
    material_chapters = {str(code)[:2] for code in material.hs_codes}
    return bool(material_chapters) and mapping_chapter not in material_chapters


def _annotate_mapping(
    mapping: HsCodeMaterialMapping,
    material: Material,
    cross_mapped: set[str],
) -> HsMappingRead:
    """Build a ``HsMappingRead`` with all mismatch flags set."""
    is_low = mapping.confidence < _LOW_CONFIDENCE_THRESHOLD
    is_missing = not mapping.description
    is_chapter = _is_chapter_mismatch(mapping, material)
    is_cross = mapping.hs_code_prefix in cross_mapped

    out = HsMappingRead.model_validate(mapping)
    out.is_low_confidence = is_low
    out.is_missing_description = is_missing
    out.is_chapter_mismatch = is_chapter
    out.is_cross_mapped = is_cross
    return out


def _compute_health(mappings: list[HsMappingRead]) -> MappingHealth:
    low = sum(1 for m in mappings if m.is_low_confidence)
    miss = sum(1 for m in mappings if m.is_missing_description)
    chap = sum(1 for m in mappings if m.is_chapter_mismatch)
    cross = sum(1 for m in mappings if m.is_cross_mapped)
    mismatched = sum(
        1 for m in mappings if (
            m.is_low_confidence or m.is_missing_description
            or m.is_chapter_mismatch or m.is_cross_mapped
        )
    )
    return MappingHealth(
        total=len(mappings),
        mismatched=mismatched,
        low_confidence=low,
        missing_description=miss,
        chapter_mismatch=chap,
        cross_mapped=cross,
    )


def _get_material_or_404(db: Session, material_id: int) -> Material:
    mat = db.get(Material, material_id)
    if mat is None:
        raise HTTPException(status_code=404, detail="Material not found")
    return mat


# ---------------------------------------------------------------------------
# GET /materials
# ---------------------------------------------------------------------------

@router.get("/materials", response_model=PaginatedResponse[MaterialListItem])
def list_materials(
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
    search: Optional[str] = Query(None),
    category: Optional[str] = Query(None),
    is_ira_critical: Optional[bool] = Query(None),
    is_eu_crma_critical: Optional[bool] = Query(None),
    has_mismatched_mappings: Optional[bool] = Query(None),
    _user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> PaginatedResponse[MaterialListItem]:
    q: Select = select(Material)

    if search:
        q = q.where(
            or_(
                Material.canonical_name.ilike(f"%{search}%"),
                Material.symbol_or_code.ilike(f"%{search}%"),
                Material.category.ilike(f"%{search}%"),
            )
        )
    if category:
        q = q.where(Material.category == category)
    if is_ira_critical is not None:
        q = q.where(Material.is_ira_critical_mineral == is_ira_critical)
    if is_eu_crma_critical is not None:
        q = q.where(Material.is_eu_crma_critical == is_eu_crma_critical)

    total = db.scalar(select(func.count()).select_from(q.subquery()))
    materials = db.scalars(
        q.order_by(Material.canonical_name)
        .offset((page - 1) * limit)
        .limit(limit)
    ).all()

    # Per-material HS mapping counts — one bulk query for efficiency.
    material_ids = [m.id for m in materials]
    mapping_counts: dict[int, int] = {}
    mismatch_counts: dict[int, int] = {}
    if material_ids:
        cross_mapped = _cross_mapped_prefixes(db)
        count_rows = (
            db.execute(
                select(
                    HsCodeMaterialMapping.material_id,
                    func.count(HsCodeMaterialMapping.id).label("cnt"),
                )
                .where(HsCodeMaterialMapping.material_id.in_(material_ids))
                .group_by(HsCodeMaterialMapping.material_id)
            )
            .all()
        )
        for mid, cnt in count_rows:
            mapping_counts[mid] = cnt

        # Mismatch counts per material
        all_mappings = db.scalars(
            select(HsCodeMaterialMapping)
            .where(HsCodeMaterialMapping.material_id.in_(material_ids))
        ).all()
        # Group by material_id
        mat_map: dict[int, list[HsCodeMaterialMapping]] = {}
        for mapping in all_mappings:
            mat_map.setdefault(mapping.material_id, []).append(mapping)

        # Need material objects for chapter mismatch check
        mat_by_id = {m.id: m for m in materials}
        for mid, mappings_list in mat_map.items():
            mat = mat_by_id.get(mid)
            if mat is None:
                continue
            annotated = [_annotate_mapping(m, mat, cross_mapped) for m in mappings_list]
            mismatch_counts[mid] = sum(
                1 for a in annotated if (
                    a.is_low_confidence or a.is_missing_description
                    or a.is_chapter_mismatch or a.is_cross_mapped
                )
            )

    # Latest global risk score per material — one bulk query using a ranked subquery.
    global_risk_scores: dict[int, float] = {}
    if material_ids:
        latest_score_sq = (
            select(
                MaterialGlobalRiskScore.material_id,
                MaterialGlobalRiskScore.overall_risk_score,
                func.row_number()
                .over(
                    partition_by=MaterialGlobalRiskScore.material_id,
                    order_by=MaterialGlobalRiskScore.as_of_date.desc(),
                )
                .label("rn"),
            )
            .where(MaterialGlobalRiskScore.material_id.in_(material_ids))
            .subquery()
        )
        score_rows = db.execute(
            select(
                latest_score_sq.c.material_id,
                latest_score_sq.c.overall_risk_score,
            ).where(latest_score_sq.c.rn == 1)
        ).all()
        for mid, score in score_rows:
            if score is not None:
                global_risk_scores[mid] = score

    items: list[MaterialListItem] = []
    for mat in materials:
        item = MaterialListItem.model_validate(mat)
        item.hs_mapping_count = mapping_counts.get(mat.id, 0)
        item.mapping_mismatch_count = mismatch_counts.get(mat.id, 0)
        item.latest_overall_risk_score = global_risk_scores.get(mat.id)
        if has_mismatched_mappings is True and item.mapping_mismatch_count == 0:
            continue
        if has_mismatched_mappings is False and item.mapping_mismatch_count > 0:
            continue
        items.append(item)

    return PaginatedResponse(data=items, total=total or 0, page=page, limit=limit)


# ---------------------------------------------------------------------------
# GET /materials/{id}
# ---------------------------------------------------------------------------

@router.get("/materials/{material_id}", response_model=MaterialDetail)
def get_material(
    material_id: int,
    _user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> MaterialDetail:
    mat = db.scalars(
        select(Material)
        .where(Material.id == material_id)
        .options(
            selectinload(Material.criticality_signals),
            selectinload(Material.hs_mappings),
            selectinload(Material.chemistry_uses),
        )
    ).first()
    if mat is None:
        raise HTTPException(status_code=404, detail="Material not found")

    cross_mapped = _cross_mapped_prefixes(db)
    annotated_mappings = [_annotate_mapping(m, mat, cross_mapped) for m in mat.hs_mappings]
    health = _compute_health(annotated_mappings)

    # Enrich chemistry_uses with slug + name from the parent chemistry row.
    chemistry_ids = [u.battery_chemistry_id for u in mat.chemistry_uses]
    chem_by_id: dict[int, BatteryChemistry] = {}
    if chemistry_ids:
        chem_by_id = {
            c.id: c
            for c in db.scalars(
                select(BatteryChemistry).where(BatteryChemistry.id.in_(chemistry_ids))
            ).all()
        }

    from app.schemas.materials import ChemistryUseRead as _ChemUseRead  # local to avoid circular
    enriched_uses: list[_ChemUseRead] = []
    for use in mat.chemistry_uses:
        row = _ChemUseRead.model_validate(use)
        chem = chem_by_id.get(use.battery_chemistry_id)
        if chem:
            row.chemistry_slug = chem.slug
            row.chemistry_name = chem.name
        enriched_uses.append(row)

    # Production shares — latest reference_year only, sorted by share descending.
    latest_year_sq = db.scalar(
        select(func.max(MaterialProductionShare.reference_year))
        .where(MaterialProductionShare.material_id == material_id)
    )
    country_shares: list[CountryShareItem] = []
    if latest_year_sq is not None:
        share_rows = db.scalars(
            select(MaterialProductionShare)
            .where(
                MaterialProductionShare.material_id == material_id,
                MaterialProductionShare.reference_year == latest_year_sq,
            )
            .order_by(MaterialProductionShare.production_share.desc())
        ).all()
        country_shares = [
            CountryShareItem(
                code=row.country_code.upper(),
                share_pct=round(row.production_share * 100),
            )
            for row in share_rows
        ]

    detail = MaterialDetail.model_validate(mat)
    detail.hs_mappings = annotated_mappings
    detail.mapping_health = health
    detail.chemistry_uses = enriched_uses
    detail.country_production_shares = country_shares
    return detail


# ---------------------------------------------------------------------------
# GET /materials/{id}/hs-mappings
# ---------------------------------------------------------------------------

@router.get("/materials/{material_id}/hs-mappings", response_model=list[HsMappingRead])
def list_material_hs_mappings(
    material_id: int,
    min_confidence: Optional[float] = Query(None, ge=0.0, le=1.0),
    _user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[HsMappingRead]:
    mat = _get_material_or_404(db, material_id)

    q = select(HsCodeMaterialMapping).where(
        HsCodeMaterialMapping.material_id == material_id
    )
    if min_confidence is not None:
        q = q.where(HsCodeMaterialMapping.confidence >= min_confidence)

    mappings = db.scalars(q.order_by(HsCodeMaterialMapping.hs_code_prefix)).all()
    cross_mapped = _cross_mapped_prefixes(db)
    return [_annotate_mapping(m, mat, cross_mapped) for m in mappings]


# ---------------------------------------------------------------------------
# GET /hs-mappings/mismatches  (global mismatches view)
# ---------------------------------------------------------------------------

@router.get("/hs-mappings/mismatches", response_model=PaginatedResponse[HsMismatchItem])
def list_hs_mismatches(
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
    _user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> PaginatedResponse[HsMismatchItem]:
    cross_mapped = _cross_mapped_prefixes(db)

    all_mappings = db.scalars(
        select(HsCodeMaterialMapping)
        .options(selectinload(HsCodeMaterialMapping.material))
        .order_by(HsCodeMaterialMapping.confidence, HsCodeMaterialMapping.hs_code_prefix)
    ).all()

    mismatched: list[HsMismatchItem] = []
    for mapping in all_mappings:
        mat = mapping.material
        is_low = mapping.confidence < _LOW_CONFIDENCE_THRESHOLD
        is_miss = not mapping.description
        is_chap = _is_chapter_mismatch(mapping, mat)
        is_cross = mapping.hs_code_prefix in cross_mapped

        if not (is_low or is_miss or is_chap or is_cross):
            continue

        mismatched.append(
            HsMismatchItem(
                id=mapping.id,
                hs_code=mapping.hs_code_prefix,
                material_id=mat.id,
                material_canonical_name=mat.canonical_name,
                hs_description=mapping.description,
                mapping_confidence=mapping.confidence,
                is_low_confidence=is_low,
                is_missing_description=is_miss,
                is_chapter_mismatch=is_chap,
                is_cross_mapped=is_cross,
                material_url=f"/data/materials/{mat.id}",
            )
        )

    total = len(mismatched)
    start = (page - 1) * limit
    return PaginatedResponse(
        data=mismatched[start : start + limit],
        total=total,
        page=page,
        limit=limit,
    )


# ---------------------------------------------------------------------------
# Notes — GET/POST /materials/{id}/notes
# ---------------------------------------------------------------------------

@router.get("/materials/{material_id}/notes", response_model=list[AnalystNoteRead])
def list_material_notes(
    material_id: int,
    _user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[AnalystNoteRead]:
    _get_material_or_404(db, material_id)
    rows = (
        db.execute(
            select(AnalystNote)
            .where(
                AnalystNote.entity_type == "material",
                AnalystNote.entity_id == str(material_id),
            )
            .order_by(AnalystNote.created_at.desc())
        )
        .scalars()
        .all()
    )
    return [AnalystNoteRead.model_validate(n) for n in rows]


@router.post(
    "/materials/{material_id}/notes",
    response_model=AnalystNoteRead,
    status_code=status.HTTP_201_CREATED,
)
def create_material_note(
    material_id: int,
    body: AnalystNoteCreate,
    _user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> AnalystNoteRead:
    _get_material_or_404(db, material_id)
    note = AnalystNote(
        entity_type="material",
        entity_id=str(material_id),
        note_type=body.note_type,
        note_text=body.note_text,
    )
    db.add(note)
    db.commit()
    db.refresh(note)
    return AnalystNoteRead.model_validate(note)


# ---------------------------------------------------------------------------
# Notes — GET/POST /materials/{material_id}/hs-mappings/{mapping_id}/notes
# ---------------------------------------------------------------------------

def _get_hs_mapping_or_404(db: Session, mapping_id: int) -> HsCodeMaterialMapping:
    m = db.get(HsCodeMaterialMapping, mapping_id)
    if m is None:
        raise HTTPException(status_code=404, detail="HS mapping not found")
    return m


@router.get(
    "/materials/{material_id}/hs-mappings/{mapping_id}/notes",
    response_model=list[AnalystNoteRead],
)
def list_hs_mapping_notes(
    material_id: int,
    mapping_id: int,
    _user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[AnalystNoteRead]:
    _get_material_or_404(db, material_id)
    _get_hs_mapping_or_404(db, mapping_id)
    rows = (
        db.execute(
            select(AnalystNote)
            .where(
                AnalystNote.entity_type == "hs_code_material_mappings",
                AnalystNote.entity_id == str(mapping_id),
            )
            .order_by(AnalystNote.created_at.desc())
        )
        .scalars()
        .all()
    )
    return [AnalystNoteRead.model_validate(n) for n in rows]


@router.post(
    "/materials/{material_id}/hs-mappings/{mapping_id}/notes",
    response_model=AnalystNoteRead,
    status_code=status.HTTP_201_CREATED,
)
def create_hs_mapping_note(
    material_id: int,
    mapping_id: int,
    body: AnalystNoteCreate,
    _user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> AnalystNoteRead:
    _get_material_or_404(db, material_id)
    _get_hs_mapping_or_404(db, mapping_id)
    note = AnalystNote(
        entity_type="hs_code_material_mappings",
        entity_id=str(mapping_id),
        note_type=body.note_type,
        note_text=body.note_text,
    )
    db.add(note)
    db.commit()
    db.refresh(note)
    return AnalystNoteRead.model_validate(note)


# ---------------------------------------------------------------------------
# Verified flag
# ---------------------------------------------------------------------------



@router.patch("/materials/{material_id}/verified", response_model=VerifiedResponse)
def set_material_verified(
    material_id: int,
    body: VerifiedUpdate,
    _user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> VerifiedResponse:
    mat = _get_material_or_404(db, material_id)
    mat.verified = body.verified
    db.commit()
    return VerifiedResponse(verified=mat.verified)
