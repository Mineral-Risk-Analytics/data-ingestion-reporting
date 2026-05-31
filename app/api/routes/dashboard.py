"""Dashboard overview route (Phase 1).

Provides the top-of-screen KPIs and two distribution charts for the
``/dashboard`` landing page.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends
from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.models.company import Company, CompanyScore
from app.models.documents import SourceDocument
from app.models.facility import FacilityMaterialLink
from app.models.regulatory import RiskEvent, RiskEventMaterial
from app.models.reporting import AnalystNote
from app.models.scoring import MaterialGeographyRiskScore, MaterialGlobalRiskScore
from app.models.source import Source
from app.models.supply import HsCodeMaterialMapping, Material, MaterialProductionShare
from app.schemas.dashboard import (
    ConfidenceBucket,
    CoreMineralsScored,
    CoverageGapItem,
    CoverageGaps,
    CoverageMatrix,
    CoverageMatrixPillar,
    CoverageMatrixRow,
    CoverageMatrixSourceCount,
    DashboardOverview,
    PillarCoverageStat,
    PillarProgress,
    ProductionCountryItem,
    RecentNoteItem,
    RecentRiskEvents30d,
    ScoreRunProgress,
    SourceCount,
    StageCount,
    TopMaterialRisk,
)
from app.services.scoring.launch_list import LAUNCH_LIST_CANONICAL_NAMES

_LOW_CONFIDENCE_THRESHOLD = 0.6

_PILLARS = [
    ("material_concentration_score", "Material concentration"),
    ("geopolitical_trade_score", "Geopolitical / trade"),
    ("regulatory_compliance_score", "Regulatory compliance"),
    ("operational_score", "Operational"),
    ("financial_pressure_score", "Financial pressure"),
]


# ---------------------------------------------------------------------------
# Coverage-gap thresholds (analyst-view dashboard, 2026-05-11)
# ---------------------------------------------------------------------------
# A launch-list mineral is flagged as a coverage gap if ANY of these are true:
#
#   no_global_score   — no MaterialGlobalRiskScore row exists
#   stale_score       — latest as_of_date is older than this many days
#   thin_events       — fewer than N risk events in the last 90 days
#   thin_pillars      — fewer than this many pillars have non-fallback signal
#                       at the latest score (pillar > 0)
#
# Calibrated to surface launch-blockers without flagging materials that are
# legitimately quiet.  Phosphate currently fails the thin_events check (no
# facility coverage yet), which is the intended behaviour — surfacing it for
# follow-up rather than papering over it.

_GAP_STALE_DAYS = 30
_GAP_THIN_EVENTS_WINDOW_DAYS = 90
_GAP_THIN_EVENTS_MIN = 5
_GAP_THIN_PILLARS_MIN = 3

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
    # Launch-list-centric KPIs (2026-05-11 analyst-view dashboard)
    # ------------------------------------------------------------------
    core_minerals_scored = _compute_core_minerals_scored(db)
    recent_risk_events = _compute_recent_risk_events_30d(db, now=now)
    coverage_gaps = _compute_coverage_gaps(db, now=now)

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

        # ── Risk-score trend per top material (2026-05-11; option C) ───
        # Compare each material's latest MaterialGlobalRiskScore.overall
        # to the most recent prior snapshot at least 7 days older.
        # Calibration shared with Materials detail Overview (see
        # ``_compute_score_trend`` in ``app/api/routes/materials.py``).
        from app.api.routes.materials import (
            _compute_score_trend as _mat_score_trend,
            _SCORE_TREND_LOOKBACK_DAYS as _mat_lookback_days,
            _SCORE_TREND_MAX_LOOKBACK_DAYS as _mat_max_lookback_days,
        )

        score_trend_by_mat: dict[int, Optional[str]] = {}
        if top_mat_ids:
            # Build a current_score / current_as_of_date map from
            # top_rows; we already have these in memory.
            cur_score_by_mat: dict[int, tuple[float, date]] = {
                r.material_id: (r.overall_risk_score, r.as_of_date)
                for r in top_rows
            }
            # One query: for each material, the most recent score
            # at least _mat_lookback_days older than the current
            # snapshot AND not older than _mat_max_lookback_days.
            #
            # Composite where-clause is per-row but expressed via
            # row_number() ranking so we get exactly one row per
            # material when there is one.
            prior_subq = (
                select(
                    MaterialGlobalRiskScore.material_id,
                    MaterialGlobalRiskScore.overall_risk_score,
                    MaterialGlobalRiskScore.as_of_date,
                    func.row_number()
                    .over(
                        partition_by=MaterialGlobalRiskScore.material_id,
                        order_by=MaterialGlobalRiskScore.as_of_date.desc(),
                    )
                    .label("rn"),
                )
                .where(
                    MaterialGlobalRiskScore.material_id.in_(top_mat_ids),
                    MaterialGlobalRiskScore.overall_risk_score.isnot(None),
                )
                .subquery()
            )
            prior_rows_q = db.execute(
                select(
                    prior_subq.c.material_id,
                    prior_subq.c.overall_risk_score,
                    prior_subq.c.as_of_date,
                )
            ).all()
            # Bucket all snapshots per material; we'll pick the right
            # prior in Python because the SQL constraint (≥7 days older
            # than the per-material current) varies row-by-row.
            snapshots_by_mat: dict[int, list[tuple[float, date]]] = {}
            for row in prior_rows_q:
                snapshots_by_mat.setdefault(row.material_id, []).append(
                    (row.overall_risk_score, row.as_of_date),
                )

            for mid, (cur_score, cur_as_of) in cur_score_by_mat.items():
                snaps = snapshots_by_mat.get(mid, [])
                # Find the most recent snapshot at least _mat_lookback_days
                # before cur_as_of but not more than _mat_max_lookback_days
                # before either.  Snaps are sorted DESC by as_of_date.
                target_max = cur_as_of - timedelta(days=_mat_lookback_days)
                target_min = cur_as_of - timedelta(days=_mat_max_lookback_days)
                prior_score: Optional[float] = None
                for snap_score, snap_date in snaps:
                    if target_min <= snap_date <= target_max:
                        prior_score = snap_score
                        break
                score_trend_by_mat[mid] = _mat_score_trend(cur_score, prior_score)

        for r in top_rows:
            top_materials.append(
                TopMaterialRisk(
                    material_id=r.material_id,
                    canonical_name=r.canonical_name,
                    symbol_or_code=r.symbol_or_code,
                    category=r.category,
                    overall_risk_score=round(r.overall_risk_score, 1),
                    top_countries=prod_shares.get(r.material_id, []),
                    trend=score_trend_by_mat.get(r.material_id),
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
        # 2026-05-11 launch-list-centric KPIs
        core_minerals_scored=core_minerals_scored,
        recent_risk_events_30d=recent_risk_events,
        coverage_gaps=coverage_gaps,
        top_materials_by_risk=top_materials,
        score_run_progress=score_run,
        recent_activity=recent_activity,
    )


# ---------------------------------------------------------------------------
# Launch-list KPI helpers (2026-05-11)
# ---------------------------------------------------------------------------

def _compute_core_minerals_scored(db: Session) -> CoreMineralsScored:
    """Count launch-list materials with a current global risk score.

    "Current" = has at least one ``MaterialGlobalRiskScore`` row with a
    non-null ``overall_risk_score``.  Materials in the launch list that
    aren't in the Materials table at all are reported as unscored
    (they're missing from the system entirely) so the analyst sees the
    same gap whether the cause is data absence or row absence.
    """
    total = len(LAUNCH_LIST_CANONICAL_NAMES)

    # Pull every launch-list material that has a non-null global score
    scored_rows = db.execute(
        select(Material.canonical_name)
        .join(
            MaterialGlobalRiskScore,
            MaterialGlobalRiskScore.material_id == Material.id,
        )
        .where(
            Material.canonical_name.in_(LAUNCH_LIST_CANONICAL_NAMES),
            MaterialGlobalRiskScore.overall_risk_score.isnot(None),
        )
        .distinct()
    ).all()
    scored_names = {row[0] for row in scored_rows}

    unscored = [
        name for name in LAUNCH_LIST_CANONICAL_NAMES if name not in scored_names
    ]

    return CoreMineralsScored(
        scored=len(scored_names),
        total=total,
        unscored_names=unscored,
    )


def _compute_recent_risk_events_30d(
    db: Session, *, now: datetime,
) -> RecentRiskEvents30d:
    """Risk-event volume in the trailing 30 days vs the 30 days before that.

    Joins ``risk_events`` → ``source_documents`` → ``sources`` to surface the
    top contributing sources.  Excludes events with no source_document_id
    (legacy / system-generated rows).
    """
    cutoff_30d = now - timedelta(days=30)
    cutoff_60d = now - timedelta(days=60)

    # Total counts: current 30d and previous 30d
    count_30d = int(
        db.scalar(
            select(func.count())
            .select_from(RiskEvent)
            .where(RiskEvent.created_at >= cutoff_30d)
        )
        or 0
    )
    count_prev = int(
        db.scalar(
            select(func.count())
            .select_from(RiskEvent)
            .where(
                RiskEvent.created_at >= cutoff_60d,
                RiskEvent.created_at < cutoff_30d,
            )
        )
        or 0
    )

    # Top sources in the current 30d window
    source_rows = db.execute(
        select(
            Source.name.label("source_name"),
            func.count(RiskEvent.id).label("n"),
        )
        .join(SourceDocument, SourceDocument.id == RiskEvent.source_document_id)
        .join(Source, Source.id == SourceDocument.source_id)
        .where(RiskEvent.created_at >= cutoff_30d)
        .group_by(Source.name)
        .order_by(func.count(RiskEvent.id).desc())
        .limit(5)
    ).all()

    top_sources = [
        SourceCount(source_name=row.source_name, count=int(row.n))
        for row in source_rows
    ]

    return RecentRiskEvents30d(
        count=count_30d,
        prev_period_count=count_prev,
        top_sources=top_sources,
    )


def _compute_coverage_gaps(db: Session, *, now: datetime) -> CoverageGaps:
    """Identify launch-list materials failing any quality bar.

    Five reasons a mineral can be flagged:

      ``no_global_score``      No ``MaterialGlobalRiskScore`` row exists.
      ``material_row_missing`` Material isn't in the DB at all (sentinel
                               material_id=-1 is returned).
      ``stale_score``          Latest score's ``as_of_date`` is older than
                               ``_GAP_STALE_DAYS`` (30 days).
      ``thin_events``          Fewer than ``_GAP_THIN_EVENTS_MIN`` (5) risk
                               events in the last
                               ``_GAP_THIN_EVENTS_WINDOW_DAYS`` (90 days).
      ``thin_pillars``         Fewer than ``_GAP_THIN_PILLARS_MIN`` (3) of
                               the five pillar columns have score > 0 at
                               the latest ``MaterialGlobalRiskScore`` row.
      ``no_facility_coverage`` Zero ``FacilityMaterialLink`` rows of any
                               shape for this material.  Captures the
                               launch-blocker case (Phosphate, today)
                               where the analyst opens the per-mineral
                               page and sees "no facilities" — the
                               operational pillar has no structural data
                               to work with at all.  Loosened from the
                               original capacity-strict check because
                               MRDS-sourced links typically have NULL
                               capacity, so the strict version flagged
                               every material with MRDS-only coverage.

    A single material can carry multiple reasons (each one is appended).
    The dashboard KPI card uses the count for the headline; drill-down
    views can render the per-material reason list.
    """
    cutoff_stale = now - timedelta(days=_GAP_STALE_DAYS)
    cutoff_events = now - timedelta(days=_GAP_THIN_EVENTS_WINDOW_DAYS)

    # Pull all launch-list materials that exist in the DB
    materials = db.execute(
        select(Material.id, Material.canonical_name).where(
            Material.canonical_name.in_(LAUNCH_LIST_CANONICAL_NAMES),
        )
    ).all()
    materials_by_name = {row.canonical_name: row.id for row in materials}

    # Latest global score per material — used for stale + thin_pillars checks
    latest_global_subq = (
        select(MaterialGlobalRiskScore)
        .distinct(MaterialGlobalRiskScore.material_id)
        .order_by(
            MaterialGlobalRiskScore.material_id,
            MaterialGlobalRiskScore.as_of_date.desc(),
            MaterialGlobalRiskScore.id.desc(),
        )
    ).subquery()

    latest_rows = db.execute(
        select(
            latest_global_subq.c.material_id,
            latest_global_subq.c.overall_risk_score,
            latest_global_subq.c.as_of_date,
            latest_global_subq.c.material_concentration_score,
            latest_global_subq.c.geopolitical_trade_score,
            latest_global_subq.c.regulatory_compliance_score,
            latest_global_subq.c.operational_score,
            latest_global_subq.c.financial_pressure_score,
        ).where(
            latest_global_subq.c.material_id.in_(materials_by_name.values()),
        )
    ).all()
    latest_by_mat = {row.material_id: row for row in latest_rows}

    # Recent risk-event counts per launch-list material via
    # RiskEventMaterial.  Empty dict for materials with no events at all.
    event_count_rows = db.execute(
        select(
            RiskEventMaterial.material_id,
            func.count(RiskEvent.id).label("n"),
        )
        .join(RiskEvent, RiskEvent.id == RiskEventMaterial.risk_event_id)
        .where(
            RiskEventMaterial.material_id.in_(materials_by_name.values()),
            RiskEvent.created_at >= cutoff_events,
        )
        .group_by(RiskEventMaterial.material_id)
    ).all()
    event_count_by_mat = {row.material_id: int(row.n) for row in event_count_rows}

    # Facility coverage per launch-list material — counts any
    # FacilityMaterialLink row regardless of whether annual_capacity_tpy
    # is set.  Originally this filtered on capacity-not-null because
    # that's what the G5 operational sub-score formula consumes, but in
    # practice MRDS-sourced links rarely have capacity (the USGS data
    # ships facility names but not throughput) so the strict check flags
    # every launch-list material with MRDS-only coverage — including
    # materials where the analyst can see facilities in the table.
    #
    # Loosened 2026-05-11 to count any link.  A separate launch-blocker
    # check ("facility_capacity_thin" — links exist but none with
    # capacity) could be added later if the partner data lands enough
    # capacity-bearing rows to make that signal useful.  Today it's not.
    facility_count_rows = db.execute(
        select(
            FacilityMaterialLink.material_id,
            func.count(FacilityMaterialLink.id).label("n"),
        )
        .where(
            FacilityMaterialLink.material_id.in_(materials_by_name.values()),
        )
        .group_by(FacilityMaterialLink.material_id)
    ).all()
    facility_count_by_mat = {
        row.material_id: int(row.n) for row in facility_count_rows
    }

    gap_items: list[CoverageGapItem] = []
    # Iterate in launch-list order so the output is presentation-stable
    for name in LAUNCH_LIST_CANONICAL_NAMES:
        material_id = materials_by_name.get(name)
        reasons: list[str] = []

        if material_id is None:
            # Material not even in the DB — count as the most severe gap
            gap_items.append(CoverageGapItem(
                material_id=-1,  # sentinel; downstream consumers can filter
                canonical_name=name,
                reasons=["no_global_score", "material_row_missing"],
            ))
            continue

        latest = latest_by_mat.get(material_id)
        if latest is None or latest.overall_risk_score is None:
            reasons.append("no_global_score")
        else:
            if latest.as_of_date is not None:
                as_of_dt = datetime.combine(
                    latest.as_of_date, datetime.min.time(), tzinfo=timezone.utc,
                ) if not isinstance(latest.as_of_date, datetime) else latest.as_of_date
                if as_of_dt < cutoff_stale:
                    reasons.append("stale_score")
            # Count pillars with non-fallback signal
            pillar_values = [
                latest.material_concentration_score,
                latest.geopolitical_trade_score,
                latest.regulatory_compliance_score,
                latest.operational_score,
                latest.financial_pressure_score,
            ]
            non_zero = sum(
                1 for v in pillar_values if v is not None and v > 0
            )
            if non_zero < _GAP_THIN_PILLARS_MIN:
                reasons.append("thin_pillars")

        events_in_window = event_count_by_mat.get(material_id, 0)
        if events_in_window < _GAP_THIN_EVENTS_MIN:
            reasons.append("thin_events")

        # Facility-coverage check — the actual launch-blocker condition
        # for Phosphate today.  A material can pass every score-side
        # check (has score, fresh, 3+ pillars, enough events) and still
        # be operationally invisible because no facilities are seeded.
        # We flag that case separately so the gap is surfaced even after
        # a rescore.
        facilities_with_capacity = facility_count_by_mat.get(material_id, 0)
        if facilities_with_capacity == 0:
            reasons.append("no_facility_coverage")

        if reasons:
            gap_items.append(CoverageGapItem(
                material_id=material_id,
                canonical_name=name,
                reasons=reasons,
            ))

    return CoverageGaps(count=len(gap_items), materials=gap_items)


# ---------------------------------------------------------------------------
# Coverage matrix endpoint (2026-05-11)
# ---------------------------------------------------------------------------
# Returns a per-launch-list-material view across two column groups:
#
#   Sources   — count of RiskEvents created in the last 90 days, grouped
#               by Source.name.  Drives the "where is signal coming from
#               for this mineral?" half of the matrix.
#   Pillars   — latest MaterialGlobalRiskScore for each of the 5 pillar
#               columns, with a has_signal flag (score > 0).  Subsumes
#               the prior Score-run progress card at per-material
#               granularity.
#
# 90-day window matches the thin_events gap threshold so the matrix and
# the Coverage Gaps KPI tell a consistent story.

_COVERAGE_MATRIX_WINDOW_DAYS = 90


@router.get("/coverage-matrix", response_model=CoverageMatrix)
def coverage_matrix(
    _user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CoverageMatrix:
    """Coverage matrix: launch-list materials × sources × pillars.

    Separate endpoint from `/overview` so the dashboard can refresh the
    matrix independently (the matrix is cheap to compute and the analyst
    might want to re-query it after kicking off a new ingest, without
    re-running every overview computation).
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=_COVERAGE_MATRIX_WINDOW_DAYS)

    # Launch-list materials in canonical order
    mat_rows = db.execute(
        select(Material.id, Material.canonical_name).where(
            Material.canonical_name.in_(LAUNCH_LIST_CANONICAL_NAMES),
        )
    ).all()
    materials_by_name = {row.canonical_name: row.id for row in mat_rows}
    all_mat_ids = list(materials_by_name.values())

    # ── Sources × materials grid ──────────────────────────────────────
    # One query joins risk_event_materials → risk_events → source_documents → sources,
    # filters to launch-list materials in the window, groups by both axes.
    source_grid_rows = db.execute(
        select(
            RiskEventMaterial.material_id,
            Source.name.label("source_name"),
            func.count(RiskEvent.id).label("n"),
        )
        .join(RiskEvent, RiskEvent.id == RiskEventMaterial.risk_event_id)
        .join(SourceDocument, SourceDocument.id == RiskEvent.source_document_id)
        .join(Source, Source.id == SourceDocument.source_id)
        .where(
            RiskEventMaterial.material_id.in_(all_mat_ids or [-1]),
            RiskEvent.created_at >= cutoff,
        )
        .group_by(RiskEventMaterial.material_id, Source.name)
    ).all() if all_mat_ids else []

    # Index by (material_id, source_name) for O(1) lookup when building rows
    cell_counts: dict[tuple[int, str], int] = {
        (row.material_id, row.source_name): int(row.n)
        for row in source_grid_rows
    }

    # Total per source — used to order columns by descending volume so the
    # most-active sources land on the left of the matrix.
    source_totals: dict[str, int] = {}
    for (_, source_name), count in cell_counts.items():
        source_totals[source_name] = source_totals.get(source_name, 0) + count
    sources_in_order = sorted(
        source_totals.keys(), key=lambda s: (-source_totals[s], s),
    )

    # ── Pillars × materials grid ──────────────────────────────────────
    # Latest MaterialGlobalRiskScore per material — same DISTINCT ON pattern
    # used elsewhere.  We need both the score value and a has_signal flag
    # per pillar column.
    latest_global_subq = (
        select(MaterialGlobalRiskScore)
        .distinct(MaterialGlobalRiskScore.material_id)
        .order_by(
            MaterialGlobalRiskScore.material_id,
            MaterialGlobalRiskScore.as_of_date.desc(),
            MaterialGlobalRiskScore.id.desc(),
        )
    ).subquery()

    latest_rows = db.execute(
        select(
            latest_global_subq.c.material_id,
            latest_global_subq.c.material_concentration_score,
            latest_global_subq.c.geopolitical_trade_score,
            latest_global_subq.c.regulatory_compliance_score,
            latest_global_subq.c.operational_score,
            latest_global_subq.c.financial_pressure_score,
        ).where(
            latest_global_subq.c.material_id.in_(all_mat_ids or [-1]),
        )
    ).all() if all_mat_ids else []
    latest_by_mat = {row.material_id: row for row in latest_rows}

    pillar_columns = [label for _, label in _PILLARS]

    # ── Compose rows in canonical launch-list order ───────────────────
    rows: list[CoverageMatrixRow] = []
    for name in LAUNCH_LIST_CANONICAL_NAMES:
        material_id = materials_by_name.get(name)
        if material_id is None:
            # Material not in DB at all (sentinel row).  Empty cells.
            rows.append(CoverageMatrixRow(
                material_id=-1,
                canonical_name=name,
                sources=[
                    CoverageMatrixSourceCount(source_name=s, event_count_90d=0)
                    for s in sources_in_order
                ],
                pillars=[
                    CoverageMatrixPillar(
                        name=col_name, label=col_label,
                        score=None, has_signal=False,
                    )
                    for col_name, col_label in _PILLARS
                ],
            ))
            continue

        source_cells = [
            CoverageMatrixSourceCount(
                source_name=s,
                event_count_90d=cell_counts.get((material_id, s), 0),
            )
            for s in sources_in_order
        ]

        latest = latest_by_mat.get(material_id)
        pillar_cells: list[CoverageMatrixPillar] = []
        for col_name, col_label in _PILLARS:
            score_val: Optional[float] = None
            if latest is not None:
                score_val = getattr(latest, col_name, None)
            pillar_cells.append(CoverageMatrixPillar(
                name=col_name,
                label=col_label,
                score=score_val,
                has_signal=bool(score_val and score_val > 0),
            ))

        rows.append(CoverageMatrixRow(
            material_id=material_id,
            canonical_name=name,
            sources=source_cells,
            pillars=pillar_cells,
        ))

    # Pillar-coverage aggregate — derived from the just-computed rows.
    # Counts launch-list materials (excluding sentinel rows) that have
    # signal (>0) or any score at all (not null) per pillar.  Drives the
    # dedicated Pillar Coverage card on the dashboard.
    total_real_rows = sum(1 for r in rows if r.material_id != -1)
    pillar_coverage: list[PillarCoverageStat] = []
    for col_name, col_label in _PILLARS:
        with_signal = 0
        with_score = 0
        for r in rows:
            if r.material_id == -1:
                continue
            # Find this pillar's cell on the row (preserves order semantics
            # without making them positional).
            cell = next((p for p in r.pillars if p.name == col_name), None)
            if cell is None:
                continue
            if cell.score is not None:
                with_score += 1
            if cell.has_signal:
                with_signal += 1
        pillar_coverage.append(PillarCoverageStat(
            name=col_name,
            label=col_label,
            materials_with_signal=with_signal,
            materials_with_score=with_score,
            total=total_real_rows,
        ))

    return CoverageMatrix(
        window_days=_COVERAGE_MATRIX_WINDOW_DAYS,
        sources_in_order=sources_in_order,
        pillar_columns=pillar_columns,
        rows=rows,
        pillar_coverage=pillar_coverage,
    )
