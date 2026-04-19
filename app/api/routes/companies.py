"""Company routes backing the admin dashboard.

All routes require a verified Clerk session (see :func:`app.api.deps.get_current_user`).
List responses use the :class:`~app.schemas.common.PaginatedResponse` envelope.
"""

from __future__ import annotations

import uuid
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import Select, and_, asc, desc, func, or_, select
from sqlalchemy.orm import Session, aliased, selectinload

from app.api.deps import get_current_user, get_db
from app.models.battery_chemistry import BatteryChemistry
from app.models.company import (
    Company,
    CompanyAlias,
    CompanyMaterialExposure,
    CompanyScore,
    CompanySupplyRelationship,
)
from app.models.facility import Facility
from app.models.regulatory import (
    CompanyRegulationExposure,
    Regulation,
    RiskEvent,
    RiskEventCompany,
)
from app.models.reporting import AnalystNote
from app.models.supply import Material
from app.models.vehicle import CompanyVehicleModel, VehicleModelChemistry
from app.schemas.common import PaginatedResponse
from app.schemas.company import (
    CompanyAliasRead,
    CompanyDetail,
    CompanyEventRead,
    CompanyListItem,
    CompanySummary,
    ExposureRead,
    FacilityRead,
    RegulationExposureRead,
    RelationshipCounterparty,
    RelationshipRead,
    RelationshipsResponse,
    VehicleModelChemistryRead,
    VehicleModelRead,
)
from app.schemas.note import AnalystNoteCreate, AnalystNoteRead
from app.services.scoring.bands import score_to_band

router = APIRouter(prefix="/companies", tags=["companies"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_company_or_404(db: Session, company_id: uuid.UUID) -> Company:
    company = db.get(Company, company_id)
    if company is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Company not found"
        )
    return company


def _latest_score_subquery() -> Any:
    """Return a subquery exposing the latest CompanyScore per company_id.

    Uses ``DISTINCT ON`` (Postgres) ordered by ``as_of_date DESC, id DESC`` so
    ties break deterministically on insertion order.
    """
    return (
        select(
            CompanyScore.company_id.label("company_id"),
            CompanyScore.overall_risk_score.label("overall_risk_score"),
            CompanyScore.as_of_date.label("as_of_date"),
        )
        .distinct(CompanyScore.company_id)
        .order_by(
            CompanyScore.company_id,
            desc(CompanyScore.as_of_date),
            desc(CompanyScore.id),
        )
        .subquery()
    )


# ---------------------------------------------------------------------------
# List + detail
# ---------------------------------------------------------------------------


@router.get("", response_model=PaginatedResponse[CompanyListItem])
def list_companies(
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=25, ge=1, le=100),
    search: Optional[str] = None,
    stage: Optional[str] = None,
    country: Optional[str] = None,
    parent_id: Optional[uuid.UUID] = None,
    min_confidence: Optional[float] = Query(default=None, ge=0.0, le=1.0),
    _user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> PaginatedResponse[CompanyListItem]:
    """Paginated, filterable company list with the latest overall score joined in."""
    parent = aliased(Company)
    latest_score = _latest_score_subquery()

    base: Select[Any] = (
        select(
            Company,
            parent.canonical_name.label("parent_company_name"),
            latest_score.c.overall_risk_score.label("latest_overall_score"),
            latest_score.c.as_of_date.label("latest_score_as_of_date"),
        )
        .outerjoin(parent, Company.parent_company_id == parent.id)
        .outerjoin(latest_score, latest_score.c.company_id == Company.id)
    )

    filters = []
    if stage:
        filters.append(Company.supply_chain_stage == stage)
    if country:
        filters.append(Company.headquarters_country == country.upper())
    if parent_id:
        filters.append(Company.parent_company_id == parent_id)
    if min_confidence is not None:
        filters.append(Company.data_confidence >= min_confidence)
    if search:
        pattern = f"%{search.strip()}%"
        alias_match = (
            select(CompanyAlias.company_id)
            .where(CompanyAlias.alias.ilike(pattern))
            .scalar_subquery()
        )
        filters.append(
            or_(
                Company.canonical_name.ilike(pattern),
                Company.legal_name.ilike(pattern),
                Company.public_ticker.ilike(pattern),
                Company.id.in_(alias_match),
            )
        )

    if filters:
        base = base.where(and_(*filters))

    total_q = select(func.count()).select_from(base.subquery())
    total = int(db.scalar(total_q) or 0)

    offset = (page - 1) * limit
    rows = db.execute(
        base.order_by(asc(Company.canonical_name)).limit(limit).offset(offset)
    ).all()

    items: list[CompanyListItem] = []
    for row in rows:
        company: Company = row[0]
        parent_name: Optional[str] = row[1]
        overall: Optional[float] = row[2]
        as_of = row[3]
        items.append(
            CompanyListItem(
                id=company.id,
                canonical_name=company.canonical_name,
                legal_name=company.legal_name,
                supply_chain_stage=company.supply_chain_stage,
                headquarters_country=company.headquarters_country,
                is_public=company.is_public,
                data_confidence=company.data_confidence,
                parent_company_id=company.parent_company_id,
                parent_company_name=parent_name,
                latest_overall_score=overall,
                latest_risk_band=score_to_band(overall),
                latest_score_as_of_date=as_of,
            )
        )

    return PaginatedResponse[CompanyListItem](
        data=items, total=total, page=page, limit=limit
    )


@router.get("/{company_id}", response_model=CompanyDetail)
def get_company(
    company_id: uuid.UUID,
    _user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CompanyDetail:
    company = db.execute(
        select(Company)
        .where(Company.id == company_id)
        .options(selectinload(Company.aliases))
    ).scalar_one_or_none()
    if company is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Company not found"
        )

    parent: Optional[CompanySummary] = None
    if company.parent_company_id:
        parent_row = db.get(Company, company.parent_company_id)
        if parent_row:
            parent = CompanySummary.model_validate(parent_row)

    return CompanyDetail(
        id=company.id,
        canonical_name=company.canonical_name,
        legal_name=company.legal_name,
        supply_chain_stage=company.supply_chain_stage,
        headquarters_country=company.headquarters_country,
        headquarters_region=company.headquarters_region,
        public_ticker=company.public_ticker,
        is_public=company.is_public,
        duns_number=company.duns_number,
        lei=company.lei,
        data_confidence=company.data_confidence,
        data_source=company.data_source,
        notes=company.notes,
        parent_company_id=company.parent_company_id,
        created_at=company.created_at,
        updated_at=company.updated_at,
        aliases=[CompanyAliasRead.model_validate(a) for a in company.aliases],
        parent=parent,
    )


# ---------------------------------------------------------------------------
# Exposures / relationships / regulations
# ---------------------------------------------------------------------------


@router.get("/{company_id}/exposures", response_model=list[ExposureRead])
def get_company_exposures(
    company_id: uuid.UUID,
    _user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[ExposureRead]:
    _get_company_or_404(db, company_id)
    rows = db.execute(
        select(CompanyMaterialExposure, Material.canonical_name)
        .join(Material, Material.id == CompanyMaterialExposure.material_id)
        .where(CompanyMaterialExposure.company_id == company_id)
        .order_by(CompanyMaterialExposure.exposure_score.desc())
    ).all()

    return [
        ExposureRead(
            id=exp.id,
            material_id=exp.material_id,
            material_name=material_name,
            supply_chain_stage=exp.supply_chain_stage,
            exposure_score=exp.exposure_score,
            source_geography=exp.source_geography,
            data_confidence=exp.data_confidence,
            rationale=exp.rationale,
            as_of_date=exp.as_of_date,
        )
        for exp, material_name in rows
    ]


@router.get("/{company_id}/relationships", response_model=RelationshipsResponse)
def get_company_relationships(
    company_id: uuid.UUID,
    _user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> RelationshipsResponse:
    _get_company_or_404(db, company_id)
    buyer = aliased(Company)
    supplier = aliased(Company)

    as_buyer_rows = db.execute(
        select(
            CompanySupplyRelationship,
            supplier,
            Material.canonical_name,
        )
        .join(supplier, supplier.id == CompanySupplyRelationship.supplier_id)
        .outerjoin(Material, Material.id == CompanySupplyRelationship.material_id)
        .where(CompanySupplyRelationship.buyer_id == company_id)
        .order_by(CompanySupplyRelationship.created_at.desc())
    ).all()

    as_supplier_rows = db.execute(
        select(
            CompanySupplyRelationship,
            buyer,
            Material.canonical_name,
        )
        .join(buyer, buyer.id == CompanySupplyRelationship.buyer_id)
        .outerjoin(Material, Material.id == CompanySupplyRelationship.material_id)
        .where(CompanySupplyRelationship.supplier_id == company_id)
        .order_by(CompanySupplyRelationship.created_at.desc())
    ).all()

    def _pack(rel: CompanySupplyRelationship, other: Company, material_name: Optional[str]) -> RelationshipRead:
        return RelationshipRead(
            id=rel.id,
            relationship_type=rel.relationship_type,
            material_id=rel.material_id,
            material_name=material_name,
            data_confidence=rel.data_confidence,
            volume_share_pct=rel.volume_share_pct,
            valid_from=rel.valid_from,
            valid_to=rel.valid_to,
            counterparty=RelationshipCounterparty(
                id=other.id,
                canonical_name=other.canonical_name,
                supply_chain_stage=other.supply_chain_stage,
            ),
        )

    return RelationshipsResponse(
        as_buyer=[_pack(r, sup, mat) for r, sup, mat in as_buyer_rows],
        as_supplier=[_pack(r, buy, mat) for r, buy, mat in as_supplier_rows],
    )


@router.get("/{company_id}/regulations", response_model=list[RegulationExposureRead])
def get_company_regulations(
    company_id: uuid.UUID,
    _user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[RegulationExposureRead]:
    _get_company_or_404(db, company_id)
    rows = db.execute(
        select(CompanyRegulationExposure, Regulation)
        .join(Regulation, Regulation.id == CompanyRegulationExposure.regulation_id)
        .where(CompanyRegulationExposure.company_id == company_id)
        .order_by(Regulation.effective_date.desc().nullslast())
    ).all()

    return [
        RegulationExposureRead(
            id=exp.id,
            regulation_id=reg.id,
            regulation_title=reg.title,
            regulation_key=reg.regulation_key,
            policy_theme=reg.policy_theme,
            effective_date=reg.effective_date,
            compliance_status=exp.compliance_status,
            exposure_reason=exp.exposure_reason,
            assessed_at=exp.assessed_at,
        )
        for exp, reg in rows
    ]


# ---------------------------------------------------------------------------
# Events / facilities / vehicle models
# ---------------------------------------------------------------------------


@router.get("/{company_id}/events", response_model=list[CompanyEventRead])
def get_company_events(
    company_id: uuid.UUID,
    limit: int = Query(default=20, ge=1, le=200),
    event_type: Optional[str] = None,
    _user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[CompanyEventRead]:
    _get_company_or_404(db, company_id)
    stmt = (
        select(RiskEvent, RiskEventCompany)
        .join(RiskEventCompany, RiskEventCompany.risk_event_id == RiskEvent.id)
        .where(RiskEventCompany.company_id == company_id)
        .order_by(desc(RiskEvent.event_date))
        .limit(limit)
    )
    if event_type:
        stmt = stmt.where(RiskEvent.event_type == event_type)

    rows = db.execute(stmt).all()
    return [
        CompanyEventRead(
            id=ev.id,
            event_type=ev.event_type,
            event_date=ev.event_date,
            title=ev.title,
            summary=ev.summary,
            severity_score=ev.severity_score,
            confidence_score=ev.confidence_score,
            relevance_score=link.relevance_score,
            match_reason=link.match_reason,
        )
        for ev, link in rows
    ]


@router.get("/{company_id}/facilities", response_model=list[FacilityRead])
def get_company_facilities(
    company_id: uuid.UUID,
    _user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[FacilityRead]:
    _get_company_or_404(db, company_id)
    rows = (
        db.execute(
            select(Facility)
            .where(Facility.company_id == company_id)
            .order_by(Facility.country, Facility.city.asc().nullslast())
        )
        .scalars()
        .all()
    )
    return [FacilityRead.model_validate(f) for f in rows]


@router.get("/{company_id}/vehicle-models", response_model=list[VehicleModelRead])
def get_company_vehicle_models(
    company_id: uuid.UUID,
    _user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[VehicleModelRead]:
    _get_company_or_404(db, company_id)
    models = (
        db.execute(
            select(CompanyVehicleModel)
            .where(CompanyVehicleModel.company_id == company_id)
            .options(selectinload(CompanyVehicleModel.chemistries))
            .order_by(
                desc(CompanyVehicleModel.model_year_start),
                CompanyVehicleModel.model_name,
            )
        )
        .scalars()
        .all()
    )
    if not models:
        return []

    chemistry_ids = {
        c.battery_chemistry_id for m in models for c in m.chemistries
    }
    chem_lookup: dict[int, str] = {}
    if chemistry_ids:
        chem_rows = db.execute(
            select(BatteryChemistry.id, BatteryChemistry.slug).where(
                BatteryChemistry.id.in_(chemistry_ids)
            )
        ).all()
        chem_lookup = {row[0]: row[1] for row in chem_rows}

    out: list[VehicleModelRead] = []
    for m in models:
        out.append(
            VehicleModelRead(
                id=m.id,
                model_name=m.model_name,
                model_year_start=m.model_year_start,
                model_year_end=m.model_year_end,
                production_volume_units=m.production_volume_units,
                production_volume_year=m.production_volume_year,
                is_active=m.is_active,
                data_source=m.data_source,
                chemistries=[
                    VehicleModelChemistryRead(
                        id=c.id,
                        battery_chemistry_id=c.battery_chemistry_id,
                        chemistry_slug=chem_lookup.get(c.battery_chemistry_id, ""),
                        share_pct=c.share_pct,
                        valid_from=c.valid_from,
                        valid_to=c.valid_to,
                        notes=c.notes,
                    )
                    for c in m.chemistries
                ],
            )
        )
    return out


# ---------------------------------------------------------------------------
# Analyst notes (Flag Issue dialog)
# ---------------------------------------------------------------------------


@router.get("/{company_id}/notes", response_model=list[AnalystNoteRead])
def get_company_notes(
    company_id: uuid.UUID,
    _user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[AnalystNoteRead]:
    _get_company_or_404(db, company_id)
    rows = (
        db.execute(
            select(AnalystNote)
            .where(
                AnalystNote.entity_type == "company",
                AnalystNote.entity_id == str(company_id),
            )
            .order_by(AnalystNote.created_at.desc())
        )
        .scalars()
        .all()
    )
    return [AnalystNoteRead.model_validate(n) for n in rows]


@router.post(
    "/{company_id}/notes",
    response_model=AnalystNoteRead,
    status_code=status.HTTP_201_CREATED,
)
def create_company_note(
    company_id: uuid.UUID,
    body: AnalystNoteCreate,
    _user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> AnalystNoteRead:
    _get_company_or_404(db, company_id)
    note = AnalystNote(
        entity_type="company",
        entity_id=str(company_id),
        note_type=body.note_type,
        note_text=body.note_text,
    )
    db.add(note)
    db.commit()
    db.refresh(note)
    return AnalystNoteRead.model_validate(note)


# ---------------------------------------------------------------------------
# Legacy route retained for scoring pipeline (Phase 3 will wrap this in a
# proper admin-guarded scoring endpoint).
# ---------------------------------------------------------------------------


@router.post("/{company_id}/rescore", status_code=202)
def trigger_rescore(
    company_id: uuid.UUID,
    _user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    from app.services.scoring.orchestrator import rescore_company

    try:
        score_row = rescore_company(db, company_id, run_id="manual")
        db.commit()
        return {"company_score_id": str(score_row.id), "status": "scored"}
    except Exception as exc:  # pragma: no cover - orchestrator surfaces messages
        db.rollback()
        raise HTTPException(status_code=500, detail=str(exc)) from exc
