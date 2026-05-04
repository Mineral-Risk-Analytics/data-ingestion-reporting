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
from app.models.regulatory import RiskEvent, RiskEventCompany, RiskEventGeography, RiskEventHsMapping, RiskEventMaterial
from app.models.source import Source
from app.services.ingestion.entity_resolution import (
    build_company_cache,
    persist_company_links,
    resolve_companies_for_event,
)
from app.services.ingestion.normalizers.geography_resolver import GeographyCache
from app.services.ingestion.normalizers.material_resolver import MaterialCache
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

# ---------------------------------------------------------------------------
# Geography detection
# ---------------------------------------------------------------------------

# NOTE: _GEO_PATTERNS and _detect_geographies() were removed in Phase 2.
# Geography detection is now handled by GeographyCache (imported above), which
# loads country detection patterns from countries.detection_patterns (DB-backed).
# To add or update detection patterns, update seed_countries.py and re-run
# `bdi-ingest seed-countries` — no code change required.
#
# _detect_geographies() is kept as a thin shim for any external callers.
# Remove in Phase 3.

def _detect_geographies(
    text: str,
    _cache: "GeographyCache | None" = None,
) -> list[tuple[str, str, float]]:  # pragma: no cover
    """Deprecated shim — callers should build a GeographyCache and call .detect()."""
    if _cache is not None:
        return _cache.detect(text)
    # Fallback: return empty list if no cache provided (avoids DB call here).
    return []


# ---------------------------------------------------------------------------
# Material detection
# ---------------------------------------------------------------------------

# NOTE: MaterialCache was defined here through Phase 1 (PR 16b) and moved to
# normalizers/material_resolver.py in Phase 2 so that other ingesters can
# import it without a circular dependency.  The import at the top of this file
# re-exports MaterialCache; any existing callers of
#   from app.services.ingestion.ingest_federal_register import MaterialCache
# should be updated to use the normalizers path.

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
        # UFLPA events affect both regulatory compliance AND operational risk: entities added to the
        # UFLPA Entity List disrupt supplier relationships and mine/processing continuity directly.
        term="UFLPA",
        event_subtype="REGULATORY_COMPLIANCE",
        risk_categories=[
            RiskCategory.REGULATORY_COMPLIANCE.value,
            RiskCategory.OPERATIONAL.value,
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
# Persist helpers for new junction rows
# ---------------------------------------------------------------------------

def _persist_material_links(
    session: Session,
    event: RiskEvent,
    matches: list[tuple[int, float, str, int | None]],
) -> int:
    """
    Write RiskEventMaterial rows for each detected material AND
    RiskEventHsMapping rows where hs_mapping_id is known (stage attribution).

    Idempotent: skips pairs that already exist (unique constraints on
    risk_event_id + material_id and risk_event_id + hs_mapping_id).

    Returns the number of RiskEventMaterial rows written (not counting
    RiskEventHsMapping rows, which are a side-effect of stage attribution).
    """
    written = 0
    hs_written = 0

    for material_id, relevance, matched_keyword, hs_mapping_id in matches:
        # ── material-level junction (always) ──────────────────────────────
        existing_mat = session.scalar(
            select(RiskEventMaterial).where(
                RiskEventMaterial.risk_event_id == event.id,
                RiskEventMaterial.material_id == material_id,
            ).limit(1)
        )
        if existing_mat is None:
            session.add(RiskEventMaterial(
                risk_event_id=event.id,
                material_id=material_id,
                relevance_score=relevance,
                match_reason=f"keyword_match:{matched_keyword[:48]}",
            ))
            written += 1

        # ── stage-level junction (only when stage is known) ───────────────
        if hs_mapping_id is not None:
            existing_hs = session.scalar(
                select(RiskEventHsMapping).where(
                    RiskEventHsMapping.risk_event_id == event.id,
                    RiskEventHsMapping.hs_mapping_id == hs_mapping_id,
                ).limit(1)
            )
            if existing_hs is None:
                session.add(RiskEventHsMapping(
                    risk_event_id=event.id,
                    hs_mapping_id=hs_mapping_id,
                    relevance_score=relevance,
                    match_reason="keyword_match",
                ))
                hs_written += 1

    if written or hs_written:
        session.flush()
    return written


def _persist_geography_links(
    session: Session,
    event: RiskEvent,
    geos: list[tuple[str, str, float]],
) -> int:
    """
    Write RiskEventGeography rows for each detected country.
    Idempotent: skips pairs already present.
    Returns the number of rows written.
    """
    written = 0
    for iso2, context, relevance in geos:
        existing = session.scalar(
            select(RiskEventGeography).where(
                RiskEventGeography.risk_event_id == event.id,
                RiskEventGeography.country_code == iso2,
            ).limit(1)
        )
        if existing is not None:
            continue
        session.add(RiskEventGeography(
            risk_event_id=event.id,
            country_code=iso2,
            geography_context=context,
            relevance_score=relevance,
        ))
        written += 1
    if written:
        session.flush()
    return written


# ---------------------------------------------------------------------------
# Targeted patch for existing events
# ---------------------------------------------------------------------------

# Doc types where publication date and effective date meaningfully diverge.
# Notices and proposed rules rarely have a separate effective date — only
# final rules and presidential documents typically do.
_EFFECTIVE_DATE_DOC_TYPES = frozenset({
    "rule", "final rule", "interim final rule",
    "presidential document", "executive order", "proclamation",
})


def patch_fr_events(
    session: Session,
    *,
    fix_uflpa_routing: bool = True,
    fix_effective_dates: bool = True,
    dry_run: bool = False,
) -> dict[str, int]:
    """
    Retroactively apply two improvements to existing Federal Register events:

    1. UFLPA routing (fix_uflpa_routing=True):
       Adds RiskCategory.OPERATIONAL to the risk_categories_json of events
       ingested under the 'uflpa' query.  These events affect operational
       risk (supplier/mine continuity) as well as regulatory compliance, but
       the original ingest only tagged them REGULATORY_COMPLIANCE.
       Pure DB update — no API calls required.

    2. effective_on date (fix_effective_dates=True):
       Updates event_date on existing FR events from publication_date to
       effective_on where they differ.  Re-fetches effective_on from the
       Federal Register API in batches of 100 documents using the
       conditions[document_number][] parameter.  Only targets event types
       where the gap is meaningful (final rules, executive orders,
       presidential documents). Notices and proposed rules are skipped.

    Args:
        session:              SQLAlchemy session. Commits after each fix.
        fix_uflpa_routing:    Add OPERATIONAL category to UFLPA events.
        fix_effective_dates:  Re-fetch and apply effective_on dates.
        dry_run:              Compute changes but do NOT write or commit.

    Returns:
        {
            "uflpa_events_patched": int,
            "effective_dates_fetched": int,
            "effective_dates_updated": int,
            "api_errors": int,
        }
    """
    uflpa_patched = 0
    effective_fetched = 0
    effective_updated = 0
    api_errors = 0

    # ── Fix 1: UFLPA → OPERATIONAL routing ──────────────────────────────────
    if fix_uflpa_routing:
        uflpa_events = list(session.scalars(
            select(RiskEvent).where(
                RiskEvent.event_type == "federal_register_notice",
                # JSONB path operator: metadata_json->>'query_name' = 'uflpa'
                RiskEvent.metadata_json["query_name"].astext == "uflpa",
            )
        ).all())

        operational_val = RiskCategory.OPERATIONAL.value
        for ev in uflpa_events:
            cats: list = list(ev.risk_categories_json or [])
            if operational_val not in cats:
                if not dry_run:
                    cats.append(operational_val)
                    ev.risk_categories_json = cats
                uflpa_patched += 1

        if not dry_run and uflpa_patched:
            session.commit()

        log.info(
            "patch_fr_events.uflpa_routing",
            dry_run=dry_run,
            patched=uflpa_patched,
        )

    # ── Fix 2: effective_on dates ────────────────────────────────────────────
    if fix_effective_dates:
        from app.models.documents import SourceDocument

        # Only target doc types where publication ≠ effective matters.
        # metadata_json->>'doc_type' is stored by _upsert_source_document.
        candidate_rows = list(session.execute(
            select(RiskEvent, SourceDocument)
            .join(
                SourceDocument,
                SourceDocument.id == RiskEvent.source_document_id,
            )
            .where(
                RiskEvent.event_type == "federal_register_notice",
                # Lower-case comparison for safety; FR returns mixed-case types.
                # Use func.lower on the JSONB text extraction.
                SourceDocument.metadata_json["doc_type"].astext.ilike(
                    "%rule%"
                )
                | SourceDocument.metadata_json["doc_type"].astext.ilike(
                    "%presidential%"
                )
                | SourceDocument.metadata_json["doc_type"].astext.ilike(
                    "%executive order%"
                )
                | SourceDocument.metadata_json["doc_type"].astext.ilike(
                    "%proclamation%"
                ),
            )
        ).all())

        if not candidate_rows:
            log.info("patch_fr_events.effective_dates.no_candidates")
        else:
            log.info(
                "patch_fr_events.effective_dates.candidates",
                count=len(candidate_rows),
            )

            # Build (doc_number → (event, source_doc)) mapping
            doc_map: dict[str, tuple] = {}
            for ev, src in candidate_rows:
                doc_number = (
                    (src.metadata_json or {}).get("document_number")
                    or src.external_id
                )
                if doc_number:
                    doc_map[doc_number] = (ev, src)

            # Batch-fetch effective_on from the FR API, 100 documents per call.
            # The FR API supports conditions[document_number][] for multi-doc lookup.
            doc_numbers = list(doc_map.keys())
            _BATCH = 100

            with httpx.Client(follow_redirects=True) as client:
                for batch_start in range(0, len(doc_numbers), _BATCH):
                    batch = doc_numbers[batch_start: batch_start + _BATCH]
                    time.sleep(_RATE_LIMIT_DELAY)

                    try:
                        effective_map = _fetch_effective_on_batch(client, batch)
                        effective_fetched += len(effective_map)
                    except Exception as exc:
                        log.warning(
                            "patch_fr_events.effective_dates.api_error",
                            batch_start=batch_start,
                            error=str(exc),
                        )
                        api_errors += 1
                        continue

                    for doc_num, effective_on in effective_map.items():
                        if doc_num not in doc_map:
                            continue
                        ev, src = doc_map[doc_num]
                        if not effective_on:
                            continue

                        new_dt = datetime.combine(
                            effective_on, datetime.min.time(), tzinfo=timezone.utc
                        )

                        # Skip if the stored event_date already matches
                        if ev.event_date and abs(
                            (ev.event_date.replace(tzinfo=timezone.utc)
                             if ev.event_date.tzinfo is None
                             else ev.event_date)
                            - new_dt
                        ).days == 0:
                            continue

                        if not dry_run:
                            ev.event_date = new_dt
                            # Store pub_date in metadata_json if not already there
                            meta = dict(ev.metadata_json or {})
                            if "publication_date" not in meta and src.published_at:
                                meta["publication_date"] = src.published_at.date().isoformat()
                            meta["effective_on"] = effective_on.isoformat()
                            ev.metadata_json = meta

                        effective_updated += 1
                        log.debug(
                            "patch_fr_events.effective_dates.updated",
                            doc_number=doc_num,
                            old_date=ev.event_date.isoformat() if ev.event_date else None,
                            new_date=new_dt.isoformat(),
                        )

            if not dry_run and effective_updated:
                session.commit()

        log.info(
            "patch_fr_events.effective_dates",
            dry_run=dry_run,
            fetched=effective_fetched,
            updated=effective_updated,
            api_errors=api_errors,
        )

    log.info(
        "patch_fr_events.done",
        dry_run=dry_run,
        uflpa_patched=uflpa_patched,
        effective_updated=effective_updated,
    )

    return {
        "uflpa_events_patched": uflpa_patched,
        "effective_dates_fetched": effective_fetched,
        "effective_dates_updated": effective_updated,
        "api_errors": api_errors,
    }


def _fetch_effective_on_batch(
    client: httpx.Client,
    document_numbers: list[str],
) -> dict[str, date]:
    """
    Fetch effective_on dates for a list of document numbers using the FR API
    single-document endpoint (/documents/{doc_number}.json).

    The search endpoint does not support conditions[document_number][] bulk
    lookup, so we call the per-document endpoint once per number. Callers
    should pass batches of reasonable size (≤100) and respect rate limits
    between calls. Returns {document_number: effective_on_date} for documents
    that have a non-null effective_on. Documents without one are omitted.
    """
    result: dict[str, date] = {}
    for doc_num in document_numbers:
        url = f"{_BASE_URL}/documents/{doc_num}.json?fields[]=effective_on&fields[]=publication_date"
        try:
            resp = client.get(url, timeout=_API_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
            raw_eff = data.get("effective_on")
            if raw_eff:
                result[doc_num] = datetime.strptime(raw_eff[:10], "%Y-%m-%d").date()
        except Exception as exc:
            log.debug(
                "patch_fr_events.effective_dates.doc_fetch_error",
                doc_number=doc_num,
                error=str(exc),
            )
        time.sleep(_RATE_LIMIT_DELAY)
    return result


# ---------------------------------------------------------------------------
# Historical backfill
# ---------------------------------------------------------------------------

def backfill_fr_links(
    session: Session,
    *,
    force: bool = False,
    batch_size: int = 100,
    dry_run: bool = False,
) -> dict[str, int]:
    """
    Retroactively tag existing Federal Register risk events with
    RiskEventMaterial and RiskEventGeography rows.

    Events ingested before material/geography detection was added have no
    junction rows and are invisible to get_events_for_material() and any
    geography-filtered queries. This function scans those events and fills
    in the missing rows using the same detection logic as the live ingester.

    Idempotent: the persist helpers skip pairs that already exist, so re-runs
    are safe. Use --force to re-scan events that already have some links (useful
    after adding new aliases or geo patterns).

    Args:
        session:    SQLAlchemy session. Commits after each batch of events.
        force:      If True, run detection on ALL FR events regardless of
                    whether they already have links. The per-row uniqueness
                    constraint means no duplicates will be created.
                    Default False: skips events that already have at least one
                    material link OR at least one geography link.
        batch_size: Number of events to process before committing. Default 100.
        dry_run:    Detect and count but do NOT write or commit any rows.

    Returns:
        {
            "events_scanned": int,
            "events_skipped": int,    # already fully tagged (no-force mode)
            "events_tagged": int,     # had at least one new link written
            "material_links_written": int,
            "geography_links_written": int,
        }
    """
    material_cache = MaterialCache.build(session)
    geo_cache = GeographyCache.build(session)

    # Subqueries to check existing links — used only in non-force mode to
    # skip events already tagged. exists() is much cheaper than COUNT for
    # large event tables.
    def _has_material_link(event_id: int) -> bool:
        return session.scalar(
            select(RiskEventMaterial.material_id)
            .where(RiskEventMaterial.risk_event_id == event_id)
            .limit(1)
        ) is not None

    def _has_geography_link(event_id: int) -> bool:
        return session.scalar(
            select(RiskEventGeography.country_code)
            .where(RiskEventGeography.risk_event_id == event_id)
            .limit(1)
        ) is not None

    # Only process FR-sourced events.
    # event_type is set to "federal_register_notice" by _insert_risk_event.
    events: list[RiskEvent] = list(
        session.scalars(
            select(RiskEvent).where(
                RiskEvent.event_type == "federal_register_notice"
            )
        ).all()
    )

    events_scanned = 0
    events_skipped = 0
    events_tagged = 0
    material_links_written = 0
    geography_links_written = 0
    pending_since_commit = 0

    for ev in events:
        events_scanned += 1

        # In default mode skip events that already have links on both sides.
        if not force:
            if _has_material_link(ev.id) and _has_geography_link(ev.id):
                events_skipped += 1
                continue

        # Reconstruct search text from stored title + summary.
        search_text = " ".join(filter(None, [ev.title, ev.summary]))
        if not search_text.strip():
            events_skipped += 1
            continue

        detected_materials = material_cache.detect(search_text)
        detected_geos = geo_cache.detect(search_text)

        if dry_run:
            if detected_materials or detected_geos:
                events_tagged += 1
                material_links_written += len(detected_materials)
                geography_links_written += len(detected_geos)
            continue

        mat_written = _persist_material_links(session, ev, detected_materials)
        geo_written = _persist_geography_links(session, ev, detected_geos)

        if mat_written or geo_written:
            events_tagged += 1
            material_links_written += mat_written
            geography_links_written += geo_written

        pending_since_commit += 1
        if pending_since_commit >= batch_size:
            session.commit()
            pending_since_commit = 0

        log.debug(
            "backfill_fr_links.event_tagged",
            event_id=ev.id,
            materials=mat_written,
            geographies=geo_written,
        )

    if not dry_run and pending_since_commit:
        session.commit()

    log.info(
        "backfill_fr_links.done",
        dry_run=dry_run,
        force=force,
        events_scanned=events_scanned,
        events_skipped=events_skipped,
        events_tagged=events_tagged,
        material_links_written=material_links_written,
        geography_links_written=geography_links_written,
    )

    return {
        "events_scanned": events_scanned,
        "events_skipped": events_skipped,
        "events_tagged": events_tagged,
        "material_links_written": material_links_written,
        "geography_links_written": geography_links_written,
    }


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
            # publication_date preserved here so callers can distinguish
            # from event_date (which may be effective_on for final rules).
            "publication_date": doc.published_at.date().isoformat()
            if doc.published_at else None,
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
    material_cache = MaterialCache.build(session)
    geo_cache = GeographyCache.build(session)

    # Track document_numbers claimed by earlier queries to avoid duplicate events
    # when the same FR document matches multiple search terms.
    claimed_doc_numbers: set[str] = set()

    documents_created = 0
    events_created = 0
    company_links = 0
    material_links = 0
    geography_links = 0
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

                    # Prefer effective_on over publication_date for event_date.
                    # Using pub_dt alone biases regulations as "older" than they are
                    # in effect — a rule published in January but effective in July
                    # would be time-weighted as a 6-month-old signal on day one.
                    effective_dt = (
                        datetime.combine(
                            parsed.effective_date, datetime.min.time(), tzinfo=timezone.utc
                        )
                        if parsed.effective_date else None
                    )
                    event_dt = effective_dt or pub_dt

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
                            "effective_on": parsed.effective_date.isoformat()
                            if parsed.effective_date else None,
                        },
                    )

                    if is_new:
                        documents_created += 1

                    severity = _severity(
                        parsed.doc_type, parsed.abstract_text, parsed.title, query
                    )

                    # Detect geographies and materials from title + abstract
                    search_text = " ".join(filter(None, [parsed.title, parsed.abstract_text]))
                    detected_geos = geo_cache.detect(search_text)
                    detected_materials = material_cache.detect(search_text)

                    # Set geo_primary to the highest-relevance primary geography
                    # (used for display in RiskEvent.geography_json)
                    geo_primary = None
                    primary_geos = [
                        (iso2, score) for iso2, ctx, score in detected_geos
                        if ctx == "primary"
                    ]
                    if primary_geos:
                        geo_primary = max(primary_geos, key=lambda x: x[1])[0]

                    ev = _insert_risk_event(
                        session=session,
                        doc=doc,
                        title=parsed.title or doc_number,
                        summary=parsed.abstract_text,
                        event_date=event_dt,
                        severity=severity,
                        risk_categories=query.risk_categories,
                        query=query,
                        geo_primary=geo_primary,
                        agencies=parsed.agencies,
                        content_hash=ch,
                    )
                    events_created += 1

                    # Tag materials detected in title/abstract
                    mat_written = _persist_material_links(session, ev, detected_materials)
                    material_links += mat_written

                    # Tag geographies detected in title/abstract
                    geo_written = _persist_geography_links(session, ev, detected_geos)
                    geography_links += geo_written

                    # Resolve and link companies
                    co_matches = resolve_companies_for_event(session, ev, company_cache)
                    persist_company_links(session, ev, co_matches)
                    linked = len(co_matches)
                    company_links += linked

                    log.debug(
                        "ingest_federal_register.event_created",
                        document_number=doc_number,
                        subtype=query.event_subtype,
                        severity=severity,
                        companies_linked=linked,
                        materials_linked=mat_written,
                        geographies_linked=geo_written,
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
        material_links=material_links,
        geography_links=geography_links,
        skipped_existing=skipped_existing,
        api_errors=api_errors,
    )
    return {
        "documents_created": documents_created,
        "events_created": events_created,
        "company_links": company_links,
        "material_links": material_links,
        "geography_links": geography_links,
        "skipped_existing": skipped_existing,
        "api_errors": api_errors,
    }
