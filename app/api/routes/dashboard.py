"""Dashboard overview route (Phase 1).

Provides the top-of-screen KPIs and two distribution charts for the
``/dashboard`` landing page.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.models.company import Company, CompanyScore
from app.models.reporting import AnalystNote
from app.schemas.dashboard import ConfidenceBucket, DashboardOverview, StageCount

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


_CONFIDENCE_BUCKETS = [
    ("0.0-0.2", 0.0, 0.2),
    ("0.2-0.4", 0.2, 0.4),
    ("0.4-0.6", 0.4, 0.6),
    ("0.6-0.8", 0.6, 0.8),
    ("0.8-1.0", 0.8, 1.0001),  # include 1.0 in the top bucket
    ("unknown", None, None),
]


@router.get("/overview", response_model=DashboardOverview)
def dashboard_overview(
    _user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> DashboardOverview:
    total = int(db.scalar(select(func.count()).select_from(Company)) or 0)

    stage_rows = db.execute(
        select(
            func.coalesce(Company.supply_chain_stage, "unknown").label("stage"),
            func.count().label("n"),
        ).group_by(Company.supply_chain_stage)
    ).all()
    stage_counts = [StageCount(stage=row[0], count=int(row[1])) for row in stage_rows]
    stage_counts.sort(key=lambda s: (-s.count, s.stage))

    bucket_case = case(
        (Company.data_confidence.is_(None), "unknown"),
        (Company.data_confidence < 0.2, "0.0-0.2"),
        (Company.data_confidence < 0.4, "0.2-0.4"),
        (Company.data_confidence < 0.6, "0.4-0.6"),
        (Company.data_confidence < 0.8, "0.6-0.8"),
        else_="0.8-1.0",
    )
    bucket_rows = db.execute(
        select(bucket_case.label("bucket"), func.count().label("n")).group_by(
            bucket_case
        )
    ).all()
    bucket_map = {row[0]: int(row[1]) for row in bucket_rows}
    confidence = [
        ConfidenceBucket(bucket=label, count=bucket_map.get(label, 0))
        for label, _, _ in _CONFIDENCE_BUCKETS
    ]

    companies_with_score = int(
        db.scalar(select(func.count(func.distinct(CompanyScore.company_id))))
        or 0
    )

    seven_days_ago = datetime.now(timezone.utc) - timedelta(days=7)
    recent_notes_7d = int(
        db.scalar(
            select(func.count()).select_from(AnalystNote).where(
                AnalystNote.created_at >= seven_days_ago
            )
        )
        or 0
    )

    return DashboardOverview(
        company_count_total=total,
        company_count_by_stage=stage_counts,
        confidence_distribution=confidence,
        companies_with_score=companies_with_score,
        recent_notes_count_7d=recent_notes_7d,
    )
