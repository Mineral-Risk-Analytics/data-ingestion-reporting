"""
Federal Register ingestion: fetch policy notices and create GEOPOLITICAL_TRADE
and REGULATORY_COMPLIANCE RiskEvent rows with precise event_subtype classification.

Why standalone (not through the generic IngestionPipeline)
-----------------------------------------------------------
The generic pipeline's build_regulatory_risk_event() does not set event_subtype
in metadata_json, so events never populate the geo pillar's export_restriction_exposure
or tariff_exposure sub-inputs. This module classifies each document before creating
the event, ensuring the right evidence_aggregator path fires.

Signal types produced
---------------------
  EXPORT_RESTRICTION  → feeds geo pillar export_restriction_exposure sub-input
                        (evidence_aggregator checks event_subtype == "EXPORT_RESTRICTION")
  TARIFF              → feeds geo pillar tariff_exposure sub-input
  TRADE_POLICY        → feeds geo pillar tariff_exposure sub-input (same aggregator path)
  REGULATORY_COMPLIANCE → feeds regulatory pillar event-driven portion

Targeted search queries
-----------------------
Four query groups are run in sequence, each covering a distinct risk signal type.
Documents matching multiple queries are deduplicated by content_hash — the event
is classified by whichever query first claims it, in priority order:
  1. export_control  (EXPORT_RESTRICTION, highest scoring impact)
  2. tariff          (TARIFF)
  3. uflpa           (REGULATORY_COMPLIANCE — specific to battery supply chain policy)
  4. critical_minerals (TRADE_POLICY — broader market signals)

Severity calibration
--------------------
Base severity by document type:
  Rule (final)       → 0.70   confirmed policy, full enforcement
  Proposed Rule      → 0.50   signal only, may change
  Notice             → 0.40   informational / procedural
  Presidential Doc   → 0.80   executive authority
  Other              → 0.40

+0.12 per high-signal keyword hit (battery, lithium, graphite, cobalt, nickel,
UFLPA, FEOC, Xinjiang, Section 301, Section 232), capped at 0.92.

API notes
---------
  Base URL:  https://www.federalregister.gov/api/v1
  Endpoint:  GET /documents.json
  Auth:      None — completely public API
  Rate limit: none documented; 0.5s sleep between pages is conservative

Idempotency
-----------
content_hash (SHA-256 of document_number + publication_date) prevents duplicate
RiskEvent rows on re-run. SourceDocument rows are deduplicated by (source_id,
external_id) — re-runs update the document but skip event + company link creation.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Optional
from urllib.parse import quote, urlencode

import httpx
import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.constants import RiskCategory
from app.models.documents import SourceDocument
from app.models.enums import DocumentType, ImplementationPhase, SourceType
from app.models.regulatory import RiskEvent, RiskEventCompany
from app.models.source import Source
from app.services.ingestion.entity_resolution import (
    build_company_cache,
    persist_company_links,
    resolve_companies_for_event,
)
from app.services.ingestion.parsers.regulation_parser import parse_federal_register_document

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_BASE_URL = "https://www.federalregister.gov/api/v1"
_SOURCE_NAME = "Federal Register API"
_PER_PAGE = 100          # max items per API page (FR allows up to 1000, 100 is safe)
_RATE_LIMIT_DELAY = 0.5  # seconds between API calls
_API_TIMEOUT = httpx.Timeout(connect=15.0, read=60.0, write=10.0, pool=10.0)

# Severity boost per keyword hit; base is set by doc type (see _severity())
_KEYWORD_BOOST = 0.12

# Keywords that signal high relevance to battery supply chain
_HIGH_SIGNAL_KEYWORDS = frozenset({
    "battery", "lithium", "graphite", "cobalt", "nickel", "manganese",
    "uflpa", "feoc", "xinjiang", "forced labor", "forced labour",
    "section 301", "section 232", "critical mineral", "electric vehicle",
    "inflation reduction act", "domestic content", "clean vehicle",
    "anode", "cathode", "cathode active material", "precursor",
})

# ---------------------------------------------------------------------------
# Query configuration
# ---------------------------------------------------------------------------

@dataclass
class QueryConfig:
    name: str
    term: str
    event_subtype: str
    risk_categories: list[str]
    base_severity_boost: float = 0.0  # added on top of doc-type base


# Queries run in this order; a document is claimed by the FIRST query that matches it.
# Priority order: export_control > tariff > uflpa > critical_minerals > lithium_battery
#
# NOTE: The FR API full-text search (conditions[term]) does NOT support boolean OR
# expressions like "term1" OR "term2". Multiple space-separated words are treated as
# AND — all must appear in the document. Queries are kept short and specific so the
# AND semantics still return relevant, targeted results.
TARGETED_QUERIES: list[QueryConfig] = [
    QueryConfig(
        name="export_control",
        term="export restriction battery",
        event_subtype="EXPORT_RESTRICTION",
        risk_categories=[
            RiskCategory.GEOPOLITICAL_TRADE.value,
            RiskCategory.REGULATORY_COMPLIANCE.value,
        ],
        base_severity_boost=0.15,
    ),
    QueryConfig(
        name="tariff",
        term="tariff battery",
        event_subtype="TARIFF",
        risk_categories=[
            RiskCategory.GEOPOLITICAL_TRADE.value,
        ],
        base_severity_boost=0.10,
    ),
    QueryConfig(
        name="uflpa",
        # UFLPA alone is highly specific — every UFLPA document is supply-chain relevant.
        # "UFLPA forced labor" co-occurrence is rare in document text so the paired query returns 0.
        term="UFLPA",
        event_subtype="REGULATORY_COMPLIANCE",
        risk_categories=[
            RiskCategory.REGULATORY_COMPLIANCE.value,
        ],
        base_severity_boost=0.20,
    ),
    QueryConfig(
        name="critical_minerals",
        term="critical mineral",
        event_subtype="TRADE_POLICY",
        risk_categories=[
            RiskCategory.GEOPOLITICAL_TRADE.value,
            RiskCategory.REGULATORY_COMPLIANCE.value,
        ],
        base_severity_boost=0.0,
    ),
    QueryConfig(
        name="lithium_battery",
        # Separate pass for lithium/cobalt supply chain docs not caught by critical_minerals.
        term="lithium battery supply",
        event_subtype="TRADE_POLICY",
        risk_categories=[
            RiskCategory.GEOPOLITICAL_TRADE.value,
            RiskCategory.REGULATORY_COMPLIANCE.value,
        ],
        base_severity_boost=0.0,
    ),
]


# ---------------------------------------------------------------------------
# Severity scoring
# ---------------------------------------------------------------------------

_DOC_TYPE_SEVERITY: dict[str, float] = {
    "rule":                  0.70,
    "final rule":            0.70,
    "interim final rule":    0.65,
    "proposed rule":         0.50,
    "advance notice":        0.40,
    "notice":                0.40,
    "presidential document": 0.80,
    "executive order":       0.85,
    "proclamation":          0.75,
}


def _severity(doc_type: str | None, abstract: str | None, title: str | None,
              query: QueryConfig) -> float:
    doc_type_norm = (doc_type or "notice").lower()
    base = _DOC_TYPE_SEVERITY.get(doc_type_norm, 0.40)
    base += query.base_severity_boost

    # Keyword boost
    text = " ".join(filter(None, [abstract, title])).lower()
    hits = sum(1 for kw in _HIGH_SIGNAL_KEYWORDS if kw in text)
    boosted = base + min(0.22, hits * _KEYWORD_BOOST)

    return round(min(0.92, max(0.30, boosted)), 2)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _content_hash(document_number: str, publication_date: str) -> str:
    payload = f"{document_number}|{publication_date}"
    return hashlib.sha256(payload.encode()).hexdigest()


def _get_or_create_source(session: Session) -> Source:
    existing = session.scalar(
        select(Source).where(Source.name == _SOURCE_NAME)
    )
    if existing:
        return existing
    source = Source(
        name=_SOURCE_NAME,
        source_type=SourceType.FEDERAL_REGISTER.value,
        phase=ImplementationPhase.PHASE_1.value,
        is_active=True,
        config_json={"base_url": _BASE_URL},
    )
    session.add(source)
    session.flush()
    log.info("ingest_federal_register.source_created", source_id=source.id)
    return source


def _upsert_source_document(
    session: Session,
    source: Source,
    external_id: str,
    title: str | None,
    url: str | None,
    published_at: datetime | None,
    abstract: str | None,
    metadata_json: dict,
) -> tuple[SourceDocument, bool]:
    """Returns (document, is_new). is_new=False means event already created — skip."""
    existing = session.scalar(
        select(SourceDocument).where(
            SourceDocument.source_id == source.id,
            SourceDocument.external_id == external_id,
        )
    )
    if existing:
        return existing, False

    doc = SourceDocument(
        source_id=source.id,
        external_id=external_id,
        title=title,
        url=url,
        published_at=published_at,
        document_type=DocumentType.API_JSON.value,
        raw_text=abstract,
        metadata_json=metadata_json,
    )
    session.add(doc)
    session.flush()
    return doc, True


def _event_exists_by_hash(session: Session, content_hash: str) -> bool:
    return session.scalar(
        select(RiskEvent.id).where(RiskEvent.content_hash == content_hash).limit(1)
    ) is not None


def _insert_risk_event(
    session: Session,
    doc: SourceDocument,
    title: str,
    summary: str | None,
    event_date: datetime | None,
    severity: float,
    risk_categories: list[str],
    query: QueryConfig,
    geo_primary: str | None,
    agencies: list[str],
    content_hash: str,
) -> RiskEvent:
    ev = RiskEvent(
        source_document_id=doc.id,
        event_type="federal_register_notice",
        event_date=event_date,
        title=title[:1024],
        summary=summary,
        severity_score=severity,
        confidence_score=0.70,
        risk_categories_json=risk_categories,
        geography_json={"primary": geo_primary, "scope": "federal"},
        content_hash=content_hash,
        metadata_json={
            "event_subtype": query.event_subtype,
            "query_name": query.name,
            "agencies": agencies[:8],
            "source": "federal_register_api",
        },
    )
    session.add(ev)
    session.flush()
    return ev


# ---------------------------------------------------------------------------
# API fetch
# ---------------------------------------------------------------------------

def _fetch_page(
    client: httpx.Client,
    query: QueryConfig,
    since_date: date,
    page: int,
) -> tuple[list[dict], int]:
    """
    Fetch one page of Federal Register documents for the given query.
    Returns (items, total_pages).

    URL is built manually with urlencode(doseq=True) to preserve bracket-style
    parameter names (conditions[term], fields[], etc.) exactly as the FR API
    expects — passing a params dict to httpx causes it to percent-encode the
    brackets, which the Rails backend may not match against its routing rules.
    """
    # Build the query string manually with literal square brackets.
    # Both urllib.parse.urlencode (default) and httpx params= percent-encode
    # brackets → conditions%5Bterm%5D — which the FR API Rails router does not
    # match. Passing quote_via with safe='[]' keeps the brackets literal, matching
    # the form the FR API documentation shows and expects.
    def _q(s: str, safe: str, encoding: str | None = None, errors: str | None = None) -> str:
        return quote(str(s), safe="[]", encoding=encoding, errors=errors)

    param_pairs: list[tuple[str, str]] = [
        ("conditions[term]", query.term),
        ("conditions[publication_date][gte]", since_date.isoformat()),
        ("per_page", str(_PER_PAGE)),
        ("page", str(page)),
        ("order", "newest"),
    ]
    # conditions[type][] must use the FR API's short abbreviation codes.
    # Passing human-readable names ("Rule", "Notice", ...) silently matches
    # zero documents even though the API returns 200 OK. The codes below
    # correspond to: final Rule, Proposed Rule, Notice, Presidential Document.
    # (The `type` field on returned documents is still the human-readable form,
    # which parse_federal_register_document expects — only the filter is coded.)
    for doc_type_code in ("RULE", "PRORULE", "NOTICE", "PRESDOCU"):
        param_pairs.append(("conditions[type][]", doc_type_code))
    for field in (
        "document_number", "title", "abstract", "publication_date",
        "effective_on", "agencies", "html_url", "pdf_url",
        "raw_text_url", "topics", "type",
    ):
        param_pairs.append(("fields[]", field))

    url = f"{_BASE_URL}/documents.json?{urlencode(param_pairs, quote_via=_q)}"
    resp = client.get(url, timeout=_API_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()

    items = data.get("results") or []
    total_pages = data.get("total_pages") or 1

    return [r for r in items if isinstance(r, dict)], int(total_pages)


# ---------------------------------------------------------------------------
# Main ingest function
# ---------------------------------------------------------------------------

def ingest_federal_register(
    session: Session,
    since_date: Optional[date] = None,
    max_pages: int = 5,
    queries: Optional[list[QueryConfig]] = None,
) -> dict[str, int]:
    """
    Fetch Federal Register notices and create GEOPOLITICAL_TRADE /
    REGULATORY_COMPLIANCE RiskEvent rows with precise event_subtype classification.

    Args:
        session:    SQLAlchemy session. Commits internally after each query batch.
        since_date: Only fetch documents published on or after this date.
                    Default: 90 days ago. Pass date(2020, 1, 1) for a historical backfill.
        max_pages:  Maximum pages to fetch per query (each page = up to 100 documents).
                    Default 5 → up to 500 documents per query, 2000 total across 4 queries.
        queries:    Override the default TARGETED_QUERIES list. Primarily for testing.

    Returns:
        {
            "documents_created": int,
            "events_created": int,
            "company_links": int,
            "skipped_existing": int,
            "api_errors": int,
        }
    """
    if since_date is None:
        since_date = date.today() - timedelta(days=90)

    active_queries = queries if queries is not None else TARGETED_QUERIES

    log.info(
        "ingest_federal_register.start",
        since_date=since_date.isoformat(),
        max_pages=max_pages,
        query_count=len(active_queries),
    )

    source = _get_or_create_source(session)
    company_cache = build_company_cache(session)

    # Track document_numbers claimed by earlier queries to avoid duplicate events
    # when the same FR document matches multiple search terms.
    claimed_doc_numbers: set[str] = set()

    documents_created = 0
    events_created = 0
    company_links = 0
    skipped_existing = 0
    api_errors = 0

    with httpx.Client(follow_redirects=True) as client:
        for query in active_queries:
            log.info(
                "ingest_federal_register.query_start",
                query=query.name,
                event_subtype=query.event_subtype,
            )

            for page in range(1, max_pages + 1):
                time.sleep(_RATE_LIMIT_DELAY)

                try:
                    items, total_pages = _fetch_page(client, query, since_date, page)
                except (httpx.HTTPStatusError, httpx.TimeoutException, ValueError) as exc:
                    log.warning(
                        "ingest_federal_register.api_error",
                        query=query.name,
                        page=page,
                        error=str(exc),
                    )
                    api_errors += 1
                    break
                except Exception as exc:
                    # Catch-all so unexpected errors (ConnectError, SSLError, etc.)
                    # surface in api_errors rather than silently aborting the run.
                    log.warning(
                        "ingest_federal_register.api_error_unexpected",
                        query=query.name,
                        page=page,
                        error_type=type(exc).__name__,
                        error=str(exc),
                    )
                    api_errors += 1
                    break

                log.info(
                    "ingest_federal_register.page_fetched",
                    query=query.name,
                    page=page,
                    items_returned=len(items),
                    total_pages=total_pages,
                )

                for item in items:
                    parsed = parse_federal_register_document(item)
                    if not parsed.external_id:
                        continue

                    doc_number = parsed.document_number or parsed.external_id

                    # Skip if already claimed by a higher-priority query
                    if doc_number in claimed_doc_numbers:
                        skipped_existing += 1
                        continue
                    claimed_doc_numbers.add(doc_number)

                    ch = _content_hash(
                        doc_number,
                        parsed.publication_date.isoformat() if parsed.publication_date else "",
                    )

                    # Check for existing risk event (idempotent re-runs)
                    if _event_exists_by_hash(session, ch):
                        skipped_existing += 1
                        continue

                    pub_dt = (
                        datetime.combine(
                            parsed.publication_date, datetime.min.time(), tzinfo=timezone.utc
                        )
                        if parsed.publication_date else None
                    )

                    doc, is_new = _upsert_source_document(
                        session=session,
                        source=source,
                        external_id=parsed.external_id,
                        title=parsed.title,
                        url=parsed.html_url or parsed.pdf_url,
                        published_at=pub_dt,
                        abstract=parsed.abstract_text,
                        metadata_json={
                            "document_number": doc_number,
                            "agencies": parsed.agencies[:8],
                            "topics": parsed.topics[:10],
                            "doc_type": parsed.doc_type,
                            "query_name": query.name,
                        },
                    )

                    if is_new:
                        documents_created += 1

                    severity = _severity(
                        parsed.doc_type, parsed.abstract_text, parsed.title, query
                    )

                    # Detect primary geography from agency names / abstract
                    geo_primary = None
                    text = " ".join(filter(None, [parsed.abstract_text, parsed.title])).lower()
                    if any(w in text for w in ("china", "chinese", "cn ", "prc")):
                        geo_primary = "CN"
                    elif any(w in text for w in ("russia", "russian", "ru ")):
                        geo_primary = "RU"

                    ev = _insert_risk_event(
                        session=session,
                        doc=doc,
                        title=parsed.title or doc_number,
                        summary=parsed.abstract_text,
                        event_date=pub_dt,
                        severity=severity,
                        risk_categories=query.risk_categories,
                        query=query,
                        geo_primary=geo_primary,
                        agencies=parsed.agencies,
                        content_hash=ch,
                    )
                    events_created += 1

                    # Resolve and link companies
                    matches = resolve_companies_for_event(session, ev, company_cache)
                    persist_company_links(session, ev, matches)
                    linked = len(matches)
                    company_links += linked

                    log.debug(
                        "ingest_federal_register.event_created",
                        document_number=doc_number,
                        subtype=query.event_subtype,
                        severity=severity,
                        companies_linked=linked,
                    )

                # Commit after each page to return DB connection to pool
                session.commit()

                log.info(
                    "ingest_federal_register.page_done",
                    query=query.name,
                    page=page,
                    total_pages=total_pages,
                )

                if page >= total_pages:
                    break

    log.info(
        "ingest_federal_register.done",
        documents_created=documents_created,
        events_created=events_created,
        company_links=company_links,
        skipped_existing=skipped_existing,
        api_errors=api_errors,
    )
    return {
        "documents_created": documents_created,
        "events_created": events_created,
        "company_links": company_links,
        "skipped_existing": skipped_existing,
        "api_errors": api_errors,
    }
