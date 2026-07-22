"""Response schemas for the public Intelligence Hub entity pages.

Companies (browse + profile) and Regulations (browse + detail) — the
Mineral Risk Analytics public site, mocked in the design-system project
(ui_kits/intelligence_hub/{companies,company,regulations,regulation}.html).

Contract notes (decided 2026-07-14):

* Companies are visible ONLY when ``companies.is_published`` is TRUE
  (migration 054; set by the workbook loader from the partner publish
  flag).  Internal notes are NEVER exposed — the profile ``intro`` is a
  dedicated public field (currently None until an admin authoring flow
  exists; the frontend falls back to a generated one-liner).
* Exposure rows join ``company_material_exposures`` (the WHAT: material ×
  stage × source geography) with the latest
  ``material_geography_risk_scores`` row (the HOW RISKY: L1 score for
  that material × geography).  ``score`` is 0-100; ``band`` uses the
  shared bands.py cutoffs (sole source of truth with the frontend's
  risk-band.ts).
* Stage labels translate DB stage codes to display vocabulary once,
  server-side (``mining`` → "Mining", ``cell_making`` → "Cell", …) so the
  frontend never learns the internal enum.
* Linked intelligence is TAG-DRIVEN: a published post is linked to a
  company when its ``tags`` array contains the company's canonical name,
  and to a regulation when it contains the ``regulation_key``.  This is
  an editorial contract — the partner tags posts; no NLP inference.
* Linked events for companies come from ``risk_event_companies``, which
  is empty until the Phase-5 ``LINK_EVENTS_TO_COMPANIES`` flag flips —
  the field ships now so the frontend contract is stable.
"""

from __future__ import annotations

from datetime import date, datetime
from datetime import date as Date  # alias: TimelineNodeOut's field named `date` shadows the type
from typing import Optional

from pydantic import BaseModel, ConfigDict


# ---------------------------------------------------------------------------
# Shared
# ---------------------------------------------------------------------------

class RiskBandOut(BaseModel):
    """Display band: label ('High') + level token for CSS ('high')."""
    label: str
    level: str  # low | med | high | crit
    score: Optional[float] = None  # 0-100, one decimal


class LinkedPostOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    slug: str
    title: str
    content_type: str
    pillar: Optional[str] = None
    materials: Optional[list[str]] = None
    geographies: Optional[list[str]] = None
    summary: Optional[str] = None
    published_at: Optional[datetime] = None
    read_time_minutes: Optional[int] = None


class LinkedEventOut(BaseModel):
    title: str
    event_type: str
    event_subtype: Optional[str] = None
    event_date: Optional[datetime] = None
    severity_score: Optional[float] = None


# ---------------------------------------------------------------------------
# Companies
# ---------------------------------------------------------------------------

class PublicCompanyListItem(BaseModel):
    slug: str
    name: str
    legal_name: Optional[str] = None
    stage_label: Optional[str] = None       # "Refining · Cell"
    hq_country: Optional[str] = None        # ISO2
    materials: list[str] = []               # top exposures, max 3
    band: Optional[RiskBandOut] = None


class CompanyFactOut(BaseModel):
    label: str
    value: str
    mono: bool = False


class ExposureOut(BaseModel):
    """One material the company depends on — "market + map" v4
    (2026-07-21): deduped to ONE row per material, stages combined into
    ``stage_label`` ("Refining · Cell"). ``risk_score`` is the material's
    GLOBAL rollup (sidebar-consistent, insufficient-data gated) — the
    where-question lives entirely in ``GeoFootprintOut`` now, which is
    why the v1-v3 ``geography``/``score_basis`` fields are gone."""

    material: str
    stage_label: Optional[str] = None       # combined, e.g. "Refining · Cell"
    risk_score: Optional[float] = None      # 0-100 global rollup, gated
    band: Optional[RiskBandOut] = None


class GeoFootprintMaterialOut(BaseModel):
    """One material's L1 score at this country (chip)."""

    material: str
    score: float                            # 0-100, 1dp
    level: str                              # low | med | high | crit (CSS token)


class GeoFootprintOut(BaseModel):
    """One country the company operates or sources in — "market + map" v4
    (2026-07-21), replacing the per-facility list. Groups every facility
    in the country plus CME sourcing; chips carry the L1 material×
    geography scores AT this place (the only section with per-geo
    scores — exposure rows are global-only)."""

    country: str                            # ISO2
    facility_count: int                     # 0 = sourcing-only country
    activities: list[str] = []              # distinct facility types, sorted
    sourcing_materials: list[str] = []      # CME source_geography materials
    location_risk: Optional[RiskBandOut] = None      # max across chips
    materials: list[GeoFootprintMaterialOut] = []    # chips, score desc



class FacilityMaterialTag(BaseModel):
    """A material a facility handles + its risk band AT the facility's
    country. Level only (no number), matching the footprint chips."""

    material: str
    level: str  # low | med | high | crit


class FacilityDetailOut(BaseModel):
    """One facility, for the geographic-footprint drawer (2026-07-22)."""

    name: Optional[str] = None
    facility_type: str
    status: str
    status_level: str           # op | ramp | build | idle | closed
    place: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    data_source: Optional[str] = None
    materials: list[FacilityMaterialTag] = []


class CompanyCountryFacilitiesOut(BaseModel):
    country: str                # ISO2
    country_name: str           # full display name (falls back to ISO2)
    facilities: list[FacilityDetailOut]

class PublicCompanyProfile(BaseModel):
    slug: str
    name: str
    legal_name: Optional[str] = None
    band: Optional[RiskBandOut] = None
    facts: list[CompanyFactOut] = []
    intro: Optional[str] = None             # public copy — None until admin flow
    exposures: list[ExposureOut] = []
    geographies: list[GeoFootprintOut] = []
    facilities_total: int = 0               # total facilities across all countries
    linked_posts: list[LinkedPostOut] = []
    linked_events: list[LinkedEventOut] = []


# ---------------------------------------------------------------------------
# Regulations
# ---------------------------------------------------------------------------

class PublicRegulationListItem(BaseModel):
    regulation_key: str
    title: Optional[str] = None
    issuer: Optional[str] = None
    geography: Optional[str] = None
    theme: Optional[str] = None
    status: Optional[str] = None            # display: "In force" / "Proposed" / …
    status_level: Optional[str] = None      # inforce | proposed | pending | ended
    effective_date: Optional[date] = None


class TimelineNodeOut(BaseModel):
    label: str                              # "Proposed" / "Enacted" / "Effective"
    # NOTE: field name `date` shadows the imported `date` type inside the
    # class namespace — with postponed annotations, Pydantic resolved
    # Optional[date] to Optional[None], rejecting every real date (the
    # ValidationError-as-CORS bug, 2026-07-16).  The `Date` alias points
    # at the module-level type unambiguously.
    date: Optional[Date] = None
    note: Optional[str] = None
    active: bool = False
    future: bool = False


class MaterialScopeOut(BaseModel):
    material: str
    scope_type: str                         # covered | restricted | banned | …
    severity_multiplier: Optional[float] = None  # from SCOPE_SEVERITY_MULTIPLIER


class GeographyScopeOut(BaseModel):
    country_code: str
    scope_type: str                         # jurisdiction | origin_country | targeted_country


class ComplianceWeightOut(BaseModel):
    country_code: str                       # ISO2 or 'DEFAULT'
    weight: float                           # 0-1; 1 = highest compliance risk


class PublicRegulationDetail(BaseModel):
    regulation_key: str
    title: Optional[str] = None
    issuer: Optional[str] = None
    geography: Optional[str] = None
    theme: Optional[str] = None
    status: Optional[str] = None
    status_level: Optional[str] = None
    summary: Optional[str] = None
    timeline: list[TimelineNodeOut] = []
    materials_scope: list[MaterialScopeOut] = []
    geographies_scope: list[GeographyScopeOut] = []
    compliance_weights: list[ComplianceWeightOut] = []
    source_url: Optional[str] = None
    linked_posts: list[LinkedPostOut] = []
    linked_events: list[LinkedEventOut] = []
    linked_event_count: int = 0
