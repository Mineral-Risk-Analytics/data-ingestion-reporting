"""Market intelligence scores — material × geography risk surface.

Foundation Phase 2 routes. All endpoints read from
``material_geography_risk_scores`` via the ``MaterialGeographyRiskScore`` ORM
model. Scoring runs are owned by ``app.services.scoring.market_aggregator``;
this router exposes them.

Latest-per-group queries use Postgres ``DISTINCT ON`` for the
"latest score per geography per material" lookups so the database does the
heavy lifting instead of pulling the full history into Python.

Transaction discipline
----------------------
``score_all_active_materials()`` commits inside its own per-pair loop and
rolls back individual failures; the route therefore does NOT wrap the call
in an outer transaction or call ``db.commit()`` afterwards.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from datetime import datetime, timedelta, timezone

from app.api.deps import get_current_user, get_db
from app.models.facility import Facility, FacilityMaterialLink
from app.models.regulatory import RiskEvent, RiskEventGeography, RiskEventMaterial
from app.models.scoring import MaterialGeographyRiskScore, MaterialGlobalRiskScore
from app.models.supply import Material, MaterialProductionShare
from app.schemas.common import PaginatedResponse
from app.schemas.market_scores import (
    EvidenceFacilityItem,
    EvidenceRegulationItem,
    EvidenceRiskEventItem,
    MarketScoreEvidence,
    MaterialGeographyScoreDetail,
    MaterialGeographyScoreRead,
    MaterialGlobalScoreRead,
    RescoredResult,
)
from app.services.scoring.evidence_query import (
    RISK_EVENT_EVIDENCE_WINDOW_DAYS,
    get_evidence_for_material_x_geography,
)
from app.services.scoring.market_aggregator import score_all_active_materials

router = APIRouter(tags=["market-scores"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ensure_material_exists(db: Session, material_id: int) -> None:
    if db.get(Material, material_id) is None:
        raise HTTPException(status_code=404, detail="Material not found")


def _latest_per_geography_for_material(material_id: int) -> Select:
    """Latest ``MaterialGeographyRiskScore`` per geography for one material."""
    return (
        select(MaterialGeographyRiskScore)
        .where(MaterialGeographyRiskScore.material_id == material_id)
        .distinct(MaterialGeographyRiskScore.geography_code)
        .order_by(
            MaterialGeographyRiskScore.geography_code,
            MaterialGeographyRiskScore.as_of_date.desc(),
            MaterialGeographyRiskScore.id.desc(),
        )
    )


def _latest_per_pair() -> Select:
    """Latest ``MaterialGeographyRiskScore`` per (material_id, geography_code)."""
    return (
        select(MaterialGeographyRiskScore)
        .distinct(
            MaterialGeographyRiskScore.material_id,
            MaterialGeographyRiskScore.geography_code,
        )
        .order_by(
            MaterialGeographyRiskScore.material_id,
            MaterialGeographyRiskScore.geography_code,
            MaterialGeographyRiskScore.as_of_date.desc(),
            MaterialGeographyRiskScore.id.desc(),
        )
    )


# ---------------------------------------------------------------------------
# GET /materials/{material_id}/global-score
# ---------------------------------------------------------------------------

@router.get(
    "/materials/{material_id}/global-score",
    response_model=MaterialGlobalScoreRead,
)
def get_material_global_score(
    material_id: int,
    _user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> MaterialGlobalScoreRead:
    """Return the most recent trade-flow-weighted global rollup for this material.

    This is the score that feeds chemistry risk scoring — one row per material
    per scoring date. Returns 404 when no rollup has been computed yet.
    """
    _ensure_material_exists(db, material_id)
    row = db.scalars(
        select(MaterialGlobalRiskScore)
        .where(MaterialGlobalRiskScore.material_id == material_id)
        .order_by(
            MaterialGlobalRiskScore.as_of_date.desc(),
            MaterialGlobalRiskScore.id.desc(),
        )
        .limit(1)
    ).first()
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"No global score for material {material_id}. "
                "Trigger a rescore via POST /market/rescore."
            ),
        )
    return MaterialGlobalScoreRead.model_validate(row)


# ---------------------------------------------------------------------------
# GET /materials/{material_id}/market-scores
# ---------------------------------------------------------------------------

@router.get(
    "/materials/{material_id}/market-scores",
    response_model=list[MaterialGeographyScoreRead],
)
def list_material_market_scores(
    material_id: int,
    _user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[MaterialGeographyScoreRead]:
    """Return the latest score per geography for this material.

    Enriched (2026-05-11) with per-country ``production_share_pct`` (latest
    ``reference_year`` from ``material_production_shares``) and
    ``facility_count`` (rows in ``facility_material_links`` joined to
    facilities located in that country).  The frontend uses these to filter
    the Country Scores table down to countries the material has real exposure
    to, rather than rendering every geography that was scored.
    """
    _ensure_material_exists(db, material_id)
    rows = db.scalars(_latest_per_geography_for_material(material_id)).all()

    # --- Production shares: country_code -> pct (latest reference_year only) ---
    latest_year_row = db.execute(
        select(func.max(MaterialProductionShare.reference_year)).where(
            MaterialProductionShare.material_id == material_id
        )
    ).first()
    latest_year = latest_year_row[0] if latest_year_row else None

    share_pct_by_country: dict[str, int] = {}
    if latest_year is not None:
        share_rows = db.execute(
            select(
                MaterialProductionShare.country_code,
                MaterialProductionShare.production_share,
            ).where(
                MaterialProductionShare.material_id == material_id,
                MaterialProductionShare.reference_year == latest_year,
            )
        ).all()
        for cc, share in share_rows:
            if cc is None or share is None:
                continue
            # production_share is a 0.0–1.0 fraction; surface as integer percent
            # so the UI can render "12%" without further math.  Round to nearest
            # integer; values < 0.5% will display as 0% (acceptable — those
            # countries aren't material exposure anyway).
            share_pct_by_country[cc.upper()] = int(round(share * 100))

    # --- Facility counts: country (ISO2) -> count of FacilityMaterialLink rows ---
    facility_count_rows = db.execute(
        select(Facility.country, func.count(FacilityMaterialLink.id))
        .join(FacilityMaterialLink, FacilityMaterialLink.facility_id == Facility.id)
        .where(FacilityMaterialLink.material_id == material_id)
        .group_by(Facility.country)
    ).all()
    facility_count_by_country: dict[str, int] = {
        country.upper(): count for country, count in facility_count_rows if country
    }

    # --- Live intersection event count: country -> count of distinct events
    # tagged to BOTH this material AND that country across ALL risk categories.
    #
    # Backfills the table's Events column with the analyst-facing intersection
    # count so it matches what the dropdown evidence drill-down surfaces.
    # The persisted MaterialGeographyRiskScore.event_count_geo_specific is
    # populated at scoring time (now also fixed to the all-category
    # intersection, see market_aggregator.py 2026-06-14 change) but stale rows
    # from prior scoring runs reflect the narrower trade+operational
    # intersection.  Overlay with the live count so the dashboard is honest
    # immediately, without waiting for a full rescore.
    #
    # Date filter matches RISK_EVENT_EVIDENCE_WINDOW_DAYS used by the
    # dropdown's evidence query (730 days, mirroring the longest finite
    # EVIDENCE_WINDOWS entry) so the column and dropdown counts agree.
    # Null-date events (sanctions programmes without a single anchor date)
    # are kept on both sides.
    cutoff = datetime.now(timezone.utc).date() - timedelta(
        days=RISK_EVENT_EVIDENCE_WINDOW_DAYS
    )
    intersection_count_rows = db.execute(
        select(
            RiskEventGeography.country_code,
            func.count(RiskEventMaterial.risk_event_id.distinct()),
        )
        .join(
            RiskEventMaterial,
            RiskEventMaterial.risk_event_id == RiskEventGeography.risk_event_id,
        )
        .join(
            RiskEvent,
            RiskEvent.id == RiskEventMaterial.risk_event_id,
        )
        .where(
            RiskEventMaterial.material_id == material_id,
            RiskEvent.duplicate_of_id.is_(None),  # 055
            RiskEvent.triage_status != "rejected",  # 066: soft-dismissed, hidden everywhere
            RiskEventMaterial.is_direct.is_(True),  # 056
            ((RiskEvent.event_date >= cutoff) | (RiskEvent.event_date.is_(None))),
        )
        .group_by(RiskEventGeography.country_code)
    ).all()
    intersection_count_by_country: dict[str, int] = {
        (cc or "").upper(): int(cnt) for cc, cnt in intersection_count_rows if cc
    }

    out: list[MaterialGeographyScoreRead] = []
    for r in rows:
        item = MaterialGeographyScoreRead.model_validate(r)
        cc = (r.geography_code or "").upper()
        item.production_share_pct = share_pct_by_country.get(cc)
        item.facility_count = facility_count_by_country.get(cc, 0)
        # Overlay live intersection count when available — covers the gap
        # between the persisted score and current event ingestion state.
        # Keep the persisted value as a fallback for the (rare) case where
        # the read-side query returns no row (zero events for this country).
        live_count = intersection_count_by_country.get(cc)
        if live_count is not None:
            item.event_count_geo_specific = live_count
        out.append(item)
    return out


# ---------------------------------------------------------------------------
# GET /materials/{material_id}/market-scores/{geography_code}
# ---------------------------------------------------------------------------

@router.get(
    "/materials/{material_id}/market-scores/{geography_code}",
    response_model=MaterialGeographyScoreDetail,
)
def get_material_market_score_detail(
    material_id: int,
    geography_code: str,
    _user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> MaterialGeographyScoreDetail:
    """Return the latest score for one (material, geography) pair with full rationale."""
    _ensure_material_exists(db, material_id)
    geography_code = geography_code.upper()

    row = db.scalars(
        select(MaterialGeographyRiskScore)
        .where(
            MaterialGeographyRiskScore.material_id == material_id,
            MaterialGeographyRiskScore.geography_code == geography_code,
        )
        .order_by(
            MaterialGeographyRiskScore.as_of_date.desc(),
            MaterialGeographyRiskScore.id.desc(),
        )
        .limit(1)
    ).first()
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"No market score for material {material_id} × {geography_code}. "
                "Trigger a rescore via POST /market/rescore."
            ),
        )
    return MaterialGeographyScoreDetail.model_validate(row)


# ---------------------------------------------------------------------------
# GET /materials/{material_id}/market-scores/{geography_code}/evidence
# ---------------------------------------------------------------------------

@router.get(
    "/materials/{material_id}/market-scores/{geography_code}/evidence",
    response_model=MarketScoreEvidence,
)
def get_material_market_score_evidence(
    material_id: int,
    geography_code: str,
    _user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> MarketScoreEvidence:
    """Raw evidence — regulations, facilities, risk events — at the strict
    intersection of (material × country).

    Backs the per-country dropdown drill-down on the material detail page.
    Returns the same regulations / facilities / events the scoring engine
    saw when computing this (material, country) pair's pillar scores,
    constrained to rows tagged to BOTH dimensions so the analyst sees
    country-specific evidence rather than "global material" events that
    happen to fall into the country's pillar via the union path.

    See ``evidence_query.get_evidence_for_material_x_geography`` for the
    full membership semantics + ordering rules.
    """
    _ensure_material_exists(db, material_id)
    cc = geography_code.upper()

    bundle = get_evidence_for_material_x_geography(db, material_id, cc)

    regulations = [
        EvidenceRegulationItem(
            id=r.id,
            regulation_key=r.regulation_key,
            title=r.title,
            issuing_body=r.issuing_body,
            status=r.status,
            effective_date=r.effective_date,
            summary=r.summary,
            material_scope_type=getattr(r, "material_scope_type", "covered"),
            geography_scope_type=getattr(r, "geography_scope_type", "jurisdiction"),
            geography_compliance_weight=(
                (r.geography_compliance_weights or {}).get(cc)
                if r.geography_compliance_weights
                else None
            ),
        )
        for r in bundle.regulations
    ]

    facilities = [
        EvidenceFacilityItem(
            id=str(f.id),
            name=f.name,
            facility_type=f.facility_type,
            status=f.status,
            region=f.region,
            city=f.city,
            capacity_notes=f.capacity_notes,
            is_primary_product=getattr(f, "link_is_primary_product", True),
            annual_capacity_tpy=getattr(f, "link_annual_capacity_tpy", None),
            supply_chain_stage=getattr(f, "link_supply_chain_stage", None),
        )
        for f in bundle.facilities
    ]

    risk_events = [
        EvidenceRiskEventItem(
            id=ev.id,
            title=ev.title,
            event_type=ev.event_type,
            event_subtype=ev.event_subtype,
            severity_score=ev.severity_score,
            confidence_score=ev.confidence_score,
            event_date=ev.event_date.date() if ev.event_date else None,
            summary=ev.summary,
            source_system=getattr(ev, "source_system", None),
        )
        for ev in bundle.risk_events
    ]

    return MarketScoreEvidence(
        material_id=material_id,
        geography_code=cc,
        regulations=regulations,
        facilities=facilities,
        risk_events=risk_events,
        regulation_total=bundle.regulation_total,
        facility_total=bundle.facility_total,
        risk_event_total=bundle.risk_event_total,
        risk_event_broad_total=bundle.risk_event_broad_total,
        risk_event_window_days=RISK_EVENT_EVIDENCE_WINDOW_DAYS,
    )


# ---------------------------------------------------------------------------
# GET /market/scores
# ---------------------------------------------------------------------------

@router.get("/market/scores", response_model=PaginatedResponse[MaterialGeographyScoreRead])
def browse_market_scores(
    material_id: Optional[int] = Query(None),
    geography_code: Optional[str] = Query(None),
    min_overall: Optional[float] = Query(None, ge=0.0, le=100.0),
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
    _user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> PaginatedResponse[MaterialGeographyScoreRead]:
    """Latest score per (material, geography) pair, filtered and paginated."""
    base = _latest_per_pair()
    if material_id is not None:
        base = base.where(MaterialGeographyRiskScore.material_id == material_id)
    if geography_code is not None:
        base = base.where(
            MaterialGeographyRiskScore.geography_code == geography_code.upper()
        )

    # Wrap as subquery so DISTINCT ON results can be filtered by the
    # post-aggregation min_overall threshold and counted.
    subq = base.subquery()

    filtered = select(MaterialGeographyRiskScore).join(
        subq, MaterialGeographyRiskScore.id == subq.c.id
    )
    if min_overall is not None:
        filtered = filtered.where(
            MaterialGeographyRiskScore.overall_risk_score >= min_overall
        )

    total = db.scalar(select(func.count()).select_from(filtered.subquery()))
    rows = db.scalars(
        filtered.order_by(
            MaterialGeographyRiskScore.overall_risk_score.desc().nullslast(),
            MaterialGeographyRiskScore.material_id,
            MaterialGeographyRiskScore.geography_code,
        )
        .offset((page - 1) * limit)
        .limit(limit)
    ).all()

    return PaginatedResponse(
        data=[MaterialGeographyScoreRead.model_validate(r) for r in rows],
        total=total or 0,
        page=page,
        limit=limit,
    )


# ---------------------------------------------------------------------------
# POST /market/rescore
# ---------------------------------------------------------------------------

@router.post("/market/rescore", response_model=RescoredResult)
def rescore_market(
    _user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> RescoredResult:
    """Rescore every active material × geography pair against today's date.

    Synchronous for now — runtime scales with event volume × geography fan-out.
    Migrate to a background job once batch runtimes exceed acceptable request
    timeouts. ``score_all_active_materials`` commits per pair internally;
    the route deliberately does not open a wrapping transaction.
    """
    today = date.today()
    run_id = f"market-rescore-{uuid.uuid4()}"
    results = score_all_active_materials(db, today, run_id=run_id)
    return RescoredResult(scored=len(results), as_of_date=today, run_id=run_id)
