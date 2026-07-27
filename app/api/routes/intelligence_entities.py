"""Public Intelligence Hub entity routes — Companies + Regulations.

Extends the Phase-4 public content site (see ``intelligence.py``) with the
entity pages mocked in the Mineral Risk Analytics design system:

    GET /intelligence/companies                 browse (search / stage / band)
    GET /intelligence/companies/{slug}          profile
    GET /intelligence/regulations               browse (search / theme / status)
    GET /intelligence/regulations/{key}         detail

All four are PUBLIC (no auth) and read-only, matching the published-posts
endpoints.  Admin CRUD stays on the existing ``companies.py`` /
``regulations.py`` routers — these routes never expose internal fields
(notes, verification state, unpublished companies).

Key contracts (full rationale in ``app/schemas/intelligence_entities.py``):
  * companies filtered on ``is_published`` (migration 054)
  * exposure risk = latest L1 material_geography_risk_scores for the
    exposure's (material, source_geography); band via bands.py
  * linked posts are tag-driven (canonical name / regulation_key in
    ``insight_posts.tags``)
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.models.country import Country
from app.models.company import (
    Company,
    CompanyMaterialExposure,
    CompanyScore,
)
from app.models.facility import CompanyFacility, Facility, FacilityMaterialLink
from app.models.intelligence import InsightPost
from app.models.regulatory import (
    Regulation,
    RegulationGeographyScope,
    RegulationMaterialScope,
    RiskEvent,
    RiskEventCompany,
    RiskEventRegulation,
)
from app.models.documents import SourceDocument
from app.models.scoring import MaterialGeographyRiskScore, MaterialGlobalRiskScore
from app.models.supply import Material
from app.schemas.common import PaginatedResponse
from app.schemas.intelligence_entities import (
    CompanyCountryFacilitiesOut,
    FacilityDetailOut,
    FacilityMaterialTag,
    CompanyFactOut,
    ComplianceWeightOut,
    ExposureOut,
    GeoFootprintMaterialOut,
    GeoFootprintOut,
    GeographyScopeOut,
    LinkedEventOut,
    LinkedPostOut,
    MaterialScopeOut,
    PublicCompanyListItem,
    PublicCompanyProfile,
    PublicRegulationDetail,
    PublicRegulationListItem,
    RiskBandOut,
    TimelineNodeOut,
)
from app.services.scoring.bands import score_to_band
from app.services.scoring.event_impact import SCOPE_SEVERITY_MULTIPLIER

router = APIRouter(prefix="/intelligence", tags=["intelligence-entities"])


# ---------------------------------------------------------------------------
# Display vocabularies — translated ONCE, server-side.  The frontend never
# learns internal enum values.
# ---------------------------------------------------------------------------

# supply_chain_stages.stage_code / CME stage → public label
_STAGE_LABELS: dict[str, str] = {
    # activity-axis codes (companies.primary_activity_stage_fk)
    "mining": "Mining",
    "beneficiation": "Beneficiation",
    "refining": "Refining",
    "precursor_production": "Precursor",
    "cathode_active_material": "Cathode",
    "anode_active_material": "Anode",
    "electrolyte_production": "Electrolyte",
    "separator_production": "Separator",
    "cell_making": "Cell",
    "module_assembly": "Module",
    "pack_assembly": "Pack",
    "vehicle_assembly": "OEM",
    "recycling": "Recycling",
    "trading": "Trading",
    "integrated": "Integrated",
    "financial": "Financial",
    # CME vocabulary (company_material_exposures.supply_chain_stage)
    "cell": "Cell",
    "pack": "Pack",
    "oem": "OEM",
}

# bands.py band → display label + CSS level token (mockup vocabulary)
_BAND_DISPLAY: dict[str, tuple[str, str]] = {
    "LOW": ("Low", "low"),
    "MOD": ("Moderate", "med"),
    "HIGH": ("High", "high"),
    "CRIT": ("Critical", "crit"),
}

# regulations.status → display + level token
_REG_STATUS_DISPLAY: dict[str, tuple[str, str]] = {
    "effective": ("In force", "inforce"),
    "enacted": ("Enacted", "inforce"),
    "proposed": ("Proposed", "proposed"),
    "pending": ("Pending", "pending"),
    "superseded": ("Superseded", "ended"),
    # 2026-07-26: complete the workbook status vocabulary. "stayed" =
    # adopted but court-blocked, never in force (SEC_CLIMATE_2024);
    # "archived" = removed from the curated set by the workbook sync.
    "stayed": ("Stayed", "ended"),
    "archived": ("Archived", "ended"),
}


def _editorial_out(metadata_json):
    """Build EditorialOut from metadata_json["editorial"] (workbook-curated).
    Returns None when absent so the frontend can fall back to summary."""
    from app.schemas.intelligence_entities import EditorialOut, FurtherReadingOut
    ed = (metadata_json or {}).get("editorial")
    if not isinstance(ed, dict):
        return None
    fr = [
        FurtherReadingOut(
            title=x.get("title", ""), publisher=x.get("publisher", ""),
            url=x.get("url", ""),
        )
        for x in ed.get("further_reading", [])
        if isinstance(x, dict) and x.get("title") and x.get("url")
    ]
    sections = {
        k: v for k, v in (ed.get("sections") or {}).items()
        if isinstance(v, str) and v.strip()
    }
    if not (ed.get("standfirst") or sections or fr):
        return None
    return EditorialOut(
        standfirst=ed.get("standfirst"), sections=sections, further_reading=fr,
    )

# regulations.policy_theme → public display label (2026-07-15, Nicole).
# snake_case normalized server-side; 'pending_review' is an INTERNAL
# placeholder (10/19 rows await manual triage) — displayed as None so the
# frontend renders "—" and excludes it from filter chips.
_THEME_DISPLAY: dict[str, str] = {
    "supply_chain_due_diligence": "Supply chain due diligence",
    "supply_chain_resilience": "Supply chain resilience",
    "responsible_sourcing": "Responsible sourcing",
    "battery_lifecycle_compliance": "Battery lifecycle compliance",
    "domestic_content_incentives": "Domestic content incentives",
    "carbon_pricing": "Carbon pricing",
    "climate_disclosure": "Climate disclosure",
    "chemicals_regulatory": "Chemicals regulatory",
}


def _theme_display(raw: Optional[str]) -> Optional[str]:
    if not raw or raw.strip().lower() == "pending_review":
        return None
    key = raw.strip().lower()
    return _THEME_DISPLAY.get(key, key.replace("_", " ").capitalize())


# facilities.status → level token for the profile chips
_FACILITY_STATUS_LEVELS: dict[str, str] = {
    "operating": "op",
    "ramp_up": "ramp",
    "ramp-up": "ramp",
    "construction": "build",
    "under_construction": "build",
    "planned": "build",
    "announced": "build",
    "idled": "idle",
    "suspended": "idle",
    "care_and_maintenance": "idle",
    "care_maintenance": "idle",
    "mothballed": "idle",
    "closed": "closed",
    "divested": "closed",
}


def _stage_label(code: Optional[str]) -> Optional[str]:
    if not code:
        return None
    return _STAGE_LABELS.get(code.strip().lower(), code.replace("_", " ").title())


def _band_out(score_0_100: Optional[float]) -> Optional[RiskBandOut]:
    band = score_to_band(score_0_100)
    if band is None:
        return None
    label, level = _BAND_DISPLAY[band]
    # normalize the numeric to 0-100 for display regardless of input axis
    s = float(score_0_100)
    if 0.0 <= s <= 1.0:
        s *= 100.0
    return RiskBandOut(label=label, level=level, score=round(s, 1))


def _reg_status(raw: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    if not raw:
        return None, None
    display, level = _REG_STATUS_DISPLAY.get(
        raw.strip().lower(), (raw.replace("_", " ").title(), "pending")
    )
    return display, level


# ---------------------------------------------------------------------------
# Shared subqueries / helpers
# ---------------------------------------------------------------------------

def _latest_company_score_sq():
    """(company_id, overall_risk_score) for each company's newest score row."""
    ranked = (
        select(
            CompanyScore.company_id,
            CompanyScore.overall_risk_score,
            func.row_number()
            .over(
                partition_by=CompanyScore.company_id,
                order_by=(CompanyScore.as_of_date.desc(), CompanyScore.id.desc()),
            )
            .label("rn"),
        )
    ).subquery()
    return (
        select(ranked.c.company_id, ranked.c.overall_risk_score)
        .where(ranked.c.rn == 1)
        .subquery()
    )


def _tagged_posts(db: Session, tag: str, limit: int = 6) -> list[LinkedPostOut]:
    """Published posts whose tags array contains ``tag`` (editorial contract)."""
    rows = db.scalars(
        select(InsightPost)
        .where(
            InsightPost.status == "published",
            InsightPost.tags.isnot(None),
            # same containment idiom as intelligence.py's _array_contains —
            # PG `@> ARRAY[tag]` natively, JSON containment under the
            # SQLite test shim.
            InsightPost.tags.contains([tag]),
        )
        .order_by(InsightPost.published_at.desc().nulls_last())
        .limit(limit)
    ).all()
    return [LinkedPostOut.model_validate(p) for p in rows]


# ---------------------------------------------------------------------------
# Companies — browse
# ---------------------------------------------------------------------------

@router.get(
    "/companies",
    response_model=PaginatedResponse[PublicCompanyListItem],
    summary="Public company browse (published companies only)",
)
def list_public_companies(
    q: Optional[str] = Query(None, description="Search canonical or legal name."),
    stage: Optional[str] = Query(
        None, description="Display stage filter, e.g. 'Refining' (case-insensitive)."
    ),
    band: Optional[str] = Query(
        None, description="Band filter: low | med | high | crit."
    ),
    page: int = Query(1, ge=1),
    limit: int = Query(25, ge=1, le=100),
    db: Session = Depends(get_db),
) -> PaginatedResponse[PublicCompanyListItem]:
    score_sq = _latest_company_score_sq()

    stmt = (
        select(Company, score_sq.c.overall_risk_score)
        .join(score_sq, score_sq.c.company_id == Company.id, isouter=True)
        .where(Company.is_published.is_(True))
    )
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(
            or_(Company.canonical_name.ilike(like), Company.legal_name.ilike(like))
        )
    if stage:
        # match against the raw FK code whose display label equals the filter
        wanted = stage.strip().lower()
        codes = [c for c, lbl in _STAGE_LABELS.items() if lbl.lower() == wanted]
        if codes:
            stmt = stmt.where(Company.primary_activity_stage_fk.in_(codes))
        else:
            stmt = stmt.where(Company.primary_activity_stage_fk == stage)

    rows = db.execute(stmt.order_by(Company.canonical_name)).all()

    # top-3 materials per company from CME, by exposure_score
    company_ids = [c.id for c, _ in rows]
    mats_by_company: dict[uuid.UUID, list[str]] = {}
    if company_ids:
        mat_rows = db.execute(
            select(
                CompanyMaterialExposure.company_id,
                Material.canonical_name,
                func.max(CompanyMaterialExposure.exposure_score).label("mx"),
            )
            .join(Material, Material.id == CompanyMaterialExposure.material_id)
            .where(CompanyMaterialExposure.company_id.in_(company_ids))
            .group_by(CompanyMaterialExposure.company_id, Material.canonical_name)
            .order_by(func.max(CompanyMaterialExposure.exposure_score).desc())
        ).all()
        for cid, mat, _mx in mat_rows:
            bucket = mats_by_company.setdefault(cid, [])
            if len(bucket) < 3:
                bucket.append(mat)

    items: list[PublicCompanyListItem] = []
    for company, overall in rows:
        b = _band_out(overall)
        if band and (b is None or b.level != band.strip().lower()):
            continue
        items.append(PublicCompanyListItem(
            slug=company.slug or str(company.id),
            name=company.canonical_name,
            legal_name=company.legal_name,
            stage_label=_stage_label(company.primary_activity_stage_fk),
            hq_country=company.headquarters_country,
            materials=mats_by_company.get(company.id, []),
            band=b,
        ))

    total = len(items)
    start = (page - 1) * limit
    return PaginatedResponse(
        data=items[start:start + limit], total=total, page=page, limit=limit,
    )


# ---------------------------------------------------------------------------
# Companies — profile
# ---------------------------------------------------------------------------

@router.get(
    "/companies/{slug}",
    response_model=PublicCompanyProfile,
    summary="Public company profile",
)
def get_public_company(slug: str, db: Session = Depends(get_db)) -> PublicCompanyProfile:
    company = db.scalar(
        select(Company).where(Company.slug == slug, Company.is_published.is_(True))
    )
    if company is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="company not found"
        )

    # ── band from latest score ────────────────────────────────────────
    overall = db.scalar(
        select(CompanyScore.overall_risk_score)
        .where(CompanyScore.company_id == company.id)
        .order_by(CompanyScore.as_of_date.desc(), CompanyScore.id.desc())
        .limit(1)
    )
    band = _band_out(overall)

    # ══ "market + map" display architecture (2026-07-21 v4, Nicole) ═══
    # After three same-day iterations on the exposure list's score basis
    # (v1 at-source, v2 global-only, v3 at-source+captions), the page now
    # has ONE score basis per section, no fallback mixing anywhere:
    #   header band (Phase 4)  -> this company overall
    #   material exposure      -> GLOBAL material rollup (market view,
    #                             sidebar-consistent; one deduped row per
    #                             material, stages combined)
    #   geographic footprint   -> material×geography L1 AT each country
    #                             the company operates or sources in
    # The v3 mixed-basis column failed concretely: Glencore's cobalt bar
    # meant "risk at CD" while its nickel bar meant "global fallback" —
    # while the page itself displayed the CA nickel mine the fallback
    # claimed not to know about.

    # ── exposures: one row per material, GLOBAL score ─────────────────
    expo_raw = db.execute(
        select(
            CompanyMaterialExposure.material_id,
            Material.canonical_name,
            CompanyMaterialExposure.supply_chain_stage,
            CompanyMaterialExposure.source_geography,
            CompanyMaterialExposure.exposure_score,
        )
        .join(Material, Material.id == CompanyMaterialExposure.material_id)
        .where(CompanyMaterialExposure.company_id == company.id)
        .order_by(CompanyMaterialExposure.id)
    ).all()

    _mat_ids = {r.material_id for r in expo_raw}
    _global_scores: dict[int, float] = {}
    if _mat_ids:
        _ranked_g = (
            select(
                MaterialGlobalRiskScore.material_id,
                MaterialGlobalRiskScore.overall_risk_score,
                MaterialGlobalRiskScore.material_concentration_score,
                func.row_number()
                .over(
                    partition_by=MaterialGlobalRiskScore.material_id,
                    order_by=MaterialGlobalRiskScore.as_of_date.desc(),
                )
                .label("rn"),
            )
            .where(MaterialGlobalRiskScore.material_id.in_(_mat_ids))
            .subquery()
        )
        for _mid, _g_overall, _g_conc in db.execute(
            select(
                _ranked_g.c.material_id,
                _ranked_g.c.overall_risk_score,
                _ranked_g.c.material_concentration_score,
            ).where(_ranked_g.c.rn == 1)
        ):
            # Insufficient-data gate, same as /risk-summary.
            if _g_overall is not None and _g_conc is not None and _g_conc > 0:
                _global_scores[_mid] = float(_g_overall)

    _by_mat: dict[int, dict] = {}
    # (material_id, geo) sourcing pairs feed the footprint section below.
    _sourcing_pairs: set[tuple[int, str]] = set()
    _mat_names: dict[int, str] = {}
    for mid, name, stg, geo, _exp in expo_raw:
        _mat_names[mid] = name
        if geo:
            _sourcing_pairs.add((mid, geo.upper()))
        d = _by_mat.setdefault(mid, {"material": name, "stages": []})
        _lbl = _stage_label(stg)
        if _lbl and _lbl not in d["stages"]:
            d["stages"].append(_lbl)

    exposures = []
    for mid, d in _by_mat.items():
        rs = _global_scores.get(mid)
        exposures.append(
            ExposureOut(
                material=d["material"],
                stage_label=" · ".join(d["stages"]) or None,
                risk_score=round(rs, 1) if rs is not None else None,
                band=_band_out(rs),
            )
        )
    exposures.sort(key=lambda e: (e.risk_score is None, -(e.risk_score or 0.0)))

    # ── geographic footprint: one row per country ─────────────────────
    # Country universe = facility countries ∪ CME source geographies.
    # Chips = L1 score at that country for every material the company
    # either processes there (facility material links) or sources there.
    fac_rows = db.execute(
        select(Facility.id, Facility.country, Facility.facility_type)
        .join(CompanyFacility, CompanyFacility.facility_id == Facility.id)
        .where(CompanyFacility.company_id == company.id)
    ).all()
    fac_total = len(fac_rows)

    _fac_by_country: dict[str, dict] = {}
    _fac_ids = []
    _fac_country: dict = {}
    for fid, country, ftype in fac_rows:
        c = (country or "").upper()
        if not c:
            continue
        _fac_ids.append(fid)
        _fac_country[fid] = c
        d = _fac_by_country.setdefault(c, {"count": 0, "activities": []})
        d["count"] += 1
        _ft = ftype.replace("_", " ")
        if _ft not in d["activities"]:
            d["activities"].append(_ft)

    # material ids per country via facility links
    _country_mids: dict[str, set[int]] = {c: set() for c in _fac_by_country}
    if _fac_ids:
        for _fid, _mid, _mname in db.execute(
            select(
                FacilityMaterialLink.facility_id,
                FacilityMaterialLink.material_id,
                Material.canonical_name,
            )
            .join(Material, Material.id == FacilityMaterialLink.material_id)
            .where(FacilityMaterialLink.facility_id.in_(_fac_ids))
        ):
            _mat_names[_mid] = _mname
            _country_mids.setdefault(_fac_country[_fid], set()).add(_mid)

    _sourcing_by_country: dict[str, set[int]] = {}
    for _mid, _geo in _sourcing_pairs:
        _sourcing_by_country.setdefault(_geo, set()).add(_mid)
        _country_mids.setdefault(_geo, set()).add(_mid)

    _all_footprint_mids = set().union(*_country_mids.values()) if _country_mids else set()
    _l1_by_pair: dict[tuple[int, str], float] = {}
    if _all_footprint_mids:
        _ranked_l1 = (
            select(
                MaterialGeographyRiskScore.material_id,
                MaterialGeographyRiskScore.geography_code,
                MaterialGeographyRiskScore.overall_risk_score,
                func.row_number()
                .over(
                    partition_by=(
                        MaterialGeographyRiskScore.material_id,
                        MaterialGeographyRiskScore.geography_code,
                    ),
                    order_by=MaterialGeographyRiskScore.as_of_date.desc(),
                )
                .label("rn"),
            )
            .where(MaterialGeographyRiskScore.material_id.in_(_all_footprint_mids))
            .subquery()
        )
        for _mid, _geo, _rs in db.execute(
            select(
                _ranked_l1.c.material_id,
                _ranked_l1.c.geography_code,
                _ranked_l1.c.overall_risk_score,
            ).where(_ranked_l1.c.rn == 1)
        ):
            if _rs is not None:
                _l1_by_pair[(_mid, _geo)] = float(_rs)

    geographies = []
    for _c, _mids_here in _country_mids.items():
        _scored = sorted(
            (
                (_mat_names[_m], _l1_by_pair[(_m, _c)])
                for _m in _mids_here
                if (_m, _c) in _l1_by_pair
            ),
            key=lambda t: (-t[1], t[0]),
        )
        _facinfo = _fac_by_country.get(_c, {"count": 0, "activities": []})
        geographies.append(
            GeoFootprintOut(
                country=_c,
                facility_count=_facinfo["count"],
                activities=sorted(_facinfo["activities"]),
                sourcing_materials=sorted(
                    _mat_names[_m] for _m in _sourcing_by_country.get(_c, set())
                ),
                location_risk=_band_out(_scored[0][1]) if _scored else None,
                materials=[
                    GeoFootprintMaterialOut(
                        material=_n,
                        score=round(_v, 1),
                        level=(_band_out(_v) or RiskBandOut(label="", level="low")).level,
                    )
                    for _n, _v in _scored
                ],
            )
        )
    geographies.sort(
        key=lambda g: (
            g.location_risk is None,
            -(g.location_risk.score if g.location_risk and g.location_risk.score else 0.0),
            g.country,
        )
    )

    # ── linked intelligence (tag contract) + events (Phase-5 flag) ────
    linked_posts = _tagged_posts(db, company.canonical_name)
    event_rows = db.execute(
        select(RiskEvent)
        .join(RiskEventCompany, RiskEventCompany.risk_event_id == RiskEvent.id)
        .where(
            RiskEventCompany.company_id == company.id,
            RiskEvent.duplicate_of_id.is_(None),  # 055
        )
        .order_by(RiskEvent.event_date.desc().nulls_last())
        .limit(6)
    ).scalars().all()
    linked_events = [
        LinkedEventOut(
            title=e.title,
            event_type=e.event_type,
            event_subtype=e.event_subtype,
            event_date=e.event_date,
            severity_score=e.severity_score,
        )
        for e in event_rows
    ]

    # ── facts strip ───────────────────────────────────────────────────
    facts: list[CompanyFactOut] = []
    stage_lbl = _stage_label(company.primary_activity_stage_fk)
    if stage_lbl:
        facts.append(CompanyFactOut(label="Stage", value=stage_lbl))
    if company.headquarters_country:
        facts.append(CompanyFactOut(label="HQ", value=company.headquarters_country))
    if company.public_ticker:
        exch = (company.exchanges or [None])[0]
        facts.append(CompanyFactOut(
            label="Listing",
            value=f"{exch}: {company.public_ticker}" if exch else company.public_ticker,
            mono=True,
        ))
    facts.append(CompanyFactOut(
        label="Coverage",
        value=f"{len(linked_posts)} posts · {len(linked_events)} events",
    ))

    return PublicCompanyProfile(
        slug=company.slug or str(company.id),
        name=company.canonical_name,
        legal_name=company.legal_name,
        band=band,
        facts=facts,
        intro=company.public_intro,  # 057: partner-authored public copy; None until written
        exposures=exposures,
        geographies=geographies,
        facilities_total=int(fac_total),
        linked_posts=linked_posts,
        linked_events=linked_events,
    )


@router.get(
    "/companies/{slug}/facilities",
    response_model=CompanyCountryFacilitiesOut,
    summary="Facilities for a company in one country (footprint drawer)",
)
def get_company_country_facilities(
    slug: str,
    country: str = Query(min_length=2, max_length=2),
    db: Session = Depends(get_db),
) -> CompanyCountryFacilitiesOut:
    """Detail for the geographic-footprint drawer: every facility this
    company operates in `country`, with the material(s) each handles and
    that material's L1 risk band AT this country. Public — gated on the
    company being published, same as the profile."""
    company = db.scalar(
        select(Company).where(Company.slug == slug, Company.is_published.is_(True))
    )
    if company is None:
        raise HTTPException(status_code=404, detail="Company not found")
    cc = country.upper()

    fac_rows = db.execute(
        select(Facility)
        .join(CompanyFacility, CompanyFacility.facility_id == Facility.id)
        .where(CompanyFacility.company_id == company.id, Facility.country == cc)
        .order_by(Facility.status, Facility.name)
    ).scalars().all()

    links_by_fac: dict = {}
    mids: set[int] = set()
    if fac_rows:
        for _fid, _mid, _mname in db.execute(
            select(
                FacilityMaterialLink.facility_id,
                FacilityMaterialLink.material_id,
                Material.canonical_name,
            )
            .join(Material, Material.id == FacilityMaterialLink.material_id)
            .where(FacilityMaterialLink.facility_id.in_([f.id for f in fac_rows]))
        ):
            links_by_fac.setdefault(_fid, []).append((_mid, _mname))
            mids.add(_mid)

    l1: dict[int, float] = {}
    if mids:
        ranked = (
            select(
                MaterialGeographyRiskScore.material_id,
                MaterialGeographyRiskScore.overall_risk_score,
                func.row_number()
                .over(
                    partition_by=MaterialGeographyRiskScore.material_id,
                    order_by=MaterialGeographyRiskScore.as_of_date.desc(),
                )
                .label("rn"),
            )
            .where(
                MaterialGeographyRiskScore.material_id.in_(mids),
                MaterialGeographyRiskScore.geography_code == cc,
            )
            .subquery()
        )
        for _mid, _rs in db.execute(
            select(ranked.c.material_id, ranked.c.overall_risk_score).where(
                ranked.c.rn == 1
            )
        ):
            if _rs is not None:
                l1[_mid] = float(_rs)

    facilities = []
    for f in fac_rows:
        place = (
            f"{f.city}, {f.region}" if f.city and f.region else (f.city or f.region)
        )
        mats = []
        for _mid, _mname in links_by_fac.get(f.id, []):
            band = _band_out(l1.get(_mid))
            if band is not None:
                mats.append(FacilityMaterialTag(material=_mname, level=band.level))
        mats.sort(key=lambda t: t.material)
        facilities.append(
            FacilityDetailOut(
                name=f.name,
                facility_type=f.facility_type.replace("_", " "),
                status=f.status.replace("_", " ").title(),
                status_level=_FACILITY_STATUS_LEVELS.get(f.status.lower(), "op"),
                place=place,
                latitude=f.latitude,
                longitude=f.longitude,
                data_source=f.data_source,
                materials=mats,
            )
        )
    country_name = db.scalar(select(Country.name).where(Country.iso2 == cc)) or cc
    return CompanyCountryFacilitiesOut(
        country=cc, country_name=country_name, facilities=facilities
    )


# ---------------------------------------------------------------------------
# Regulations — browse
# ---------------------------------------------------------------------------

@router.get(
    "/regulations",
    response_model=PaginatedResponse[PublicRegulationListItem],
    summary="Public regulation browse",
)
def list_public_regulations(
    q: Optional[str] = Query(None, description="Search key or title."),
    theme: Optional[str] = Query(None, description="policy_theme filter."),
    reg_status: Optional[str] = Query(
        None, alias="status",
        description="Status filter: proposed | enacted | effective | superseded.",
    ),
    page: int = Query(1, ge=1),
    limit: int = Query(25, ge=1, le=100),
    db: Session = Depends(get_db),
) -> PaginatedResponse[PublicRegulationListItem]:
    # 2026-07-15 (Nicole): the verified flag is THE public-visibility gate
    # for regulations — she reviews/enriches a regulation, then flips
    # verified to make it visible.  Mirrors companies.is_published.
    stmt = select(Regulation).where(Regulation.verified.is_(True))
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(
            or_(Regulation.regulation_key.ilike(like), Regulation.title.ilike(like))
        )
    if theme:
        stmt = stmt.where(Regulation.policy_theme.ilike(f"%{theme.strip()}%"))
    if reg_status:
        stmt = stmt.where(Regulation.status == reg_status.strip().lower())

    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    regs = db.scalars(
        stmt.order_by(Regulation.effective_date.desc().nulls_last())
        .offset((page - 1) * limit)
        .limit(limit)
    ).all()

    items = []
    for r in regs:
        disp, level = _reg_status(r.status)
        items.append(PublicRegulationListItem(
            regulation_key=r.regulation_key,
            title=r.title,
            issuer=r.issuing_body,
            geography=r.geography,
            theme=_theme_display(r.policy_theme),
            status=disp,
            status_level=level,
            effective_date=r.effective_date,
        ))
    return PaginatedResponse(data=items, total=int(total), page=page, limit=limit)


# ---------------------------------------------------------------------------
# Regulations — detail
# ---------------------------------------------------------------------------

@router.get(
    "/regulations/{regulation_key}",
    response_model=PublicRegulationDetail,
    summary="Public regulation detail",
)
def get_public_regulation(
    regulation_key: str, db: Session = Depends(get_db)
) -> PublicRegulationDetail:
    reg = db.scalar(
        select(Regulation).where(
            Regulation.regulation_key == regulation_key,
            Regulation.verified.is_(True),  # public gate (2026-07-15)
        )
    )
    if reg is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="regulation not found"
        )

    disp, level = _reg_status(reg.status)

    # ── timeline: fixed 3-stage strip (2026-07-15, Nicole) ─────────────
    # Proposed → Enacted → Effective, matching the status ladder.  Dates
    # attach where the two dated columns provide them (publication_date ≈
    # the proposed/published moment; effective_date ≈ in force); there is
    # no enacted_date column, so the Enacted node is undated and carried
    # by status alone.  `active` marks the CURRENT stage; `future` marks
    # stages not yet reached — the frontend renders reached / current /
    # future states on a fixed strip rather than a variable-length list.
    # Revision/amendment nodes were considered and cut (no tracking data).
    today = date.today()
    _status_norm = (reg.status or "").strip().lower()
    if _status_norm == "stayed":
        # 2026-07-26 (Nicole): court-stayed rules got past Proposed (they
        # were adopted) but never took effect — render Adopted as reached
        # and a terminal Stayed node instead of a dangling Effective date
        # that never arrived.
        timeline = [
            TimelineNodeOut(
                label="Proposed", date=reg.publication_date,
                active=False, future=False,
            ),
            TimelineNodeOut(
                label="Adopted", date=None, active=False, future=False,
            ),
            TimelineNodeOut(
                label="Stayed", date=None, active=True, future=False,
                note="Blocked by court order — never took effect.",
            ),
        ]
    else:
        _stage_rank = {"proposed": 0, "enacted": 1, "effective": 2}
        _current = _stage_rank.get(_status_norm, 0)
        _eff_future = reg.effective_date is not None and reg.effective_date > today
        _eff_reached = _current >= 2 and not _eff_future
        timeline = [
            TimelineNodeOut(
                label="Proposed",
                date=reg.publication_date,
                active=_current == 0,
                future=False,
            ),
            TimelineNodeOut(
                label="Enacted",
                date=None,
                active=_current == 1,
                future=_current < 1,
            ),
            TimelineNodeOut(
                label="Effective",
                # 2026-07-26: only show the date when the stage is reached
                # or genuinely scheduled ahead — a PAST effective_date on a
                # not-yet-effective status is stale data, not a milestone.
                date=reg.effective_date if (_eff_reached or _eff_future) else None,
                active=_eff_reached,
                future=_current < 2 or _eff_future,
            ),
        ]

    # ── scope chips + severity multipliers ────────────────────────────
    mat_rows = db.execute(
        select(Material.canonical_name, RegulationMaterialScope.scope_type)
        .join(Material, Material.id == RegulationMaterialScope.material_id)
        .where(RegulationMaterialScope.regulation_id == reg.id)
        .order_by(Material.canonical_name)
    ).all()
    materials_scope = [
        MaterialScopeOut(
            material=m,
            scope_type=st,
            severity_multiplier=SCOPE_SEVERITY_MULTIPLIER.get(st),
        )
        for m, st in mat_rows
    ]
    geo_rows = db.execute(
        select(
            RegulationGeographyScope.country_code,
            RegulationGeographyScope.scope_type,
        )
        .where(RegulationGeographyScope.regulation_id == reg.id)
        .order_by(RegulationGeographyScope.country_code)
    ).all()
    geographies_scope = [
        GeographyScopeOut(country_code=cc, scope_type=st) for cc, st in geo_rows
    ]

    # ── compliance-weight impact block (real numbers, not mock copy) ──
    weights: list[ComplianceWeightOut] = []
    if reg.geography_compliance_weights:
        weights = sorted(
            (
                ComplianceWeightOut(country_code=k, weight=float(v))
                for k, v in reg.geography_compliance_weights.items()
                if isinstance(v, (int, float))
            ),
            key=lambda w: -w.weight,
        )[:8]

    # ── source link ───────────────────────────────────────────────────
    source_url = None
    if reg.source_document_id:
        source_url = db.scalar(
            select(SourceDocument.url)
            .where(SourceDocument.id == reg.source_document_id)
        )

    # ── linked events + posts ─────────────────────────────────────────
    event_count = db.scalar(
        select(func.count(RiskEventRegulation.id))
        .where(RiskEventRegulation.regulation_id == reg.id)
    ) or 0
    event_rows = db.execute(
        select(RiskEvent)
        .join(RiskEventRegulation, RiskEventRegulation.risk_event_id == RiskEvent.id)
        .where(
            RiskEventRegulation.regulation_id == reg.id,
            RiskEvent.duplicate_of_id.is_(None),  # 055
        )
        .order_by(RiskEvent.event_date.desc().nulls_last())
        .limit(6)
    ).scalars().all()
    linked_events = [
        LinkedEventOut(
            title=e.title,
            event_type=e.event_type,
            event_subtype=e.event_subtype,
            event_date=e.event_date,
            severity_score=e.severity_score,
        )
        for e in event_rows
    ]
    linked_posts = _tagged_posts(db, reg.regulation_key)

    return PublicRegulationDetail(
        regulation_key=reg.regulation_key,
        title=reg.title,
        issuer=reg.issuing_body,
        geography=reg.geography,
        theme=_theme_display(reg.policy_theme),
        status=disp,
        status_level=level,
        summary=reg.summary,
        editorial=_editorial_out(reg.metadata_json),
        timeline=timeline,
        materials_scope=materials_scope,
        geographies_scope=geographies_scope,
        compliance_weights=weights,
        source_url=source_url,
        linked_posts=linked_posts,
        linked_events=linked_events,
        linked_event_count=int(event_count),
    )
