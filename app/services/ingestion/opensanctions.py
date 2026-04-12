"""OpenSanctions consolidated sanctions list ingestion.

Downloads the free daily bulk CSV export from OpenSanctions
(https://www.opensanctions.org) and produces two types of ``RiskEvent`` rows:

1. **Company match events** (``event_type="sanctions_listing"``) — one event per
   company in the ``companies`` table whose canonical name or alias matches a
   sanctioned entity by normalised string comparison.

2. **Geography signal events** (``event_type="geography_sanctions_exposure"``) —
   one event per high-concentration geography (default: CN, CD, RU, IR, KP)
   recording how many sanctioned company/organisation records are linked to that
   country in the OpenSanctions dataset.

Important caveats
-----------------
- **Name matching is fuzzy by normalisation only** (lowercase + whitespace
  collapse). There will be false positives — two unrelated companies can share
  a common name fragment. A human review step is strongly recommended before
  acting on any sanctions match.

- **The ``companies`` table may be empty on first run.** In that case, company
  matching produces zero events. This is expected — seed companies from SEC
  EDGAR, the USGS ingest, or manual data entry first.

- **Geography events are volume signals, not quality signals.** A country with
  1,000 sanctioned shell companies is not necessarily riskier than one with 10
  major state-owned enterprises. The severity score (``count / 500``) reflects
  raw volume. Treat it as one input among many.

No raw sanctions entity records are stored in the database — only structured
``RiskEvent`` rows for confirmed matches.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Optional

import httpx
import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.company import Company, CompanyAlias
from app.models.regulatory import RiskEvent, RiskEventCompany, RiskEventGeography

log = structlog.get_logger(__name__)

OPENSANCTIONS_CSV_URL = (
    "https://data.opensanctions.org/datasets/latest/sanctions/targets.simple.csv"
)

_SOURCE = "opensanctions"
_BATCH_SIZE = 100

# Only these schema types are relevant for company matching.
_COMPANY_SCHEMAS = {"Company", "Organization", "LegalEntity", "PublicBody"}

_DEFAULT_HIGH_CONCENTRATION_GEOS = ["CN", "CD", "RU", "IR", "KP"]


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

def download_sanctions_csv(
    url: str = OPENSANCTIONS_CSV_URL,
    timeout: int = 120,
) -> io.BytesIO:
    """Stream-download the sanctions CSV and return an in-memory BytesIO buffer.

    The file is ~15–30 MB. Streamed to avoid a single large allocation.

    Raises:
        httpx.HTTPStatusError: on non-2xx HTTP response.
        httpx.TimeoutException: if the download exceeds ``timeout`` seconds.
    """
    log.info("opensanctions.download.start", url=url)
    chunks: list[bytes] = []

    with httpx.stream("GET", url, timeout=timeout, follow_redirects=True) as resp:
        resp.raise_for_status()
        for chunk in resp.iter_bytes():
            chunks.append(chunk)

    buf = io.BytesIO(b"".join(chunks))
    buf.seek(0)
    log.info("opensanctions.download.done", bytes=buf.getbuffer().nbytes)
    return buf


# ---------------------------------------------------------------------------
# Parse
# ---------------------------------------------------------------------------

def _split_pipe(value: str) -> list[str]:
    """Split a pipe-separated cell value into a non-empty list of stripped strings."""
    if not value:
        return []
    return [part.strip() for part in value.split("|") if part.strip()]


def _parse_date(value: str) -> Optional[datetime]:
    """Parse an ISO date string to a datetime, returning None on failure."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.split("T")[0])
    except ValueError:
        return None


def parse_sanctions_csv(buf: io.BytesIO) -> list[dict]:
    """Parse the CSV buffer into a list of sanctioned entity dicts.

    Only keeps rows where ``schema`` is one of:
        Company, Organization, LegalEntity, PublicBody

    Parses in a single streaming pass — does not load all rows into memory.

    Returns dicts with keys:
        opensanctions_id  (str)
        name              (str — primary name)
        aliases           (list[str] — pipe-split, stripped, lowercased, deduped)
        countries         (list[str] — uppercase ISO2 codes)
        datasets          (list[str])
        first_seen        (datetime | None)
        last_seen         (datetime | None)
    """
    # Read bytes then wrap a fresh BytesIO so the TextIOWrapper does not close
    # the caller's buffer when it goes out of scope (Python 3.12+ GC behaviour).
    buf.seek(0)
    reader = csv.DictReader(io.TextIOWrapper(io.BytesIO(buf.read()), encoding="utf-8"))

    results: list[dict] = []
    for row in reader:
        schema = (row.get("schema") or "").strip()
        if schema not in _COMPANY_SCHEMAS:
            continue

        # Aliases: split, lowercase, dedupe, exclude primary name duplicate
        primary_name = (row.get("name") or "").strip()
        raw_aliases = _split_pipe(row.get("aliases") or "")
        aliases_norm = list(
            dict.fromkeys(
                a.lower() for a in raw_aliases if a.lower() != primary_name.lower()
            )
        )

        countries = [c.strip().upper() for c in _split_pipe(row.get("countries") or "") if c.strip()]
        datasets = _split_pipe(row.get("datasets") or "")

        results.append(
            {
                "opensanctions_id": (row.get("id") or "").strip(),
                "name": primary_name,
                "aliases": aliases_norm,
                "countries": countries,
                "datasets": datasets,
                "first_seen": _parse_date(row.get("first_seen") or ""),
                "last_seen": _parse_date(row.get("last_seen") or ""),
            }
        )

    log.info("opensanctions.parse.done", entity_count=len(results))
    return results


# ---------------------------------------------------------------------------
# Company matching
# ---------------------------------------------------------------------------

_WHITESPACE_RE = re.compile(r"\s+")


def _normalise(name: str) -> str:
    """Lowercase, strip, collapse internal whitespace."""
    return _WHITESPACE_RE.sub(" ", name.lower().strip())


def _build_name_index(entities: list[dict]) -> dict[str, list[dict]]:
    """Build a lookup dict: normalised_name → list of matching sanctioned entities.

    Indexes both ``name`` and each entry in ``aliases``.
    """
    index: dict[str, list[dict]] = {}
    for entity in entities:
        keys = [_normalise(entity["name"])] + [_normalise(a) for a in entity["aliases"]]
        for key in keys:
            if key:
                index.setdefault(key, []).append(entity)
    return index


def match_companies(
    session: Session,
    entities: list[dict],
) -> list[tuple[Any, list[dict]]]:
    """Return ``(Company ORM row, list of matching sanctioned entities)`` pairs.

    Queries all ``companies`` and ``company_aliases`` in two bulk queries (no N+1
    lookups). Normalises names the same way as ``_build_name_index``. Returns only
    companies with at least one match.
    """
    name_index = _build_name_index(entities)
    if not name_index:
        return []

    all_companies: list[Company] = list(session.scalars(select(Company)).all())
    all_aliases: list[CompanyAlias] = list(session.scalars(select(CompanyAlias)).all())

    # Map company_id → Company for alias → company resolution
    company_by_id: dict[Any, Company] = {c.id: c for c in all_companies}

    matched: dict[Any, list[dict]] = {}  # company_id → matched entities

    # Check canonical names
    for company in all_companies:
        key = _normalise(company.canonical_name)
        if key in name_index:
            matched.setdefault(company.id, []).extend(name_index[key])

    # Check aliases
    for alias_row in all_aliases:
        key = _normalise(alias_row.alias)
        if key in name_index:
            company = company_by_id.get(alias_row.company_id)
            if company:
                matched.setdefault(company.id, []).extend(name_index[key])

    # Deduplicate matched entities per company by opensanctions_id
    result: list[tuple[Any, list[dict]]] = []
    for company_id, entity_list in matched.items():
        seen_ids: set[str] = set()
        unique: list[dict] = []
        for e in entity_list:
            if e["opensanctions_id"] not in seen_ids:
                seen_ids.add(e["opensanctions_id"])
                unique.append(e)
        company = company_by_id[company_id]
        result.append((company, unique))

    log.info("opensanctions.match.done", companies_matched=len(result))
    return result


# ---------------------------------------------------------------------------
# Content hash
# ---------------------------------------------------------------------------

def _content_hash(title: str, summary: str, event_date: Optional[datetime]) -> str:
    """SHA-256 of ``'{title}|{summary}|{date_isoformat}'``."""
    date_str = event_date.date().isoformat() if event_date else ""
    parts = f"{title}|{summary}|{date_str}"
    return hashlib.sha256(parts.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Main ingest
# ---------------------------------------------------------------------------

def ingest_opensanctions(
    session: Session,
    url: str = OPENSANCTIONS_CSV_URL,
    high_concentration_geos: Optional[list[str]] = None,
) -> dict[str, int]:
    """Download, parse, match, and insert OpenSanctions data as RiskEvents.

    Args:
        session:                  SQLAlchemy session. Commits internally.
        url:                      Override download URL (useful for tests).
        high_concentration_geos:  ISO2 codes to treat as high-risk geographies.
                                  Defaults to CN, CD, RU, IR, KP.

    Returns:
        {
            "company_events_inserted": int,
            "company_events_skipped_existing": int,
            "geography_events_inserted": int,
            "companies_matched": int,
            "total_entities_parsed": int,
        }
    """
    if high_concentration_geos is None:
        high_concentration_geos = list(_DEFAULT_HIGH_CONCENTRATION_GEOS)

    # --- Step 1: download and parse ------------------------------------------
    buf = download_sanctions_csv(url)
    entities = parse_sanctions_csv(buf)

    company_events_inserted = 0
    company_events_skipped = 0
    geography_events_inserted = 0
    batch_count = 0

    # --- Step 2: company match events ----------------------------------------
    matches = match_companies(session, entities)

    for company, matched_entities in matches:
        datasets = sorted({ds for e in matched_entities for ds in e["datasets"]})
        event_date = datetime.now(timezone.utc)
        title = f"{company.canonical_name} — Active Sanctions Match"
        summary = (
            f"Matched against {len(matched_entities)} sanctioned entity record(s) "
            f"in: {', '.join(datasets)}"
        )
        ch = _content_hash(title, summary, event_date)

        existing = session.scalar(
            select(RiskEvent).where(RiskEvent.content_hash == ch)
        )
        if existing is not None:
            log.debug(
                "opensanctions.company_event.skip",
                company=company.canonical_name,
                content_hash=ch,
            )
            company_events_skipped += 1
            continue

        primary_country = (
            matched_entities[0]["countries"][0]
            if matched_entities[0]["countries"]
            else None
        )

        event = RiskEvent(
            event_type="sanctions_listing",
            event_date=event_date,
            title=title,
            summary=summary,
            severity_score=1.0,
            confidence_score=1.0,
            risk_categories_json=["geopolitical_trade"],
            geography_json={"primary": primary_country},
            content_hash=ch,
            metadata_json={
                "opensanctions_ids": [e["opensanctions_id"] for e in matched_entities],
                "datasets": datasets,
            },
        )
        session.add(event)
        session.flush()

        session.add(
            RiskEventCompany(
                risk_event_id=event.id,
                company_id=company.id,
                relevance_score=1.0,
                match_reason="named_company",
            )
        )

        # One RiskEventGeography per unique country across all matched entities
        seen_countries: set[str] = set()
        for e in matched_entities:
            for country in e["countries"]:
                if country and country not in seen_countries:
                    seen_countries.add(country)
                    session.add(
                        RiskEventGeography(
                            risk_event_id=event.id,
                            country_code=country,
                            geography_context="primary",
                            relevance_score=1.0,
                        )
                    )

        log.info(
            "opensanctions.company_event.inserted",
            company=company.canonical_name,
            matched_count=len(matched_entities),
        )
        company_events_inserted += 1
        batch_count += 1
        if batch_count % _BATCH_SIZE == 0:
            session.flush()

    # --- Step 3: geography signal events -------------------------------------
    for country in high_concentration_geos:
        country_entities = [e for e in entities if country in e["countries"]]
        count = len(country_entities)
        if count == 0:
            log.debug("opensanctions.geo_event.skip_empty", country=country)
            continue

        dataset_counter: Counter[str] = Counter(
            ds for e in country_entities for ds in e["datasets"]
        )
        dataset_sample = sorted(ds for ds, _ in dataset_counter.most_common(5))

        event_date = datetime.now(timezone.utc)
        title = f"{country} — {count} Sanctioned Entities (OpenSanctions)"
        summary = (
            f"OpenSanctions bulk data contains {count} company/organisation records "
            f"linked to {country} across datasets: {', '.join(dataset_sample)}"
        )
        ch = _content_hash(title, summary, event_date)

        existing = session.scalar(
            select(RiskEvent).where(RiskEvent.content_hash == ch)
        )
        if existing is not None:
            log.debug("opensanctions.geo_event.skip", country=country, content_hash=ch)
            continue

        geo_event = RiskEvent(
            event_type="geography_sanctions_exposure",
            event_date=event_date,
            title=title,
            summary=summary,
            severity_score=min(1.0, count / 500),
            confidence_score=0.85,
            risk_categories_json=["geopolitical_trade"],
            geography_json={"primary": country},
            content_hash=ch,
            metadata_json={
                "sanctioned_entity_count": count,
                "datasets": dataset_sample,
            },
        )
        session.add(geo_event)
        session.flush()

        session.add(
            RiskEventGeography(
                risk_event_id=geo_event.id,
                country_code=country,
                geography_context="primary",
                relevance_score=1.0,
            )
        )

        log.info(
            "opensanctions.geo_event.inserted",
            country=country,
            sanctioned_count=count,
            severity=min(1.0, count / 500),
        )
        geography_events_inserted += 1
        batch_count += 1
        if batch_count % _BATCH_SIZE == 0:
            session.flush()

    # --- Step 4: commit -------------------------------------------------------
    session.commit()

    log.info(
        "opensanctions.ingest.done",
        company_events_inserted=company_events_inserted,
        company_events_skipped_existing=company_events_skipped,
        geography_events_inserted=geography_events_inserted,
        companies_matched=len(matches),
        total_entities_parsed=len(entities),
    )
    return {
        "company_events_inserted": company_events_inserted,
        "company_events_skipped_existing": company_events_skipped,
        "geography_events_inserted": geography_events_inserted,
        "companies_matched": len(matches),
        "total_entities_parsed": len(entities),
    }
