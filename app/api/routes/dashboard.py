"""Dashboard overview route (Phase 1).

Provides the top-of-screen KPIs and two distribution charts for the
``/dashboard`` landing page.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.models.company import Company, CompanyScore
from app.models.reporting import AnalystNote
from app.models.scoring import MaterialGeographyRiskScore, MaterialGlobalRiskScore
from app.models.supply import HsCodeMaterialMapping, Material, MaterialProductionShare
from app.schemas.dashboard import (
    ConfidenceBucket,
    DashboardOverview,
    PillarProgress,
    ProductionCountryItem,
    RecentNoteItem,
    ScoreRunProgress,
    StageCount,
    TopMaterialRisk,
)

_LOW_CONFIDENCE_THRESHOLD = 0.6

_PILLARS = [
    ("material_concentration_score", "Material concentration"),
    ("geopolitical_trade_score", "Geopolitical / trade"),
    ("regulatory_compliance_score", "Regulatory compliance"),
    ("operational_score", "Operational"),
    ("financial_pressure_score", "Financial pressure"),
]

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

    # Materials tracked + quarterly delta
    material_total = int(db.scalar(select(func.count()).select_from(Material)) or 0)
    now = datetime.now(timezone.utc)
    quarter_start_month = (math.ceil(now.month / 3) - 1) * 3 + 1
    quarter_start = datetime(now.year, quarter_start_month, 1, tzinfo=timezone.utc)
    material_this_quarter = int(
        db.scalar(
            select(func.count()).select_from(Material).where(
                Material.created_at >= quarter_start
            )
        )
        or 0
    )

    # Suspect mappings — mapping rows flagged low-confidence OR cross-mapped
    # (chapter mismatch excluded here; it requires Python iteration and is
    # covered in the per-material detail view)
    cross_subq = (
        select(HsCodeMaterialMapping.hs_code_prefix)
        .group_by(HsCodeMaterialMapping.hs_code_prefix)
        .having(func.count(HsCodeMaterialMapping.id) > 1)
    )
    suspect_count = int(
        db.scalar(
            select(func.count()).select_from(HsCodeMaterialMapping).where(
                or_(
                    HsCodeMaterialMapping.confidence < _LOW_CONFIDENCE_THRESHOLD,
                    HsCodeMaterialMapping.hs_code_prefix.in_(cross_subq),
                )
            )
        )
        or 0
    )

    # Distinct entities with notes in last 7 days
    entity_count_7d = int(
        db.scalar(
            select(
                func.count(
                    func.distinct(
                        func.concat(AnalystNote.entity_type, ":", AnalystNote.entity_id)
                    )
                )
            ).select_from(AnalystNote).where(
                AnalystNote.created_at >= seven_days_ago
            )
        )
        or 0
    )

    # ------------------------------------------------------------------
    # Top materials by risk (latest global score, top 5)
    # ------------------------------------------------------------------
    top_materials: list[TopMaterialRisk] = []
    try:
        latest_global_subq = (
            select(MaterialGlobalRiskScore)
            .distinct(MaterialGlobalRiskScore.material_id)
            .order_by(
                MaterialGlobalRiskScore.material_id,
                MaterialGlobalRiskScore.as_of_date.desc(),
                MaterialGlobalRiskScore.id.desc(),
            )
        ).subquery()

        top_rows = db.execute(
            select(
                latest_global_subq.c.material_id,
                latest_global_subq.c.overall_risk_score,
                latest_global_subq.c.as_of_date,
                Material.canonical_name,
                Material.symbol_or_code,
                Material.category,
                Material.patent_occurrence_trend,
            )
            .join(Material, Material.id == latest_global_subq.c.material_id)
            .where(latest_global_subq.c.overall_risk_score.isnot(None))
            .order_by(latest_global_subq.c.overall_risk_score.desc())
            .limit(5)
        ).all()

        # Production shares for these materials
        top_mat_ids = [r.material_id for r in top_rows]
        prod_shares: dict[int, list[ProductionCountryItem]] = {}
        if top_mat_ids:
            latest_yr_subq = (
                select(
                    MaterialProductionShare.material_id,
                    func.max(MaterialProductionShare.reference_year).label("yr"),
                )
                .where(MaterialProductionShare.material_id.in_(top_mat_ids))
                .group_by(MaterialProductionShare.material_id)
            ).subquery()

            share_rows = db.execute(
                select(
                    MaterialProductionShare.material_id,
                    MaterialProductionShare.country_code,
                    MaterialProductionShare.production_share,
                )
                .join(
                    latest_yr_subq,
                    and_(
                        MaterialProductionShare.material_id == latest_yr_subq.c.material_id,
                        MaterialProductionShare.reference_year == latest_yr_subq.c.yr,
                    ),
                )
                .order_by(
                    MaterialProductionShare.material_id,
                    MaterialProductionShare.production_share.desc(),
                )
            ).all()

            for mid, code, share in share_rows:
                if mid not in prod_shares:
                    prod_shares[mid] = []
                if len(prod_shares[mid]) < 3:
                    prod_shares[mid].append(
                        ProductionCountryItem(
                            code=code, share_pct=round(share * 100)
                        )
                    )

        for r in top_rows:
            top_materials.append(
                TopMaterialRisk(
                    material_id=r.material_id,
                    canonical_name=r.canonical_name,
                    symbol_or_code=r.symbol_or_code,
                    category=r.category,
                    overall_risk_score=round(r.overall_risk_score, 1),
                    top_countries=prod_shares.get(r.material_id, []),
                    trend=r.patent_occurrence_trend,
                    as_of_date=r.as_of_date.isoformat() if r.as_of_date else "",
                )
            )
    except Exception:
        pass  # Gracefully degrade if scoring tables are empty

    # ------------------------------------------------------------------
    # Score-run progress
    # ------------------------------------------------------------------
    score_run: ScoreRunProgress | None = None
    try:
        last_run_date = db.scalar(
            select(func.max(MaterialGeographyRiskScore.as_of_date))
        )
        if last_run_date:
            # All scored pairs in the last run
            run_stats = db.execute(
                select(
                    func.count(func.distinct(MaterialGeographyRiskScore.geography_code)).label("geos"),
                    func.count(func.distinct(MaterialGeographyRiskScore.material_id)).label("mats"),
                    func.count().label("total_pairs"),
                ).where(MaterialGeographyRiskScore.as_of_date == last_run_date)
            ).one()

            # Pairs with actual event signal (event_count > 0 means real data drove the score)
            valid_stats = db.execute(
                select(
                    func.count(func.distinct(MaterialGeographyRiskScore.geography_code)).label("geos"),
                    func.count(func.distinct(MaterialGeographyRiskScore.material_id)).label("mats"),
                ).where(
                    MaterialGeographyRiskScore.as_of_date == last_run_date,
                    MaterialGeographyRiskScore.event_count > 0,
                )
            ).one()

            total_geos = int(
                db.scalar(
                    select(func.count(func.distinct(MaterialGeographyRiskScore.geography_code)))
                ) or 0
            )
            total_pairs = max(run_stats.total_pairs, 1)

            pillar_progress: list[PillarProgress] = []
            for col_attr, label in _PILLARS:
                col = getattr(MaterialGeographyRiskScore, col_attr)
                # Non-null (column was populated at all)
                computed = int(
                    db.scalar(
                        select(func.count())
                        .select_from(MaterialGeographyRiskScore)
                        .where(
                            MaterialGeographyRiskScore.as_of_date == last_run_date,
                            col.isnot(None),
                        )
                    ) or 0
                )
                # Score > 0 (actual data signal, not a floor value)
                with_signal = int(
                    db.scalar(
                        select(func.count())
                        .select_from(MaterialGeographyRiskScore)
                        .where(
                            MaterialGeographyRiskScore.as_of_date == last_run_date,
                            col > 0,
                        )
                    ) or 0
                )
                pillar_progress.append(
                    PillarProgress(
                        name=col_attr,
                        label=label,
                        signal_pct=round(with_signal / total_pairs * 100),
                        computed_pct=round(computed / total_pairs * 100),
                    )
                )

            score_run = ScoreRunProgress(
                last_run_date=last_run_date.isoformat(),
                valid_geographies=int(valid_stats.geos),
                valid_materials=int(valid_stats.mats),
                scored_geographies=int(run_stats.geos),
                scored_materials=int(run_stats.mats),
                total_geographies=total_geos,
                total_materials=material_total,
                pillars=pillar_progress,
            )
    except Exception:
        pass

    # ------------------------------------------------------------------
    # Recent analyst activity (last 7 notes, enriched with entity names)
    # ------------------------------------------------------------------
    recent_activity: list[RecentNoteItem] = []
    try:
        note_rows = db.scalars(
            select(AnalystNote)
            .where(AnalystNote.created_at >= seven_days_ago)
            .order_by(AnalystNote.created_at.desc())
            .limit(7)
        ).all()

        # Resolve material names for material-type notes
        mat_note_ids = [int(n.entity_id) for n in note_rows if n.entity_type == "material"]
        mat_name_map: dict[int, str] = {}
        if mat_note_ids:
            for m in db.scalars(
                select(Material).where(Material.id.in_(mat_note_ids))
            ).all():
                mat_name_map[m.id] = m.canonical_name

        for note in note_rows:
            entity_name: str | None = None
            if note.entity_type == "material":
                entity_name = mat_name_map.get(int(note.entity_id))
            recent_activity.append(
                RecentNoteItem(
                    entity_type=note.entity_type,
                    entity_id=note.entity_id,
                    entity_name=entity_name,
                    note_type=note.note_type,
                    note_text=note.note_text,
                    created_at=note.created_at.isoformat(),
                )
            )
    except Exception:
        pass

    return DashboardOverview(
        company_count_total=total,
        company_count_by_stage=stage_counts,
        confidence_distribution=confidence,
        companies_with_score=companies_with_score,
        recent_notes_count_7d=recent_notes_7d,
        material_count_total=material_total,
        material_count_this_quarter=material_this_quarter,
        suspect_mappings_count=suspect_count,
        recent_notes_entity_count_7d=entity_count_7d,
        top_materials_by_risk=top_materials,
        score_run_progress=score_run,
        recent_activity=recent_activity,
    )
