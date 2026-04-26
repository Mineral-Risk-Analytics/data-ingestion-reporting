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

from app.api.deps import get_current_user, get_db
from app.models.scoring import MaterialGeographyRiskScore
from app.models.supply import Material
from app.schemas.common import PaginatedResponse
from app.schemas.market_scores import (
    MaterialGeographyScoreDetail,
    MaterialGeographyScoreRead,
    RescoredResult,
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
    """Return the latest score per geography for this material."""
    _ensure_material_exists(db, material_id)
    rows = db.scalars(_latest_per_geography_for_material(material_id)).all()
    return [MaterialGeographyScoreRead.model_validate(r) for r in rows]


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
