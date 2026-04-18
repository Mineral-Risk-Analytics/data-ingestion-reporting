"""GLEIF LEI enrichment for the companies table.

Queries the Global Legal Entity Identifier Foundation public API (no key
required) to backfill ``lei``, ``legal_name``, and additional aliases for
each company where ``lei IS NULL``.

The LEI is the primary identifier used by OpenSanctions for entity matching,
so enriching companies before running ingest-opensanctions dramatically
improves match precision.

Run order:
    bdi-ingest seed-companies   # populate companies table first
    bdi-ingest ingest-gleif     # this module
    bdi-ingest ingest-opensanctions  # re-run for better entity matching
"""

from __future__ import annotations

import re
import time
from difflib import SequenceMatcher
from typing import Any, Optional

import httpx
import structlog
from sqlalchemy import not_, select
from sqlalchemy.orm import Session

from app.models.company import Company, CompanyAlias

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GLEIF_API_BASE = "https://api.gleif.org/api/v1"

# Minimum similarity score (0–1) to accept a GLEIF candidate as a match.
# 0.82 balances false positives (e.g. "Ford Motor Company" → "Ford Motor Credit
# Company") against missed matches with different legal suffix conventions.
GLEIF_MATCH_THRESHOLD = 0.82

# Companies intentionally excluded from GLEIF search to avoid wasted API calls.
#   - Norilsk Nickel: no active LEI post-2022 sanctions
#   - BTR New Energy: not consistently registered under a searchable legal name
#   - DRC mine SARLs (Tenke, Kisanfu, Mutanda, Kamoto, Congo DPR Huayou):
#     local Congolese entities, not registered with GLEIF
#   - Balama Graphite: Mozambican entity, not GLEIF registered
#   - Redwood Materials / Cirba Solutions: private US companies without LEIs
_GLEIF_SKIP_COMPANIES: frozenset[str] = frozenset({
    "Norilsk Nickel",
    "BTR New Energy",
    "Tenke Fungurume Mining",
    "Kisanfu Mining",
    "Mutanda Mining",
    "Kamoto Copper Company",
    "Congo DPR Huayou Cobalt",
    "Balama Graphite",
    "Redwood Materials",
    "Cirba Solutions",
})

# Legal suffixes stripped during name normalisation for similarity comparison.
_LEGAL_SUFFIXES = re.compile(
    r"\b(inc\.?|ltd\.?|co\.?|ag|plc|n\.?v\.?|s\.?a\.?|gmbh|llc|corp\.?|"
    r"limited|group|holding|holdings|international|corporation|company)\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Name similarity
# ---------------------------------------------------------------------------

def _normalize_name(name: str) -> str:
    """Lowercase, strip legal suffixes, strip punctuation, collapse whitespace."""
    name = name.lower()
    name = _LEGAL_SUFFIXES.sub("", name)
    # Strip punctuation (commas, periods, etc.) that survive suffix removal
    name = re.sub(r"[,.\-/\\]", " ", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name


def _name_similarity(a: str, b: str) -> float:
    """Return a similarity score 0–1 between two company name strings.

    Normalises both strings: lowercase, strip legal suffixes
    (Inc., Ltd., Co., AG, plc, N.V., S.A., GmbH, LLC, Corp., Limited),
    collapse whitespace.

    Uses SequenceMatcher from difflib (stdlib). No external deps.
    Returns 1.0 for identical normalised strings.
    """
    norm_a = _normalize_name(a)
    norm_b = _normalize_name(b)
    if norm_a == norm_b:
        return 1.0
    return SequenceMatcher(None, norm_a, norm_b).ratio()


# ---------------------------------------------------------------------------
# GLEIF API client
# ---------------------------------------------------------------------------

def _search_gleif(name: str, timeout: int = 15) -> list[dict]:
    """Search GLEIF lei-records by entity name. Returns raw ``data`` list."""
    params = {
        "filter[entity.names]": name,
        "filter[entity.status]": "ACTIVE",
        "page[size]": 5,
    }
    try:
        response = httpx.get(
            f"{GLEIF_API_BASE}/lei-records",
            params=params,
            timeout=timeout,
        )
        response.raise_for_status()
        return response.json().get("data", [])
    except httpx.HTTPStatusError as exc:
        log.warning("gleif.http_error", name=name, status=exc.response.status_code)
        return []
    except (httpx.TimeoutException, httpx.RequestError) as exc:
        log.warning("gleif.request_error", name=name, error=str(exc))
        return []


def _best_candidate(
    canonical_name: str,
    candidates: list[dict],
) -> tuple[Optional[dict], float]:
    """Return the best-matching GLEIF candidate and its similarity score.

    Scores each candidate against both its legalName and any otherNames.
    Returns (None, 0.0) if no candidates pass GLEIF_MATCH_THRESHOLD.
    """
    best_record: Optional[dict] = None
    best_score = 0.0

    for record in candidates:
        attrs = record.get("attributes", {})
        entity = attrs.get("entity", {})

        # Score against legal name
        legal_name_obj = entity.get("legalName") or {}
        legal_name = legal_name_obj.get("name") or ""
        score = _name_similarity(canonical_name, legal_name)

        # Score against each otherName and take the maximum
        for other in entity.get("otherNames") or []:
            other_score = _name_similarity(canonical_name, other.get("name") or "")
            score = max(score, other_score)

        if score > best_score:
            best_score = score
            best_record = record

    if best_score < GLEIF_MATCH_THRESHOLD:
        return None, best_score

    return best_record, best_score


# ---------------------------------------------------------------------------
# Enrichment functions
# ---------------------------------------------------------------------------

def enrich_company_from_gleif(
    session: Session,
    company: Company,
    timeout: int = 15,
    rate_limit_delay: float = 0.5,
    gleif_search_name: Optional[str] = None,
) -> dict[str, Any]:
    """Query GLEIF by company name, match, and update Company fields.

    Only updates fields that are currently NULL in the DB — does not overwrite
    manually set values.

    Args:
        gleif_search_name: If provided, use this string in the GLEIF name-search
            filter instead of company.canonical_name.  Useful when the canonical
            name is a ticker/abbreviation that GLEIF won't recognise (e.g. "CATL"
            → search "Contemporary Amperex Technology" instead).

    Updates:
        company.lei              (if NULL and match found)
        company.legal_name       (if NULL and match found)
        company.headquarters_country (if NULL and match found)

    Inserts CompanyAlias rows for each GLEIF otherName not already present
    (alias_type = "aka").

    Returns {
        "company": canonical_name,
        "matched": bool,
        "lei": str | None,
        "similarity": float | None,
        "aliases_added": int,
        "search_name_used": str,
    }
    """
    search_name = gleif_search_name or company.canonical_name

    result: dict[str, Any] = {
        "company": company.canonical_name,
        "matched": False,
        "lei": None,
        "similarity": None,
        "aliases_added": 0,
        "search_name_used": search_name,
    }

    candidates = _search_gleif(search_name, timeout=timeout)

    if not candidates:
        log.info(
            "gleif.no_results",
            canonical_name=company.canonical_name,
            search_name=search_name,
        )
        time.sleep(rate_limit_delay)
        return result

    # Score candidates against the canonical_name (not the search override) so
    # the similarity threshold reflects how well the result maps to our record.
    best_record, similarity = _best_candidate(company.canonical_name, candidates)

    if best_record is None:
        log.info(
            "gleif.below_threshold",
            canonical_name=company.canonical_name,
            best_similarity=round(similarity, 3),
            threshold=GLEIF_MATCH_THRESHOLD,
        )
        time.sleep(rate_limit_delay)
        return result

    attrs = best_record.get("attributes", {})
    entity = attrs.get("entity", {})
    lei = attrs.get("lei")

    # Update NULL fields only
    if company.lei is None and lei:
        company.lei = lei
        result["lei"] = lei

    legal_name_obj = entity.get("legalName") or {}
    gleif_legal_name = legal_name_obj.get("name") or None
    if company.legal_name is None and gleif_legal_name:
        company.legal_name = gleif_legal_name

    hq_address = entity.get("headquartersAddress") or {}
    gleif_country = hq_address.get("country") or None
    if company.headquarters_country is None and gleif_country:
        company.headquarters_country = gleif_country

    # Add otherNames as aliases
    existing_aliases = {a.alias.lower() for a in company.aliases}
    aliases_added = 0
    for other in entity.get("otherNames") or []:
        other_name = other.get("name") or ""
        if other_name and other_name.lower() not in existing_aliases:
            session.add(
                CompanyAlias(
                    company_id=company.id,
                    alias=other_name,
                    alias_type="aka",
                )
            )
            existing_aliases.add(other_name.lower())
            aliases_added += 1

    result.update(
        matched=True,
        similarity=round(similarity, 3),
        aliases_added=aliases_added,
    )

    log.info(
        "gleif.matched",
        canonical_name=company.canonical_name,
        lei=lei,
        similarity=round(similarity, 3),
        aliases_added=aliases_added,
    )

    time.sleep(rate_limit_delay)
    return result


def enrich_all_companies(
    session: Session,
    rate_limit_delay: float = 0.5,
    search_name_overrides: Optional[dict[str, str]] = None,
) -> list[dict]:
    """Run GLEIF enrichment for all Company rows where lei IS NULL.

    Skips companies in ``_GLEIF_SKIP_COMPANIES`` (Norilsk Nickel, BTR New
    Energy) to avoid wasted API calls for entities known to have no active LEI.

    Args:
        search_name_overrides: Optional mapping of ``{canonical_name: search_name}``
            passed through to ``enrich_company_from_gleif``.  Typically built
            from the ``gleif_search_name`` fields in the ``_COMPANIES`` seed.

    Commits after each successful update so partial runs are not lost on error.

    Returns a list of per-company result dicts from enrich_company_from_gleif().
    """
    overrides = search_name_overrides or {}

    companies = session.scalars(
        select(Company).where(
            Company.lei.is_(None),
            not_(Company.canonical_name.in_(_GLEIF_SKIP_COMPANIES)),
        )
    ).all()

    log.info(
        "gleif.enrich_all.start",
        count=len(companies),
        skipped_known_non_gleif=len(_GLEIF_SKIP_COMPANIES),
    )

    results: list[dict] = []
    for company in companies:
        result = enrich_company_from_gleif(
            session,
            company,
            rate_limit_delay=rate_limit_delay,
            gleif_search_name=overrides.get(company.canonical_name),
        )
        results.append(result)

        if result["matched"]:
            try:
                session.commit()
            except Exception as exc:
                session.rollback()
                log.error(
                    "gleif.commit_error",
                    canonical_name=company.canonical_name,
                    error=str(exc),
                )

    log.info(
        "gleif.enrich_all.done",
        total=len(results),
        matched=sum(1 for r in results if r["matched"]),
    )
    return results
