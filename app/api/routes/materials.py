"""Materials routes — Phase 2 reference-data browser.

Includes HS-code mapping inspection and the global mismatches view that lets
analysts spot bad seed data before it corrupts trade-flow scores.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import Select, and_, func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.api.deps import get_current_user, get_db
from app.models.battery_chemistry import BatteryChemistry
from app.models.criticality_signal import MaterialCriticalitySignal
from app.models.facility import FacilityMaterialLink
from app.models.documents import SourceDocument
from app.models.regulatory import (
    RiskEvent,
    RiskEventGeography,
    RiskEventHsMapping,
    RiskEventMaterial,
)
from app.models.source import Source
from app.models.reporting import AnalystNote
from app.models.scoring import HsCodeGeographyRiskScore, MaterialGlobalRiskScore
from app.models.country import Country
from app.models.supply import (
    CountryMaterialRelevance,
    HsCodeMaterialMapping,
    HsCodeProductionShare,
    Material,
    MaterialProductionShare,
)
from app.services.scoring.launch_list import LAUNCH_LIST_CANONICAL_NAMES
from app.schemas.common import PaginatedResponse, VerifiedResponse, VerifiedUpdate
from app.schemas.materials import (
    CountryMaterialRelevanceItem,
    CountryShareItem,
    HsMappingGeographyRead,
    HsMappingRead,
    HsMismatchItem,
    MappingHealth,
    MaterialDetail,
    MaterialListItem,
    MaterialListPillarScore,
)
from app.schemas.note import AnalystNoteCreate, AnalystNoteRead
from app.schemas.regulatory import (
    MaterialRiskEventRow,
    MaterialRiskEventsResponse,
    MaterialRiskEventsSummary,
)

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


def _load_mapping_aggregates(
    db: Session,
    mapping_ids: list[int],
) -> dict[int, dict]:
    """Bulk pre-load the per-mapping aggregate data the frontend needs.

    Returns ``{mapping_id: {"geographies": list[HsMappingGeographyRead],
    "node_score": float|None, "hhi": float|None, "events_open": int,
    "scored_at": datetime|None}}``.

    Three queries are issued (one per source table); rows are joined in
    Python rather than SQL because the per-source filters
    (latest_reference_year, market_scope='global', latest as_of_date) make
    a single SQL JOIN gnarly without a CTE.  Three small queries beat one
    large one for readability + per-row debug ergonomics.

    Empty input → empty dict, no DB hits.
    """
    if not mapping_ids:
        return {}

    # Per-mapping aggregate skeleton.  Defaults match the frontend's
    # "no scoring yet / no production data yet" rendering.
    out: dict[int, dict] = {
        mid: {
            "geographies_by_country": {},   # {country_code: HsMappingGeographyRead}
            "node_score": None,
            "hhi": None,
            "events_open": 0,
            "scored_at": None,
            # Roll-up of per-country score_method values.  ``"hhi_anchored"``
            # if any geography on the node has anchored data; otherwise
            # ``"event_only_no_hhi"`` if all scores came from the fallback.
            # Stays None if nothing is scored yet.
            "score_method": None,
        }
        for mid in mapping_ids
    }

    # ── 1. Production shares (latest reference_year per mapping) ──────────
    # Subquery: for each mapping, pick the most recent reference_year that
    # has any rows.  Then pull all rows for (mapping, that year, global).
    latest_year_sq = (
        select(
            HsCodeProductionShare.hs_mapping_id,
            func.max(HsCodeProductionShare.reference_year).label("max_year"),
        )
        .where(
            HsCodeProductionShare.hs_mapping_id.in_(mapping_ids),
            HsCodeProductionShare.market_scope == "global",
        )
        .group_by(HsCodeProductionShare.hs_mapping_id)
        .subquery()
    )
    share_rows = db.execute(
        select(HsCodeProductionShare)
        .join(
            latest_year_sq,
            and_(
                HsCodeProductionShare.hs_mapping_id == latest_year_sq.c.hs_mapping_id,
                HsCodeProductionShare.reference_year == latest_year_sq.c.max_year,
            ),
        )
        .where(HsCodeProductionShare.market_scope == "global")
        .order_by(HsCodeProductionShare.production_share.desc())
    ).scalars().all()
    for row in share_rows:
        bucket = out[row.hs_mapping_id]
        country = row.country_code.upper() if row.country_code else "??"
        bucket["geographies_by_country"][country] = HsMappingGeographyRead(
            country_code=country,
            production_share=float(row.production_share) if row.production_share is not None else None,
            production_volume=float(row.production_volume) if row.production_volume is not None else None,
            reference_year=row.reference_year,
        )

    # ── 2. HS-node geography scores (latest as_of_date per mapping/country) ──
    latest_score_sq = (
        select(
            HsCodeGeographyRiskScore.hs_mapping_id,
            HsCodeGeographyRiskScore.country_code,
            HsCodeGeographyRiskScore.as_of_date,
            HsCodeGeographyRiskScore.hhi_at_stage,
            HsCodeGeographyRiskScore.tariff_exposure,
            HsCodeGeographyRiskScore.export_restriction,
            HsCodeGeographyRiskScore.composite_node_score,
            HsCodeGeographyRiskScore.metadata_json,   # carries score_method
            func.row_number()
            .over(
                partition_by=(
                    HsCodeGeographyRiskScore.hs_mapping_id,
                    HsCodeGeographyRiskScore.country_code,
                ),
                order_by=HsCodeGeographyRiskScore.as_of_date.desc(),
            )
            .label("rn"),
        )
        .where(
            HsCodeGeographyRiskScore.hs_mapping_id.in_(mapping_ids),
            HsCodeGeographyRiskScore.market_scope == "global",
        )
        .subquery()
    )
    score_rows = db.execute(
        select(latest_score_sq).where(latest_score_sq.c.rn == 1)
    ).all()
    for row in score_rows:
        mid = row.hs_mapping_id
        country = (row.country_code or "??").upper()
        bucket = out.get(mid)
        if bucket is None:
            continue
        # Find or create the geography breakdown row.
        geo = bucket["geographies_by_country"].get(country)
        if geo is None:
            # Score row exists but no production share — keep the country
            # visible to the partner so they see why the row scored.
            geo = HsMappingGeographyRead(country_code=country)
            bucket["geographies_by_country"][country] = geo
        geo.hhi = float(row.hhi_at_stage) if row.hhi_at_stage is not None else None
        geo.tariff_exposure = float(row.tariff_exposure) if row.tariff_exposure is not None else None
        geo.export_restriction = (
            float(row.export_restriction) if row.export_restriction is not None else None
        )
        geo.score = (
            float(row.composite_node_score) if row.composite_node_score is not None else None
        )
        geo.scored_at = row.as_of_date if row.as_of_date else None
        # Pluck score_method out of the metadata blob.  Tolerates both legacy
        # rows (no key) and the new dual-path rows (Option 1, 2026-05-06).
        meta = row.metadata_json if row.metadata_json else {}
        geo.score_method = meta.get("score_method") if isinstance(meta, dict) else None
        # Aggregate-level rollup
        if geo.score is not None:
            current_max = bucket["node_score"]
            if current_max is None or geo.score > current_max:
                bucket["node_score"] = geo.score
        if geo.scored_at is not None:
            if bucket["scored_at"] is None or geo.scored_at > bucket["scored_at"]:
                bucket["scored_at"] = geo.scored_at
        # Method rollup: ``"hhi_anchored"`` wins over ``"event_only_no_hhi"``
        # if any geography on this node has anchored data.  This drives the
        # frontend's "no production base" badge — only show it when the
        # node had to fall back across the board.
        if geo.score_method == "hhi_anchored":
            bucket["score_method"] = "hhi_anchored"
        elif (
            geo.score_method == "event_only_no_hhi"
            and bucket["score_method"] != "hhi_anchored"
        ):
            bucket["score_method"] = "event_only_no_hhi"

    # ── 3. Open events count per mapping ────────────────────────────────
    event_count_rows = db.execute(
        select(
            RiskEventHsMapping.hs_mapping_id,
            func.count(RiskEventHsMapping.risk_event_id).label("cnt"),
        )
        .where(RiskEventHsMapping.hs_mapping_id.in_(mapping_ids))
        .group_by(RiskEventHsMapping.hs_mapping_id)
    ).all()
    for mid, cnt in event_count_rows:
        bucket = out.get(mid)
        if bucket is not None:
            bucket["events_open"] = int(cnt or 0)

    # ── Aggregate hhi: production-weighted average across geographies ───
    # Use production_share as the weight when available, otherwise plain mean.
    for mid, bucket in out.items():
        geos = list(bucket["geographies_by_country"].values())
        hhis = [
            (g.hhi, g.production_share)
            for g in geos
            if g.hhi is not None
        ]
        if hhis:
            weighted = [
                (hhi, share if share is not None else 0.0)
                for hhi, share in hhis
            ]
            total_weight = sum(w for _, w in weighted)
            if total_weight > 0:
                bucket["hhi"] = sum(hhi * w for hhi, w in weighted) / total_weight
            else:
                bucket["hhi"] = sum(hhi for hhi, _ in weighted) / len(weighted)

    return out


def _annotate_mapping(
    mapping: HsCodeMaterialMapping,
    material: Material,
    cross_mapped: set[str],
    aggregates: Optional[dict[int, dict]] = None,
) -> HsMappingRead:
    """Build a ``HsMappingRead`` with mismatch flags + (optional) aggregates set.

    ``aggregates`` is the bulk-loaded dict from ``_load_mapping_aggregates``.
    When ``None`` the mismatch-only path is preserved (used by the global
    mismatches view + the list-materials count pass, which don't need the
    expensive joins).
    """
    is_low = mapping.confidence < _LOW_CONFIDENCE_THRESHOLD
    is_missing = not mapping.description
    is_chapter = _is_chapter_mismatch(mapping, material)
    is_cross = mapping.hs_code_prefix in cross_mapped

    out = HsMappingRead.model_validate(mapping)
    out.is_low_confidence = is_low
    out.is_missing_description = is_missing
    out.is_chapter_mismatch = is_chapter
    out.is_cross_mapped = is_cross

    if aggregates is not None:
        agg = aggregates.get(mapping.id)
        if agg is not None:
            geos = list(agg["geographies_by_country"].values())
            # Stable order: largest production share first, then alphabetical.
            geos.sort(
                key=lambda g: (
                    -(g.production_share or 0.0),
                    g.country_code,
                )
            )
            out.geographies = geos
            out.node_score = agg["node_score"]
            out.hhi = agg["hhi"]
            out.events_open = agg["events_open"]
            out.scored_at = agg["scored_at"]
            out.score_method = agg["score_method"]
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

# ---------------------------------------------------------------------------
# Risk-score trend helper (2026-05-11; revised from event-count → score-based)
# ---------------------------------------------------------------------------
# Trend signal derived from the actual MaterialGlobalRiskScore time series:
# compare the most recent overall score to the most recent prior snapshot
# at least 7 days older.  Replaces the event-volume heuristic that was a
# stand-in until score history accumulated.
#
# Rationale: more events doesn't mean more risk.  Some ingesters
# (IEA Policy Tracker INVESTMENT_PLEDGE) emit constructive signals at
# low severity.  A spike of those would have rendered "rising" red on
# the dashboard while the actual risk picture was improving.  Trending
# on the composite score is the only signal that's semantically correct
# for "is risk going up?" — the question the chip is supposed to answer.
#
# Calibration choices, documented inline:
#
#   * Lookback = 7 days.  Find the most recent snapshot whose
#     as_of_date is at least 7 days before the current snapshot.
#   * Threshold = ±5 points on the 0–100 scale.  Anything inside this
#     band reads "stable".  Below this, the move is well within
#     normal week-to-week variance for a composite score built from
#     5 pillars each ranging 0–100.
#   * Max-lookback guard = 30 days.  If the most recent prior snapshot
#     is more than 30 days older than current, return None — we'd
#     effectively be comparing this month's score to last quarter's
#     and the trend label would be meaningless.

_SCORE_TREND_LOOKBACK_DAYS = 7
_SCORE_TREND_MIN_DELTA = 5.0
_SCORE_TREND_MAX_LOOKBACK_DAYS = 30


def _compute_score_trend(
    current_score: Optional[float],
    prior_score: Optional[float],
) -> Optional[str]:
    """Return ``"rising"`` / ``"stable"`` / ``"declining"`` or None.

    ``current_score`` is the latest MaterialGlobalRiskScore.overall.
    ``prior_score`` is the score from at least 7 days earlier (but no
    more than 30 days earlier — see ``_SCORE_TREND_MAX_LOOKBACK_DAYS``).
    None means insufficient score history — UI renders a dash.
    """
    if current_score is None or prior_score is None:
        return None
    delta = current_score - prior_score
    if delta > _SCORE_TREND_MIN_DELTA:
        return "rising"
    if delta < -_SCORE_TREND_MIN_DELTA:
        return "declining"
    return "stable"


_PILLAR_COLUMNS_FOR_LIST = [
    ("material_concentration_score", "Material Concentration"),
    ("geopolitical_trade_score", "Geopolitical / Trade"),
    ("regulatory_compliance_score", "Regulatory Compliance"),
    ("operational_score", "Operational"),
    ("financial_pressure_score", "Financial Pressure"),
]

_MATERIALS_LIST_EVENT_WINDOW_DAYS = 90


@router.get("/materials", response_model=PaginatedResponse[MaterialListItem])
def list_materials(
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
    search: Optional[str] = Query(None),
    category: Optional[str] = Query(None),
    is_ira_critical: Optional[bool] = Query(None),
    is_eu_crma_critical: Optional[bool] = Query(None),
    is_launch_list: Optional[bool] = Query(
        None,
        description=(
            "Restrict to launch-list materials (the core 10 minerals the v1 "
            "product focuses on).  Pass true to scope to launch list, false "
            "for non-launch-list-only, omit for all."
        ),
    ),
    _user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> PaginatedResponse[MaterialListItem]:
    """Paginated materials list with analyst-view per-row enrichments.

    2026-05-11: dropped the ``has_mismatched_mappings`` query param and
    the per-material mismatch-count computation along with it — the HS
    mismatch UI was retired from the Materials page.  Added per-row
    enrichments (launch-list flag, top-producer shares with %, latest
    per-pillar scores, 90-day risk-event count) so the list view can
    render the new analyst-focused row layout without N+1 round-trips.
    """
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
    if is_launch_list is True:
        q = q.where(Material.canonical_name.in_(LAUNCH_LIST_CANONICAL_NAMES))
    elif is_launch_list is False:
        q = q.where(~Material.canonical_name.in_(LAUNCH_LIST_CANONICAL_NAMES))

    total = db.scalar(select(func.count()).select_from(q.subquery()))
    materials = db.scalars(
        q.order_by(Material.canonical_name)
        .offset((page - 1) * limit)
        .limit(limit)
    ).all()

    material_ids = [m.id for m in materials]
    mapping_counts: dict[int, int] = {}
    if material_ids:
        count_rows = db.execute(
            select(
                HsCodeMaterialMapping.material_id,
                func.count(HsCodeMaterialMapping.id).label("cnt"),
            )
            .where(HsCodeMaterialMapping.material_id.in_(material_ids))
            .group_by(HsCodeMaterialMapping.material_id)
        ).all()
        for mid, cnt in count_rows:
            mapping_counts[mid] = cnt

    # ── Latest global risk score + per-pillar values per material ─────
    # Single ranked-window query pulls everything we need for the
    # overall score AND the 5 pillar scores in one round-trip.
    latest_score_data: dict[int, dict] = {}
    if material_ids:
        latest_score_sq = (
            select(
                MaterialGlobalRiskScore.material_id,
                MaterialGlobalRiskScore.overall_risk_score,
                MaterialGlobalRiskScore.material_concentration_score,
                MaterialGlobalRiskScore.geopolitical_trade_score,
                MaterialGlobalRiskScore.regulatory_compliance_score,
                MaterialGlobalRiskScore.operational_score,
                MaterialGlobalRiskScore.financial_pressure_score,
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
                latest_score_sq.c.material_concentration_score,
                latest_score_sq.c.geopolitical_trade_score,
                latest_score_sq.c.regulatory_compliance_score,
                latest_score_sq.c.operational_score,
                latest_score_sq.c.financial_pressure_score,
            ).where(latest_score_sq.c.rn == 1)
        ).all()
        for row in score_rows:
            latest_score_data[row.material_id] = {
                "overall": row.overall_risk_score,
                "material_concentration_score": row.material_concentration_score,
                "geopolitical_trade_score": row.geopolitical_trade_score,
                "regulatory_compliance_score": row.regulatory_compliance_score,
                "operational_score": row.operational_score,
                "financial_pressure_score": row.financial_pressure_score,
            }

    # ── Top producer shares per material ──────────────────────────────
    # Latest reference_year per material, then top 3 producers by share.
    top_producer_shares: dict[int, list[CountryShareItem]] = {}
    if material_ids:
        latest_year_sq = (
            select(
                MaterialProductionShare.material_id,
                func.max(MaterialProductionShare.reference_year).label("yr"),
            )
            .where(MaterialProductionShare.material_id.in_(material_ids))
            .group_by(MaterialProductionShare.material_id)
        ).subquery()
        share_rows = db.execute(
            select(
                MaterialProductionShare.material_id,
                MaterialProductionShare.country_code,
                MaterialProductionShare.production_share,
            )
            .join(
                latest_year_sq,
                and_(
                    MaterialProductionShare.material_id == latest_year_sq.c.material_id,
                    MaterialProductionShare.reference_year == latest_year_sq.c.yr,
                ),
            )
            .where(MaterialProductionShare.production_share.isnot(None))
            .order_by(
                MaterialProductionShare.material_id,
                MaterialProductionShare.production_share.desc(),
            )
        ).all()
        for mid, code, share in share_rows:
            lst = top_producer_shares.setdefault(mid, [])
            if len(lst) >= 3:
                continue
            # production_share is stored as a 0-1 fraction; surface as int %
            pct = round(float(share) * 100) if share is not None else 0
            lst.append(CountryShareItem(code=code, share_pct=pct))

    # ── Risk-event count per material in the last 90 days ─────────────
    # Matches the coverage matrix window so the Materials page and the
    # dashboard tell the same story.
    #
    # 2026-06-14 bugfix: filter by event_date (when the event occurred)
    # instead of created_at (when the row was ingested).  The badge label
    # says "90D" — analysts read that as "events from the last 90 days",
    # not "events ingested in the last 90 days".  Pre-fix the badge was
    # equivalent to "all events tagged to this material" because a recent
    # re-ingestion put every row's created_at within the 90-day window
    # (Cobalt showed 741 = total ever, vs. 31 actually within 90d).
    # event_date can be NULL for events without a single anchor date
    # (sanctions programmes, multi-year regulations) — those are excluded
    # here so the count reflects strictly date-anchored recent activity.
    cutoff = datetime.now(timezone.utc) - timedelta(
        days=_MATERIALS_LIST_EVENT_WINDOW_DAYS,
    )
    recent_event_counts: dict[int, int] = {}
    if material_ids:
        event_count_rows = db.execute(
            select(
                RiskEventMaterial.material_id,
                func.count(RiskEvent.id).label("n"),
            )
            .join(RiskEvent, RiskEvent.id == RiskEventMaterial.risk_event_id)
            .where(
                RiskEventMaterial.material_id.in_(material_ids),
                RiskEvent.event_date >= cutoff,
                RiskEvent.duplicate_of_id.is_(None),  # 055
                RiskEventMaterial.is_direct.is_(True),  # 056
            )
            .group_by(RiskEventMaterial.material_id)
        ).all()
        for mid, n in event_count_rows:
            recent_event_counts[mid] = int(n)

    launch_list_set = {name.lower() for name in LAUNCH_LIST_CANONICAL_NAMES}

    items: list[MaterialListItem] = []
    for mat in materials:
        item = MaterialListItem.model_validate(mat)
        item.hs_mapping_count = mapping_counts.get(mat.id, 0)
        item.mapping_mismatch_count = 0  # mismatch UI retired 2026-05-11
        item.latest_overall_risk_score = (
            latest_score_data.get(mat.id, {}).get("overall")
        )

        # 2026-05-11 enrichments
        item.is_launch_list = (mat.canonical_name or "").lower() in launch_list_set
        item.top_producer_shares = top_producer_shares.get(mat.id, [])
        item.recent_event_count_90d = recent_event_counts.get(mat.id, 0)

        pillar_data = latest_score_data.get(mat.id, {})
        item.pillar_scores = [
            MaterialListPillarScore(
                name=col_name,
                label=col_label,
                score=pillar_data.get(col_name),
                has_signal=bool(
                    pillar_data.get(col_name) is not None
                    and pillar_data.get(col_name) > 0
                ),
            )
            for col_name, col_label in _PILLAR_COLUMNS_FOR_LIST
        ]

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
    # Bulk-load production shares + node scores + event counts so the
    # frontend HS Codes & Stages tab renders Geography / SHARE / HHI /
    # TARIFF / EXPORT / SCORE columns without per-row queries.
    aggregates = _load_mapping_aggregates(
        db, [m.id for m in mat.hs_mappings]
    )
    annotated_mappings = [
        _annotate_mapping(m, mat, cross_mapped, aggregates)
        for m in mat.hs_mappings
    ]
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

    # ── 2026-05-11 analyst-view enrichments ──────────────────────────
    now = datetime.now(timezone.utc)
    cutoff_90d = now - timedelta(days=90)

    # Risk events in last 90 days mapped via RiskEventMaterial — matches
    # the same window the dashboard uses everywhere else.
    #
    # 2026-06-14 bugfix: filter by event_date (occurrence) not created_at
    # (ingestion).  See the parallel fix on the list endpoint above for
    # context.  event_date NULL rows are excluded so the count reflects
    # strictly date-anchored recent activity.
    recent_events_n = int(
        db.scalar(
            select(func.count())
            .select_from(RiskEvent)
            .join(
                RiskEventMaterial,
                RiskEventMaterial.risk_event_id == RiskEvent.id,
            )
            .where(
                RiskEventMaterial.material_id == material_id,
                RiskEvent.event_date >= cutoff_90d,
                RiskEvent.duplicate_of_id.is_(None),  # 055
                RiskEventMaterial.is_direct.is_(True),  # 056
            )
        )
        or 0
    )

    # Risk-score trend — compare latest MaterialGlobalRiskScore to the
    # most recent snapshot at least 7 days older.  Returns None when
    # there's no prior snapshot or the prior is too stale.
    latest_score_row = db.scalar(
        select(MaterialGlobalRiskScore)
        .where(MaterialGlobalRiskScore.material_id == material_id)
        .order_by(MaterialGlobalRiskScore.as_of_date.desc())
        .limit(1)
    )
    score_trend: Optional[str] = None
    if latest_score_row is not None and latest_score_row.overall_risk_score is not None:
        prior_cutoff = latest_score_row.as_of_date - timedelta(
            days=_SCORE_TREND_LOOKBACK_DAYS,
        )
        max_lookback_cutoff = latest_score_row.as_of_date - timedelta(
            days=_SCORE_TREND_MAX_LOOKBACK_DAYS,
        )
        prior_score_row = db.scalar(
            select(MaterialGlobalRiskScore)
            .where(
                MaterialGlobalRiskScore.material_id == material_id,
                MaterialGlobalRiskScore.as_of_date <= prior_cutoff,
                MaterialGlobalRiskScore.as_of_date >= max_lookback_cutoff,
                MaterialGlobalRiskScore.overall_risk_score.isnot(None),
            )
            .order_by(MaterialGlobalRiskScore.as_of_date.desc())
            .limit(1)
        )
        if prior_score_row is not None:
            score_trend = _compute_score_trend(
                latest_score_row.overall_risk_score,
                prior_score_row.overall_risk_score,
            )

    # Facility coverage — any link, any stage, any capacity.  Matches
    # the (loosened) gap-detection check; 0 = launch-blocker for the
    # operational pillar's structural input.
    facility_n = int(
        db.scalar(
            select(func.count())
            .select_from(FacilityMaterialLink)
            .where(FacilityMaterialLink.material_id == material_id)
        )
        or 0
    )

    detail = MaterialDetail.model_validate(mat)
    detail.hs_mappings = annotated_mappings
    detail.mapping_health = health
    detail.chemistry_uses = enriched_uses
    detail.country_production_shares = country_shares
    detail.recent_event_count_90d = recent_events_n
    detail.facility_count = facility_n
    detail.score_trend_7d = score_trend
    return detail


# ---------------------------------------------------------------------------
# GET /materials/{id}/risk-events
# ---------------------------------------------------------------------------
#
# Powers the Risk events tab on the material detail page.  Returns:
#   - signal summary (counts respect the window + active filters)
#   - filtered event rows enriched with pillars_affected, source_system,
#     source_url, and geography_codes (so the row + drawer can render
#     without a follow-up fetch per event)
#
# Filters compose as additional WHERE clauses — passing none yields the
# 365-day default window the analyst tier uses for the tab.

_TAB_HIGH_SEVERITY_THRESHOLD = 0.75
"""Severity scores >= 0.75 (out of 1.0) count as 'high' in the signal
summary card.  Matches the band threshold used by the table chip
``pillarColorClass`` (>= 75/100) so the analyst sees consistent banding
between the summary card and the per-event row."""


def _coerce_pillars(raw) -> list[str]:
    """Decode RiskEvent.risk_categories_json → list[str] of pillar slugs.

    Tolerant to the few shapes the ingesters produce: list[str],
    dict[str, anything] (keys are pillar slugs), or None / empty.
    """
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(x) for x in raw if x]
    if isinstance(raw, dict):
        return [str(k) for k in raw.keys() if k]
    return []


_SORTABLE_FIELDS = {
    "title",
    "event_type",
    "source_system",
    "severity_score",
    "event_date",
}
"""Whitelist for the ``sort_by`` query param.  Anything else falls back to
the ``event_date`` default.  pillars_affected and geography_codes are
omitted because they're arrays — sorting on them would require picking
a representative element which would be confusing."""


@router.get(
    "/materials/{material_id}/risk-events",
    response_model=MaterialRiskEventsResponse,
)
def list_material_risk_events(
    material_id: int,
    window_days: int = Query(365, ge=1, le=3650),
    pillar: Optional[str] = Query(None),
    source: Optional[str] = Query(None),
    event_type: Optional[str] = Query(None),
    severity_min: Optional[float] = Query(None, ge=0.0, le=1.0),
    verified_only: bool = Query(False),
    include_broad: bool = Query(
        False,
        description="Include broad multi-material measures (is_direct=False). "
                    "Default off — material pages show material-specific "
                    "events only; broad measures live in the industry-events "
                    "surface (056).",
    ),
    search: Optional[str] = Query(None),
    sort_by: str = Query("event_date"),
    sort_dir: str = Query("desc"),
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
    _user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> MaterialRiskEventsResponse:
    """Per-material risk events feed for the analyst Risk events tab.

    Query parameters:
    - ``window_days`` (default 365): the analyst-tier default agreed for
      the tab.  Frontend exposes a select with 30 / 90 / 365 / 730 / all
      (all = 3650).
    - ``pillar``: filter to events whose risk_categories_json contains
      this pillar slug.  Multi-pillar events match if the slug appears
      in their list.
    - ``source``: filter by Source.name (e.g. ``global_trade_alert``,
      ``federal_register``).
    - ``event_type``: exact match on RiskEvent.event_type.
    - ``severity_min`` (0.0–1.0): minimum severity threshold.
    - ``verified_only``: when true, only analyst-verified rows.
    - ``search``: case-insensitive title/summary substring.
    - ``sort_by`` / ``sort_dir``: one of title | event_type |
      source_system | severity_score | event_date, asc | desc.  Default
      ``event_date desc`` (newest first).  Unknown values fall back to
      the default.  Null values for the chosen column always sort to
      the end regardless of direction.
    - ``page`` / ``limit``: pagination.
    """
    _get_material_or_404(db, material_id)

    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)

    # Base query: events tagged to this material, within the window.
    # event_date NULL events (some sanctions programmes) are excluded
    # from the tab so the date column is always populated — matches the
    # signal-card label of "Trailing N days" being a date-anchored
    # promise.
    base = (
        select(RiskEvent)
        .join(RiskEventMaterial, RiskEventMaterial.risk_event_id == RiskEvent.id)
        .where(
            RiskEventMaterial.material_id == material_id,
            RiskEvent.event_date >= cutoff,
            RiskEvent.duplicate_of_id.is_(None),  # 055
        )
    )
    if not include_broad:
        base = base.where(RiskEventMaterial.is_direct.is_(True))  # 056

    # Filter composition — applied to both the summary stats and the
    # paginated list so the cards stay in sync with the table.
    if source:
        base = (
            base.join(SourceDocument, SourceDocument.id == RiskEvent.source_document_id)
            .join(Source, Source.id == SourceDocument.source_id)
            .where(Source.name == source)
        )
    if event_type:
        base = base.where(RiskEvent.event_type == event_type)
    if severity_min is not None:
        base = base.where(RiskEvent.severity_score >= severity_min)
    if verified_only:
        base = base.where(RiskEvent.verified.is_(True))
    if search:
        like = f"%{search}%"
        base = base.where(
            or_(RiskEvent.title.ilike(like), RiskEvent.summary.ilike(like))
        )

    # Pillar filter — applied to risk_categories_json.  PostgreSQL ?
    # operator checks "does the top-level JSONB contain this key/element"
    # which works for both list[str] and dict[str, anything] shapes used
    # by the ingesters.
    if pillar:
        base = base.where(RiskEvent.risk_categories_json.op("?")(pillar))

    # Materialise distinct events once.  We use this set both for the
    # summary stats and for the paginated row list — avoiding two near-
    # identical queries.
    # Eager-load source_document + source when the caller wants to sort
    # by source_system — otherwise the sort comparator would N+1 fetch
    # per event.  Cheap because the only extra cost is two extra
    # selectinload queries (one for SourceDocuments, one for Sources)
    # regardless of result size.
    if sort_by == "source_system":
        base = base.options(
            selectinload(RiskEvent.source_document).selectinload(
                SourceDocument.source
            )
        )

    distinct_events = base.distinct(RiskEvent.id).order_by(RiskEvent.id)
    all_events = list(db.scalars(distinct_events).all())

    total = len(all_events)

    # ── Signal summary ───────────────────────────────────────────────
    high_severity = sum(
        1 for e in all_events
        if (e.severity_score or 0.0) >= _TAB_HIGH_SEVERITY_THRESHOLD
    )
    verified = sum(1 for e in all_events if e.verified)

    pillar_counts: dict[str, int] = {}
    for e in all_events:
        for p in _coerce_pillars(e.risk_categories_json):
            pillar_counts[p] = pillar_counts.get(p, 0) + 1

    summary = MaterialRiskEventsSummary(
        window_days=window_days,
        total_events=total,
        high_severity_count=high_severity,
        verified_count=verified,
        events_by_pillar=pillar_counts,
    )

    # ── Sort the paginated row list ─────────────────────────────────
    # Defaults: event_date desc (newest first).  Unknown sort_by falls
    # back to event_date.  Null values for the chosen column always sort
    # to the end regardless of direction — the analyst should never have
    # to scroll past blank rows to find data.
    if sort_by not in _SORTABLE_FIELDS:
        sort_by = "event_date"
    if sort_dir not in {"asc", "desc"}:
        sort_dir = "desc"
    descending = sort_dir == "desc"

    def _is_null(ev: RiskEvent) -> bool:
        if sort_by == "title":
            return not (ev.title or "").strip()
        if sort_by == "event_type":
            return not (ev.event_type or "").strip()
        if sort_by == "source_system":
            sd = ev.source_document
            return sd is None or sd.source is None
        if sort_by == "severity_score":
            return ev.severity_score is None
        if sort_by == "event_date":
            return ev.event_date is None
        return False

    def _value(ev: RiskEvent):
        if sort_by == "title":
            return (ev.title or "").lower()
        if sort_by == "event_type":
            return (ev.event_type or "").lower()
        if sort_by == "source_system":
            sd = ev.source_document
            return (sd.source.name if sd and sd.source else "").lower()
        if sort_by == "severity_score":
            return ev.severity_score or 0.0
        if sort_by == "event_date":
            # Use a comparable epoch number so NULL handling is symmetric.
            return ev.event_date.timestamp() if ev.event_date else 0.0
        return 0

    # Partition into has-value + nulls so nulls always trail.
    with_value = [e for e in all_events if not _is_null(e)]
    null_value = [e for e in all_events if _is_null(e)]
    with_value.sort(key=_value, reverse=descending)
    all_events = with_value + null_value

    start = (page - 1) * limit
    page_events = all_events[start:start + limit]
    page_event_ids = [e.id for e in page_events]

    # Bulk-fetch geographies + source for the page slice only — avoids
    # the N+1 that would happen if we eager-loaded on the materialised
    # set up top (we'd be loading source_document for events not on the
    # current page).
    geo_by_event: dict[int, list[str]] = {}
    if page_event_ids:
        geo_rows = db.execute(
            select(RiskEventGeography.risk_event_id, RiskEventGeography.country_code)
            .where(RiskEventGeography.risk_event_id.in_(page_event_ids))
        ).all()
        for eid, cc in geo_rows:
            geo_by_event.setdefault(eid, []).append((cc or "").upper())

    source_meta_by_event: dict[int, tuple[Optional[str], Optional[str]]] = {}
    if page_event_ids:
        meta_rows = db.execute(
            select(RiskEvent.id, Source.name, SourceDocument.url)
            .join(SourceDocument, SourceDocument.id == RiskEvent.source_document_id)
            .join(Source, Source.id == SourceDocument.source_id)
            .where(RiskEvent.id.in_(page_event_ids))
        ).all()
        for eid, sname, surl in meta_rows:
            source_meta_by_event[eid] = (sname, surl)

    rows: list[MaterialRiskEventRow] = []
    for e in page_events:
        sname, surl = source_meta_by_event.get(e.id, (None, None))
        rows.append(
            MaterialRiskEventRow(
                id=e.id,
                title=e.title,
                summary=e.summary,
                event_type=e.event_type,
                event_subtype=e.event_subtype,
                severity_score=e.severity_score,
                confidence_score=e.confidence_score,
                event_date=e.event_date,
                verified=e.verified,
                pillars_affected=_coerce_pillars(e.risk_categories_json),
                source_system=sname,
                source_url=surl,
                geography_codes=geo_by_event.get(e.id, []),
            )
        )

    return MaterialRiskEventsResponse(
        summary=summary,
        events=rows,
        total=total,
        page=page,
        limit=limit,
    )


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
    aggregates = _load_mapping_aggregates(db, [m.id for m in mappings])
    return [
        _annotate_mapping(m, mat, cross_mapped, aggregates)
        for m in mappings
    ]


# ---------------------------------------------------------------------------
# Country-material relevance — Phase 1 read endpoint
# ---------------------------------------------------------------------------

@router.get(
    "/materials/{material_id}/country-relevance",
    response_model=list[CountryMaterialRelevanceItem],
)
def list_material_country_relevance(
    material_id: int,
    role: Optional[str] = Query(
        None,
        pattern="^(producer|consumer)$",
        description="Filter to producer or consumer rows only.  Omit for both.",
    ),
    _user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[CountryMaterialRelevanceItem]:
    """List per-country relevance flags for one material.

    Returns producer rows auto-derived from material_production_shares
    plus consumer rows seeded as partner-curated placeholders. Each row
    carries:

      * country_code + country_name
      * role ('producer' | 'consumer')
      * tier ('top' | 'mid' | 'minor' | 'emerging' | null)
      * is_hcg (High-Concentration Geography flag, per-(material, country))
      * source ('mcs_share' | 'partner_curated' | 'derived')
      * derived_share + reference_year (where applicable)
      * notes

    Phase 1: read-only. Partner curation happens via the seed CLI today;
    a dedicated PATCH endpoint lands in Phase 2 when the scoring engine
    starts reading is_hcg from this table.
    """
    _get_material_or_404(db, material_id)

    q = (
        select(CountryMaterialRelevance, Country.name)
        .outerjoin(Country, Country.iso2 == CountryMaterialRelevance.country_code)
        .where(CountryMaterialRelevance.material_id == material_id)
    )
    if role is not None:
        q = q.where(CountryMaterialRelevance.role == role)
    q = q.order_by(
        CountryMaterialRelevance.role,
        # Producers sort by share descending so the top countries appear first;
        # consumers (no share) sort by country code.
        CountryMaterialRelevance.derived_share.desc().nullslast(),
        CountryMaterialRelevance.country_code,
    )

    rows = db.execute(q).all()
    return [
        CountryMaterialRelevanceItem(
            country_code=rel.country_code,
            country_name=country_name,
            role=rel.role,
            tier=rel.tier,
            is_hcg=rel.is_hcg,
            source=rel.source,
            derived_share=rel.derived_share,
            reference_year=rel.reference_year,
            notes=rel.notes,
        )
        for rel, country_name in rows
    ]


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
