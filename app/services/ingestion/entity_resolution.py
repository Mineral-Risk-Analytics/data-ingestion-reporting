"""
Entity resolution: determine which companies are relevant to a parsed RiskEvent.

Runs after event creation, before scoring. Output is a list of
(company_id, relevance_score, match_reason) tuples that get persisted to
risk_event_companies via persist_company_links().

Resolution rules (applied in priority order per company):
  1. Named company match  — relevance 1.00
  2. Geography match      — relevance 0.85
  3. Material / HS match  — relevance 0.70
  4. Category-broad match — relevance 0.70 (fallback for geo/regulatory events)

These relevance scores map directly to the relevance_multiplier input of
compute_event_impact() after clamping to [0.70, 1.30].
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Optional

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.constants import RiskCategory
from app.models.company import Company, CompanyAlias, CompanyMaterialExposure
from app.models.regulatory import RiskEvent, RiskEventCompany
from app.models.supply import Material

log = structlog.get_logger(__name__)

HIGH_CONCENTRATION_GEOS = frozenset({"CN", "CD", "RU"})

_RELEVANCE_NAMED     = 1.00
_RELEVANCE_GEOGRAPHY = 0.85
_RELEVANCE_MATERIAL  = 0.70
_RELEVANCE_BROAD     = 0.70

_BROAD_MATCH_CATEGORIES = frozenset({
    RiskCategory.GEOPOLITICAL_TRADE.value,
    RiskCategory.REGULATORY_COMPLIANCE.value,
})

_BATTERY_KEYWORDS = frozenset({
    "lithium", "battery", "cobalt", "nickel", "manganese", "graphite",
    "rare earth", "critical mineral", "ev", "electric vehicle",
    "cathode", "anode", "electrolyte", "separator", "cell",
})


@dataclass
class CachedCompanyInfo:
    """Pre-loaded company data for fast in-process resolution (avoids N+1)."""

    id: uuid.UUID
    canonical_name_lower: str
    headquarters_country: Optional[str]    # ISO2
    supply_chain_stage: Optional[str]
    alias_lowers: list[str]                # all aliases, lowercased
    material_ids: frozenset[int]           # material IDs from CompanyMaterialExposure
    material_keywords: frozenset[str]      # material canonical names, lowercased tokens


def build_company_cache(db: Session) -> list[CachedCompanyInfo]:
    """
    Query all companies with their aliases and material exposures in two queries
    (no N+1). Call once per ingestion run and pass the result through.
    """
    companies = db.scalars(
        select(Company).options(
            selectinload(Company.aliases),
            selectinload(Company.exposures).selectinload(CompanyMaterialExposure.material),
        )
    ).all()

    cache: list[CachedCompanyInfo] = []
    for c in companies:
        material_ids: set[int] = set()
        material_keywords: set[str] = set()
        for exp in c.exposures:
            material_ids.add(exp.material_id)
            if exp.material and exp.material.canonical_name:
                for tok in exp.material.canonical_name.lower().split():
                    if len(tok) > 3:
                        material_keywords.add(tok)

        cache.append(
            CachedCompanyInfo(
                id=c.id,
                canonical_name_lower=c.canonical_name.lower(),
                headquarters_country=c.headquarters_country,
                supply_chain_stage=c.supply_chain_stage,
                alias_lowers=[a.alias.lower() for a in c.aliases],
                material_ids=frozenset(material_ids),
                material_keywords=frozenset(material_keywords),
            )
        )
    return cache


def resolve_companies_for_event(
    db: Session,
    event: RiskEvent,
    all_companies: list[CachedCompanyInfo],
) -> list[tuple[uuid.UUID, float, str]]:
    """
    Return (company_id, relevance_score, match_reason) for every company
    considered relevant to this event.

    Rules applied in priority order per company. Highest match wins; a company
    is included at most once. Never returns a row with relevance 0.
    """
    event_text = " ".join(filter(None, [event.title, event.summary])).lower()
    event_categories: list[str] = event.risk_categories_json or []
    geo_json: dict = event.geography_json or {}
    meta_json: dict = event.metadata_json or {}

    geo_values: set[str] = set()
    for v in geo_json.values():
        if isinstance(v, str):
            geo_values.update(v.lower().split())
        elif isinstance(v, list):
            for item in v:
                if isinstance(item, str):
                    geo_values.update(item.lower().split())

    event_is_battery_relevant = bool(_BATTERY_KEYWORDS & set(event_text.split()))
    has_broad_category = bool(_BROAD_MATCH_CATEGORIES & set(event_categories))

    results: list[tuple[uuid.UUID, float, str]] = []

    for company in all_companies:
        if _named_match(event_text, company):
            results.append((company.id, _RELEVANCE_NAMED, "named_company"))
            continue

        if _geography_match(geo_values, company):
            results.append((company.id, _RELEVANCE_GEOGRAPHY, "geography"))
            continue

        if company.material_keywords and _material_match(event_text, meta_json, company):
            results.append((company.id, _RELEVANCE_MATERIAL, "material_hs"))
            continue

        if has_broad_category and event_is_battery_relevant:
            results.append((company.id, _RELEVANCE_BROAD, "category_broad"))

    log.debug(
        "entity_resolution.resolve_companies_for_event",
        event_id=event.id,
        event_title=event.title[:80],
        matches=len(results),
    )
    return results


def persist_company_links(
    db: Session,
    event: RiskEvent,
    matches: list[tuple[uuid.UUID, float, str]],
) -> None:
    """
    Upsert RiskEventCompany rows.

    SELECT-then-INSERT/UPDATE for idempotency: re-running entity resolution on
    the same event upgrades relevance_score and match_reason rather than creating
    duplicates. The UniqueConstraint on (risk_event_id, company_id) provides the
    secondary safety net.

    TODO (Postgres-only): replace the loop with a single pg INSERT ... ON CONFLICT
    DO UPDATE once SQLite test compatibility is no longer required.
    """
    if not matches:
        return

    for company_id, relevance, reason in matches:
        existing = db.execute(
            select(RiskEventCompany).where(
                RiskEventCompany.risk_event_id == event.id,
                RiskEventCompany.company_id == company_id,
            )
        ).scalar_one_or_none()

        if existing is not None:
            existing.relevance_score = relevance
            existing.match_reason = reason
        else:
            db.add(
                RiskEventCompany(
                    id=uuid.uuid4(),
                    risk_event_id=event.id,
                    company_id=company_id,
                    relevance_score=relevance,
                    match_reason=reason,
                )
            )


# ---------------------------------------------------------------------------
# Private match helpers
# ---------------------------------------------------------------------------

def _named_match(event_text: str, company: CachedCompanyInfo) -> bool:
    if company.canonical_name_lower in event_text:
        return True
    return any(alias in event_text for alias in company.alias_lowers if len(alias) > 4)


def _geography_match(geo_values: set[str], company: CachedCompanyInfo) -> bool:
    if not geo_values:
        return False
    if company.headquarters_country and company.headquarters_country.lower() in geo_values:
        return True
    hcg_lower = {g.lower() for g in HIGH_CONCENTRATION_GEOS}
    return bool(hcg_lower & geo_values)


def _material_match(event_text: str, meta_json: dict, company: CachedCompanyInfo) -> bool:
    if company.material_keywords & set(event_text.split()):
        return True
    hs_codes: list[str] = []
    if isinstance(meta_json.get("hs_code"), str):
        hs_codes.append(meta_json["hs_code"])
    if isinstance(meta_json.get("hs_codes"), list):
        hs_codes.extend(str(c) for c in meta_json["hs_codes"])
    battery_hs_prefixes = ("8507", "2805", "2842", "2846", "8104", "7504", "2601")
    for hs in hs_codes:
        if any(hs.startswith(p) for p in battery_hs_prefixes):
            return True
    return False
