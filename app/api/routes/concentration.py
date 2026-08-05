"""Supply Concentration transparency API — Phase 3b + Workstream A instrument.

The material-concentration pillar is data-derived: no events, nothing to
triage.  Its transparency surface is a DISPLAY view over the same computation
scoring uses — ``stage_concentration.score_material_concentration`` — plus
the pieces that computation deliberately leaves out of scoring but an analyst
needs on screen: STALE stage details, the would-have-been-binding comparison,
producer volumes, capacity streams, and the retired criticality context.

This is also the Workstream A freshness instrument (concentration-first
scoring plan §2/A2): the overview's audit tally over the launch list — fully
fresh / with stale stages / understated / no stage data — IS the freshness
exposure report, and ``?as_of=`` pins it for a point-in-time export.

Design rules:

* NOTHING here re-implements scoring policy.  Fresh-stage sub-scores,
  per-geo maxima, and the governance amplifier come straight from
  ``compute_stage_concentration``.  The one computation added — stale-stage
  detail and the understatement comparison — reuses the engine's own
  snapshot/dedupe helpers on the same loaded rows, and compares against the
  engine's ``raw_score`` (pre-amplifier), matching the approved mockup.
* Stale-stage HHIs are computed with the same single-vintage rule as fresh
  ones: an HHI must never mix reference years.
* Missing data is absent or zero, never a default — a geography with no WGI
  row is served with ``wgi_pct: null`` so the UI can say "no usable
  governance signal" instead of implying stability.
"""

from __future__ import annotations

import math
from datetime import date
from typing import Optional

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.models import CountryGovernanceSignal
from app.models.country import Country
from app.models.criticality_signal import MaterialCriticalitySignal
from app.models.supply import (
    HsCodeMaterialMapping,
    HsCodeProductionShare,
    Material,
    MaterialCapacityShare,
    MaterialProductionShare,
)
from app.services.scoring.launch_list import is_launch_list_material
from app.services.scoring.material_risk import hhi_concentration_risk
from app.services.scoring.stage_concentration import (
    CONCENTRATION_STAGES,
    FRESHNESS_YEARS,
    GOVERNANCE_AMPLIFIER_BETA,
    ShareRow,
    compute_stage_concentration,
    _load_wgi_by_geo,
)

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/concentration", tags=["concentration"])

# Presentation order for stages — supply-chain sequence, mirrored by the UI.
_STAGE_ORDER = ["ore", "concentrate", "intermediate", "refined", "battery_grade"]


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------

class StageDetailOut(BaseModel):
    stage: str
    reference_year: int
    source: Optional[str] = None
    hhi_raw: float
    hhi_cliff: float
    fresh: bool
    age_years: int
    # country -> share for the stage's single-vintage snapshot.
    shares: dict[str, float]
    conflicts: list[str] = Field(default_factory=list)


class StaleBestOut(BaseModel):
    stage: str
    reference_year: int
    score: float


class GeoResultOut(BaseModel):
    # Pre-amplifier stage-max.  Comparisons (understatement) use this, matching
    # the engine, which amplifies only the final max.
    raw_score: float
    score: float
    binding_stage: Optional[str] = None
    sub_scores: dict[str, float] = Field(default_factory=dict)
    wgi_pct: Optional[float] = None
    instability: Optional[float] = None
    amplified: bool = False
    stale_best: Optional[StaleBestOut] = None
    understated: bool = False


class ProducerOut(BaseModel):
    country_code: str
    production_share: float
    production_volume: Optional[float] = None
    reference_year: int
    unit_of_measure: Optional[str] = None
    source: Optional[str] = None


class CapacityRowOut(BaseModel):
    detail_type: str
    country_code: str
    capacity_share: Optional[float] = None
    capacity_volume: Optional[float] = None
    reference_year: int


class CriticalityContextOut(BaseModel):
    """Retired from the pillar (V1 stage-max) — served as labelled context."""
    source: Optional[str] = None
    reference_year: Optional[int] = None
    hhi_score: Optional[float] = None
    reserve_hhi_score: Optional[float] = None
    reserve_life_index: Optional[float] = None
    capacity_utilization: Optional[float] = None
    production_yoy_pct: Optional[float] = None


class PriorOreHhiOut(BaseModel):
    reference_year: int
    hhi_raw: float


class MaterialConcentrationDetail(BaseModel):
    material_id: int
    name: str
    symbol: Optional[str] = None
    is_launch_list: bool
    unit: Optional[str] = None
    as_of: date
    freshness_years: int
    governance_beta: float
    wgi_vintage: Optional[int] = None
    stages: list[StageDetailOut]
    per_geo: dict[str, GeoResultOut]
    driving_geo: Optional[str] = None
    headline_understated: bool
    producers: list[ProducerOut]
    capacity: list[CapacityRowOut]
    criticality: Optional[CriticalityContextOut] = None
    prior_ore_hhi: Optional[PriorOreHhiOut] = None
    country_names: dict[str, str] = Field(default_factory=dict)


class OverviewOreOut(BaseModel):
    hhi_raw: float
    hhi_cliff: float
    reference_year: int
    fresh: bool


class OverviewRowOut(BaseModel):
    material_id: int
    name: str
    symbol: Optional[str] = None
    is_launch_list: bool
    # fresh | stale | understated | nostage — the audit strip's four states.
    audit_state: str
    driving_geo: Optional[str] = None
    score: Optional[float] = None
    binding_stage: Optional[str] = None
    binding_year: Optional[int] = None
    ore: Optional[OverviewOreOut] = None
    prior_ore_hhi: Optional[PriorOreHhiOut] = None
    stale_stages: list[StaleBestOut] = Field(default_factory=list)
    understated_geo_count: int = 0
    reserve_life_index: Optional[float] = None
    top_production: list[ProducerOut] = Field(default_factory=list)


class AuditTallyOut(BaseModel):
    """Launch-list freshness tally — the Workstream A readout."""
    fully_fresh: int
    with_stale: int
    understated: int
    no_stage: int


class UncoveredOut(BaseModel):
    material_id: int
    name: str
    symbol: Optional[str] = None


class ConcentrationOverview(BaseModel):
    as_of: date
    freshness_years: int
    governance_beta: float
    wgi_vintage: Optional[int] = None
    audit: AuditTallyOut
    items: list[OverviewRowOut]
    uncovered: list[UncoveredOut]
    country_names: dict[str, str] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Computation helpers
# ---------------------------------------------------------------------------

def _load_rows_with_source_bulk(
    db: Session, material_ids: list[int]
) -> dict[int, tuple[list[ShareRow], dict[int, Optional[str]]]]:
    """Superset of ``stage_concentration.load_share_rows``, batched.

    Same rows and filters as the engine's loader, plus each observation's
    ``source`` (so the UI can attribute a snapshot: USGS at ore, benchmark
    downstream) — fetched for EVERY requested material in ONE query and
    grouped in Python.  The overview used to call the single-material loader
    in a loop, which multiplied into hundreds of round trips against a remote
    Postgres (~11s page loads); the round trips, not the row volume, were the
    cost.
    """
    if not material_ids:
        return {}
    result = db.execute(
        select(
            HsCodeMaterialMapping.material_id,
            HsCodeMaterialMapping.supply_chain_stage,
            HsCodeProductionShare.country_code,
            HsCodeProductionShare.production_share,
            HsCodeProductionShare.reference_year,
            HsCodeMaterialMapping.digit_count,
            HsCodeProductionShare.hs_mapping_id,
            HsCodeProductionShare.source,
        )
        .join(
            HsCodeMaterialMapping,
            HsCodeMaterialMapping.id == HsCodeProductionShare.hs_mapping_id,
        )
        .where(
            HsCodeMaterialMapping.material_id.in_(material_ids),
            HsCodeProductionShare.market_scope == "global",
            HsCodeMaterialMapping.supply_chain_stage.is_not(None),
        )
    ).all()
    out: dict[int, tuple[list[ShareRow], dict[int, Optional[str]]]] = {
        mid: ([], {}) for mid in material_ids
    }
    for r in result:
        rows, sources = out[int(r[0])]
        rows.append(ShareRow(
            stage=r[1], country_code=r[2], production_share=float(r[3]),
            reference_year=int(r[4]), digit_count=int(r[5]), hs_mapping_id=int(r[6]),
        ))
        sources[int(r[6])] = r[7]
    return out


def _load_rows_with_source(
    db: Session, material_id: int
) -> tuple[list[ShareRow], dict[int, Optional[str]]]:
    """Single-material convenience over the bulk loader (detail endpoint)."""
    return _load_rows_with_source_bulk(db, [material_id])[material_id]


def _snapshot_detail(
    stage: str, stage_rows: list[ShareRow], as_of: date,
    sources: dict[int, Optional[str]],
) -> StageDetailOut:
    """One stage's single-vintage snapshot, deduped the way the engine dedupes.

    Mirrors ``compute_stage_concentration``'s latest-year selection and
    prefer-most-specific-mapping rule so stale stages get EXACTLY the numbers
    they would have had if fresh — anything else would make the understatement
    comparison apples-to-oranges.
    """
    latest_year = max(r.reference_year for r in stage_rows)
    snapshot = [r for r in stage_rows if r.reference_year == latest_year]

    best: dict[str, ShareRow] = {}
    conflicts: list[str] = []
    for r in snapshot:
        cur = best.get(r.country_code)
        if cur is None or r.digit_count > cur.digit_count:
            best[r.country_code] = r
        elif (
            r.digit_count == cur.digit_count
            and abs(r.production_share - cur.production_share) > 1e-9
        ):
            conflicts.append(
                f"{stage}/{r.country_code}: mappings {cur.hs_mapping_id} vs "
                f"{r.hs_mapping_id} disagree; kept max"
            )
            if r.production_share > cur.production_share:
                best[r.country_code] = r

    shares = {c: r.production_share for c, r in best.items()}
    hhi_raw = sum(s * s for s in shares.values())
    age = as_of.year - latest_year
    src_values = {sources.get(r.hs_mapping_id) for r in best.values()}
    src_values.discard(None)
    return StageDetailOut(
        stage=stage,
        reference_year=latest_year,
        source=sorted(src_values)[0] if src_values else None,
        hhi_raw=hhi_raw,
        hhi_cliff=hhi_concentration_risk(hhi_raw),
        fresh=age <= FRESHNESS_YEARS,
        age_years=age,
        shares=shares,
        conflicts=conflicts,
    )


def _material_concentration(
    db: Session, material_id: int, as_of: date
) -> tuple[list[StageDetailOut], dict[str, GeoResultOut], Optional[str], bool, Optional[PriorOreHhiOut]]:
    """Full stage + per-geo picture for one material (detail endpoint)."""
    rows, sources = _load_rows_with_source(db, material_id)
    wgi = _load_wgi_by_geo(db, {r.country_code for r in rows})
    return _material_concentration_from_rows(rows, sources, wgi, as_of)


def _material_concentration_from_rows(
    rows: list[ShareRow],
    sources: dict[int, Optional[str]],
    wgi: dict[str, float],
    as_of: date,
) -> tuple[list[StageDetailOut], dict[str, GeoResultOut], Optional[str], bool, Optional[PriorOreHhiOut]]:
    """Full stage + per-geo picture from preloaded rows, stale stages included.

    Pure computation — no queries — so the overview can preload everything in
    a fixed number of round trips and call this per material.
    """
    # The engine's own result carries the fresh side: sub-scores, maxima,
    # amplifier diagnostics.  Never recompute what it already decides.
    engine = compute_stage_concentration(rows, as_of, wgi_by_geo=wgi)

    by_stage: dict[str, list[ShareRow]] = {}
    for r in rows:
        if r.stage in CONCENTRATION_STAGES and r.production_share > 0:
            by_stage.setdefault(r.stage, []).append(r)

    details = [
        _snapshot_detail(stage, stage_rows, as_of, sources)
        for stage, stage_rows in by_stage.items()
    ]
    details.sort(key=lambda d: _STAGE_ORDER.index(d.stage) if d.stage in _STAGE_ORDER else 99)

    stale_details = [d for d in details if not d.fresh]

    # Per-geo: engine result for fresh producers, extended with (a) geos that
    # only appear at stale stages (they score 0 today, which is the point) and
    # (b) the stale-best / understatement comparison per geo.
    geos = {c for d in details for c in d.shares}
    per_geo: dict[str, GeoResultOut] = {}
    for geo in geos:
        eng = engine.per_geo.get(geo)
        pct = wgi.get(geo)
        instability = None if pct is None else 1.0 - min(max(float(pct), 0.0), 100.0) / 100.0

        stale_best: Optional[StaleBestOut] = None
        for d in stale_details:
            share = d.shares.get(geo)
            if not share or share <= 0:
                continue
            s = d.hhi_cliff * math.sqrt(share) * 100.0
            if stale_best is None or s > stale_best.score:
                stale_best = StaleBestOut(stage=d.stage, reference_year=d.reference_year, score=s)

        raw = eng.raw_score if eng else 0.0
        per_geo[geo] = GeoResultOut(
            raw_score=raw,
            score=eng.score if eng else 0.0,
            binding_stage=eng.driving_stage if eng else None,
            sub_scores=dict(eng.sub_scores) if eng else {},
            wgi_pct=float(pct) if pct is not None else None,
            instability=instability,
            amplified=bool(eng and eng.governance),
            stale_best=stale_best,
            understated=bool(stale_best and stale_best.score > raw),
        )

    driving_geo = max(per_geo, key=lambda g: per_geo[g].score) if per_geo else None
    headline_understated = bool(driving_geo and per_geo[driving_geo].understated)

    # Prior ore HHI for the YoY delta — previous vintage of the same series
    # the stage table shows (NOT the retired MCS criticality hhi_score).
    prior: Optional[PriorOreHhiOut] = None
    ore_rows = by_stage.get("ore") or []
    if ore_rows:
        latest = max(r.reference_year for r in ore_rows)
        earlier = [r for r in ore_rows if r.reference_year < latest]
        if earlier:
            prior_detail = _snapshot_detail("ore", earlier, as_of, sources)
            prior = PriorOreHhiOut(
                reference_year=prior_detail.reference_year, hhi_raw=prior_detail.hhi_raw
            )

    return details, per_geo, driving_geo, headline_understated, prior


def _audit_state(details: list[StageDetailOut], headline_understated: bool) -> str:
    if not details:
        return "nostage"
    if headline_understated:
        return "understated"
    if any(not d.fresh for d in details):
        return "stale"
    return "fresh"


def _wgi_vintage(db: Session) -> Optional[int]:
    v = db.scalar(select(func.max(CountryGovernanceSignal.reference_year)))
    return int(v) if v is not None else None


def _country_names(db: Session, codes: set[str]) -> dict[str, str]:
    if not codes:
        return {}
    rows = db.execute(
        select(Country.iso2, Country.name).where(Country.iso2.in_(codes))
    ).all()
    return {iso2: name for iso2, name in rows}


def _latest_producers_bulk(
    db: Session, material_ids: list[int]
) -> dict[int, list[ProducerOut]]:
    """Latest-vintage production rows per material, largest share first.

    One query for the whole material set; the latest-year selection happens in
    Python.  Row volume is modest (materials × countries × a few vintages);
    the per-material max-year + rows pair of queries was 2×N round trips.
    """
    if not material_ids:
        return {}
    rows = db.scalars(
        select(MaterialProductionShare)
        .where(MaterialProductionShare.material_id.in_(material_ids))
        .order_by(
            MaterialProductionShare.material_id,
            MaterialProductionShare.reference_year.desc(),
            MaterialProductionShare.production_share.desc(),
        )
    ).all()
    latest_year: dict[int, int] = {}
    out: dict[int, list[ProducerOut]] = {mid: [] for mid in material_ids}
    for r in rows:
        mid = int(r.material_id)
        year = int(r.reference_year)
        if mid not in latest_year:
            latest_year[mid] = year
        if year != latest_year[mid]:
            continue
        out[mid].append(ProducerOut(
            country_code=r.country_code,
            production_share=float(r.production_share),
            production_volume=r.production_volume,
            reference_year=year,
            unit_of_measure=r.unit_of_measure,
            source=r.data_source,
        ))
    return out


def _latest_producers(db: Session, material_id: int) -> list[ProducerOut]:
    """Single-material convenience over the bulk loader (detail endpoint)."""
    return _latest_producers_bulk(db, [material_id])[material_id]


def _latest_criticality_bulk(
    db: Session, material_ids: list[int]
) -> dict[int, MaterialCriticalitySignal]:
    """Latest criticality-signal row per material, one query for the set."""
    if not material_ids:
        return {}
    rows = db.scalars(
        select(MaterialCriticalitySignal)
        .where(MaterialCriticalitySignal.material_id.in_(material_ids))
        .order_by(
            MaterialCriticalitySignal.material_id,
            MaterialCriticalitySignal.reference_year.desc(),
        )
    ).all()
    out: dict[int, MaterialCriticalitySignal] = {}
    for r in rows:
        out.setdefault(int(r.material_id), r)  # first per material = latest year
    return out


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/overview", response_model=ConcentrationOverview)
def concentration_overview(
    as_of: Optional[date] = Query(
        None,
        description="Scoring as-of date; defaults to today. Pin it to make the "
        "audit tally a reproducible point-in-time artifact.",
    ),
    _user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ConcentrationOverview:
    as_of_date = as_of or date.today()

    materials = db.execute(
        select(Material.id, Material.canonical_name, Material.symbol_or_code)
        .order_by(Material.canonical_name)
    ).all()

    # Fixed number of round trips regardless of registry size.  The first
    # version of this endpoint looped the single-material loaders (~5 queries
    # × N materials ≈ 200+ round trips against a remote Postgres ≈ 11-second
    # page loads).  Everything below is preloaded in one query each; the
    # per-material work is pure computation.
    material_ids = [m.id for m in materials]
    rows_by_material = _load_rows_with_source_bulk(db, material_ids)
    producers_by_material = _latest_producers_bulk(db, material_ids)
    crit_by_material = _latest_criticality_bulk(db, material_ids)
    wgi = _load_wgi_by_geo(
        db,
        {r.country_code for rows, _ in rows_by_material.values() for r in rows},
    )

    items: list[OverviewRowOut] = []
    uncovered: list[UncoveredOut] = []
    tally = AuditTallyOut(fully_fresh=0, with_stale=0, understated=0, no_stage=0)
    all_codes: set[str] = set()

    for m in materials:
        producers = producers_by_material[m.id]
        rows, sources = rows_by_material[m.id]
        details, per_geo, driving_geo, headline_understated, prior = (
            _material_concentration_from_rows(rows, sources, wgi, as_of_date)
        )

        # A registry row with neither stage shares nor producer rows is a
        # coverage hole, listed rather than omitted — "no producer data" is
        # the finding, and hiding it makes coverage look better than it is.
        if not details and not producers:
            uncovered.append(
                UncoveredOut(material_id=m.id, name=m.canonical_name, symbol=m.symbol_or_code)
            )
            continue

        launch = is_launch_list_material(m.canonical_name)
        state = _audit_state(details, headline_understated)
        if launch:
            if state == "nostage":
                tally.no_stage += 1
            elif state == "understated":
                tally.understated += 1
                tally.with_stale += 1
            elif state == "stale":
                tally.with_stale += 1
            else:
                tally.fully_fresh += 1

        ore = next((d for d in details if d.stage == "ore"), None)
        driving = per_geo.get(driving_geo) if driving_geo else None
        top = producers[:8]
        all_codes.update(p.country_code for p in top)
        if driving_geo:
            all_codes.add(driving_geo)

        # Reserve life is context only under V1 — served for the overview
        # column, labelled as non-scoring by the UI.
        crit = crit_by_material.get(m.id)

        items.append(OverviewRowOut(
            material_id=m.id,
            name=m.canonical_name,
            symbol=m.symbol_or_code,
            is_launch_list=launch,
            audit_state=state,
            driving_geo=driving_geo,
            score=driving.score if driving else None,
            binding_stage=driving.binding_stage if driving else None,
            binding_year=next(
                (d.reference_year for d in details if driving and d.stage == driving.binding_stage),
                None,
            ),
            ore=OverviewOreOut(
                hhi_raw=ore.hhi_raw, hhi_cliff=ore.hhi_cliff,
                reference_year=ore.reference_year, fresh=ore.fresh,
            ) if ore else None,
            prior_ore_hhi=prior,
            stale_stages=[
                StaleBestOut(stage=d.stage, reference_year=d.reference_year, score=0.0)
                for d in details if not d.fresh
            ],
            understated_geo_count=sum(1 for g in per_geo.values() if g.understated),
            reserve_life_index=(
                float(crit.reserve_life_index)
                if crit is not None and crit.reserve_life_index is not None
                else None
            ),
            top_production=top,
        ))

    return ConcentrationOverview(
        as_of=as_of_date,
        freshness_years=FRESHNESS_YEARS,
        governance_beta=GOVERNANCE_AMPLIFIER_BETA,
        wgi_vintage=_wgi_vintage(db),
        audit=tally,
        items=items,
        uncovered=uncovered,
        country_names=_country_names(db, all_codes),
    )


@router.get("/materials/{material_id}", response_model=MaterialConcentrationDetail)
def concentration_detail(
    material_id: int,
    as_of: Optional[date] = Query(None),
    _user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> MaterialConcentrationDetail:
    as_of_date = as_of or date.today()

    m = db.execute(
        select(Material.id, Material.canonical_name, Material.symbol_or_code)
        .where(Material.id == material_id)
    ).first()
    if m is None:
        raise HTTPException(status_code=404, detail="Material not found")

    details, per_geo, driving_geo, headline_understated, prior = _material_concentration(
        db, material_id, as_of_date
    )
    producers = _latest_producers(db, material_id)

    cap_rows = db.scalars(
        select(MaterialCapacityShare)
        .where(MaterialCapacityShare.material_id == material_id)
        .order_by(
            MaterialCapacityShare.detail_type,
            MaterialCapacityShare.reference_year.desc(),
            MaterialCapacityShare.capacity_share.desc().nulls_last(),
        )
    ).all()
    # Latest vintage per detail_type — streams must never mix years either.
    latest_by_type: dict[str, int] = {}
    for c in cap_rows:
        if c.detail_type not in latest_by_type:
            latest_by_type[c.detail_type] = int(c.reference_year)
    capacity = [
        CapacityRowOut(
            detail_type=c.detail_type,
            country_code=c.country_code,
            capacity_share=float(c.capacity_share) if c.capacity_share is not None else None,
            capacity_volume=c.capacity_volume,
            reference_year=int(c.reference_year),
        )
        for c in cap_rows
        if int(c.reference_year) == latest_by_type[c.detail_type]
    ]

    crit = db.scalars(
        select(MaterialCriticalitySignal)
        .where(MaterialCriticalitySignal.material_id == material_id)
        .order_by(MaterialCriticalitySignal.reference_year.desc())
        .limit(1)
    ).first()

    codes = {c for d in details for c in d.shares}
    codes.update(p.country_code for p in producers)
    codes.update(c.country_code for c in capacity)

    return MaterialConcentrationDetail(
        material_id=m.id,
        name=m.canonical_name,
        symbol=m.symbol_or_code,
        is_launch_list=is_launch_list_material(m.canonical_name),
        unit=producers[0].unit_of_measure if producers else None,
        as_of=as_of_date,
        freshness_years=FRESHNESS_YEARS,
        governance_beta=GOVERNANCE_AMPLIFIER_BETA,
        wgi_vintage=_wgi_vintage(db),
        stages=details,
        per_geo=per_geo,
        driving_geo=driving_geo,
        headline_understated=headline_understated,
        producers=producers,
        capacity=capacity,
        criticality=CriticalityContextOut(
            source=crit.source,
            reference_year=int(crit.reference_year) if crit.reference_year is not None else None,
            hhi_score=crit.hhi_score,
            reserve_hhi_score=crit.reserve_hhi_score,
            reserve_life_index=crit.reserve_life_index,
            capacity_utilization=crit.capacity_utilization,
            production_yoy_pct=crit.production_yoy_pct,
        ) if crit is not None else None,
        prior_ore_hhi=prior,
        country_names=_country_names(db, codes),
    )
