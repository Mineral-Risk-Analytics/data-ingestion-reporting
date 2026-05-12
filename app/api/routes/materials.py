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
from app.models.regulatory import RiskEventHsMapping
from app.models.reporting import AnalystNote
from app.models.scoring import HsCodeGeographyRiskScore, MaterialGlobalRiskScore
from app.models.supply import (
    HsCodeMaterialMapping,
    HsCodeProductionShare,
    Material,
    MaterialProductionShare,
)
from app.schemas.common import PaginatedResponse, VerifiedResponse, VerifiedUpdate
from app.schemas.materials import (
    CountryShareItem,
    HsMappingGeographyRead,
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
    aggregates = _load_mapping_aggregates(db, [m.id for m in mappings])
    return [
        _annotate_mapping(m, mat, cross_mapped, aggregates)
        for m in mappings
    ]


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
