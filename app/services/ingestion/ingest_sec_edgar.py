"""SEC EDGAR filing ingestion — dedicated module.

Replaces the generic ``IngestionPipeline`` path for SEC EDGAR (May 2026
refactor).  Pattern matches ``ingest_federal_register.py`` and the other
dedicated ingester modules: explicit entry function, in-module fetcher,
material attribution via ``MaterialCache``, idempotent SourceDocument
upsert.

Why the migration
-----------------
``pipeline.py`` is a Phase-1 generic dispatcher that never grew material
or HS attribution.  Every other source in the codebase had moved to
dedicated ingesters with proper attribution; SEC EDGAR was the last
production caller of the generic path.  Closing it here:
  * adds ``RiskEventMaterial`` rows when the filing narrative mentions
    a tracked material (previously lost — the audit's N3 follow-up gap)
  * sets ``event_subtype='FINANCIAL_PRESSURE'`` on the typed column
    (migration 040) so downstream filters fire correctly
  * removes one source of architectural inconsistency

Source identifier
-----------------
SEC EDGAR submissions endpoint requires a descriptive User-Agent per
https://www.sec.gov/os/accessing-edgar-data.  Set via env:
    SEC_EDGAR_USER_AGENT="YourCompanyName contact@yourdomain.com"

Coverage notes
--------------
US domestic filers submit 10-K / 10-Q / 8-K.  Foreign private issuers
submit 20-F / 6-K.  Companies without a US CIK (CATL, VW, BMW etc.) do
not file with the SEC and are excluded — the operational pillar's
financial-pressure signals for them come from other sources.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional

import httpx
import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.utils.hashing import sha256_bytes
from app.models import (
    RiskEvent,
    RiskEventHsMapping,
    RiskEventMaterial,
    Source,
    SourceDocument,
)
from app.models.enums import DocumentType, ImplementationPhase, SourceType
from app.services.ingestion.entity_resolution import (
    build_company_cache,
    persist_company_links,
    resolve_companies_for_event,
)
from app.services.ingestion.normalizers.event_normalizer import (
    build_sec_filing_event,
)
from app.services.ingestion.normalizers.material_resolver import MaterialCache
from app.services.ingestion.parsers.filing_parser import (
    ParsedFiling,
    parse_sec_filing,
)

log = structlog.get_logger(__name__)


_SOURCE_NAME = "SEC EDGAR — battery supply chain filers"


def _get_or_create_source(session: Session) -> Source:
    """Idempotent Source row for SEC EDGAR.  Mirrors the eurlex pattern."""
    existing = session.scalar(
        select(Source).where(
            Source.source_type == SourceType.SEC_EDGAR.value,
            Source.is_active.is_(True),
        ).limit(1)
    )
    if existing is not None:
        return existing
    src = Source(
        name=_SOURCE_NAME,
        source_type=SourceType.SEC_EDGAR.value,
        phase=ImplementationPhase.PHASE_1.value,
        is_active=True,
        config_json={"method": "submissions_api"},
    )
    session.add(src)
    session.flush()
    return src


def _fetch_submissions(client: httpx.Client, cik: str) -> dict:
    """GET /submissions/CIK<cik>.json — never raises, returns {} on failure
    so one bad CIK doesn't poison the whole run.
    """
    settings = get_settings()
    cik_padded = str(cik).strip().zfill(10)
    endpoint = (
        f"{settings.sec_edgar_base_url.rstrip('/')}/submissions/CIK{cik_padded}.json"
    )
    try:
        resp = client.get(endpoint, timeout=60.0)
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict):
            log.warning("sec_edgar.unexpected_payload", cik=cik_padded)
            return {}
        return data
    except httpx.HTTPError as exc:
        log.warning(
            "sec_edgar.fetch_error",
            cik=cik_padded,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return {}


def _upsert_source_document(
    session: Session,
    *,
    source: Source,
    filing: ParsedFiling,
    raw_blob: dict,
) -> SourceDocument:
    """Idempotent upsert keyed on (source_id, accession_number)."""
    ext_id = filing.accession_number.replace("/", "-")
    existing = session.scalar(
        select(SourceDocument).where(
            SourceDocument.source_id == source.id,
            SourceDocument.external_id == ext_id,
        )
    )
    title = f"{filing.form} — {filing.company_name or filing.cik}"
    checksum = sha256_bytes(
        f"{filing.accession_number}:{filing.form}".encode("utf-8")
    )
    if existing is not None:
        existing.title = title
        existing.url = filing.primary_document_url
        existing.published_at = filing.filed_at
        existing.document_type = DocumentType.FILING.value
        existing.raw_text = filing.narrative_excerpt
        existing.metadata_json = {
            "cik": filing.cik,
            "form": filing.form,
            "ticker": filing.ticker,
            "accession": filing.accession_number,
        }
        existing.checksum = checksum
        existing.fetched_at = datetime.now(timezone.utc)
        session.flush()
        return existing
    doc = SourceDocument(
        source_id=source.id,
        external_id=ext_id,
        title=title,
        url=filing.primary_document_url,
        published_at=filing.filed_at,
        document_type=DocumentType.FILING.value,
        raw_text=filing.narrative_excerpt,
        metadata_json={
            "cik": filing.cik,
            "form": filing.form,
            "ticker": filing.ticker,
            "accession": filing.accession_number,
        },
        checksum=checksum,
    )
    session.add(doc)
    session.flush()
    return doc


def _persist_material_links(
    session: Session,
    event: RiskEvent,
    matches: list[tuple[int, float, str, int | None]],
) -> tuple[int, int]:
    """Write RiskEventMaterial + RiskEventHsMapping rows for each match.

    Returns (material_links_written, hs_links_written).  Idempotent — skips
    pairs that already exist.  Logic mirrors
    ``ingest_federal_register._persist_material_links``.
    """
    mat_written = 0
    hs_written = 0
    for material_id, relevance, matched_keyword, hs_mapping_id in matches:
        existing_mat = session.scalar(
            select(RiskEventMaterial)
            .where(
                RiskEventMaterial.risk_event_id == event.id,
                RiskEventMaterial.material_id == material_id,
            )
            .limit(1)
        )
        if existing_mat is None:
            session.add(
                RiskEventMaterial(
                    risk_event_id=event.id,
                    material_id=material_id,
                    relevance_score=relevance,
                    match_reason=f"keyword_match:{matched_keyword[:48]}",
                )
            )
            mat_written += 1

        if hs_mapping_id is not None:
            existing_hs = session.scalar(
                select(RiskEventHsMapping)
                .where(
                    RiskEventHsMapping.risk_event_id == event.id,
                    RiskEventHsMapping.hs_mapping_id == hs_mapping_id,
                )
                .limit(1)
            )
            if existing_hs is None:
                session.add(
                    RiskEventHsMapping(
                        risk_event_id=event.id,
                        hs_mapping_id=hs_mapping_id,
                        relevance_score=relevance,
                        match_reason="keyword_match",
                    )
                )
                hs_written += 1

    if mat_written or hs_written:
        session.flush()
    return mat_written, hs_written


def ingest_sec_edgar(
    session: Session,
    cik_list: list[str],
    *,
    max_filings: int = 12,
    http_client: Optional[httpx.Client] = None,
) -> dict[str, int]:
    """Ingest SEC EDGAR submissions for the supplied list of CIKs.

    Args:
        session:       SQLAlchemy session.  Committed at the end of a
                       successful run.
        cik_list:      List of CIKs (zero-padded or unpadded; padded
                       internally).  Pass an empty list to no-op.
        max_filings:   Maximum recent filings per CIK to ingest.  Default
                       12 catches ~3 years of quarterly filings or ~12
                       years of annual filings depending on issuer.
        http_client:   Inject an ``httpx.Client`` for tests.  If None,
                       constructs one with the SEC-required User-Agent.

    Returns:
        Counts dict — useful for both CLI / Inngest reporting:
            ciks_processed:           int
            filings_seen:             int
            filings_inserted:         int  (new SourceDocument rows)
            risk_events_inserted:     int
            material_links_written:   int
            hs_links_written:         int
            company_links_written:    int
    """
    if not cik_list:
        log.warning("sec_edgar.ingest.no_ciks_provided")
        return {
            "ciks_processed": 0,
            "filings_seen": 0,
            "filings_inserted": 0,
            "risk_events_inserted": 0,
            "material_links_written": 0,
            "hs_links_written": 0,
            "company_links_written": 0,
        }

    settings = get_settings()
    owns_client = http_client is None
    if http_client is None:
        http_client = httpx.Client(
            headers={"User-Agent": settings.sec_edgar_user_agent},
        )

    source = _get_or_create_source(session)
    material_cache = MaterialCache.build(session)
    company_cache = build_company_cache(session)

    filings_seen = 0
    filings_inserted = 0
    risk_events_inserted = 0
    mat_links_total = 0
    hs_links_total = 0
    company_links_total = 0

    try:
        for raw_cik in cik_list:
            data = _fetch_submissions(http_client, raw_cik)
            if not data:
                continue

            parsed_filings = parse_sec_filing(data, max_filings=max_filings)
            for pf in parsed_filings:
                filings_seen += 1

                # ── 1. SourceDocument upsert (idempotent on accession) ─────
                doc_before = session.scalar(
                    select(SourceDocument).where(
                        SourceDocument.source_id == source.id,
                        SourceDocument.external_id ==
                        pf.accession_number.replace("/", "-"),
                    )
                )
                doc = _upsert_source_document(
                    session, source=source, filing=pf, raw_blob=data
                )
                if doc_before is None:
                    filings_inserted += 1

                # ── 2. RiskEvent — skip if one already exists for this doc ─
                existing_event = session.scalar(
                    select(RiskEvent)
                    .where(RiskEvent.source_document_id == doc.id)
                    .limit(1)
                )
                if existing_event is not None:
                    continue

                draft = build_sec_filing_event(
                    form=pf.form,
                    company=pf.company_name,
                    accession=pf.accession_number,
                    filed_at=pf.filed_at,
                    narrative=pf.narrative_excerpt,
                )
                event = RiskEvent(
                    source_document_id=doc.id,
                    event_type=draft.event_type,
                    # Migration 040: typed event_subtype column.  Drives
                    # the financial-pressure pillar's filing-signal evidence
                    # query in evidence_query.
                    event_subtype="FINANCIAL_PRESSURE",
                    event_date=draft.event_date,
                    title=draft.title[:1024],
                    summary=draft.summary,
                    severity_score=draft.severity_score,
                    confidence_score=draft.confidence_score,
                    risk_categories_json=draft.risk_categories,
                    geography_json=draft.geography,
                    metadata_json=draft.metadata,
                    # SEC filings are partner-curated only at the CIK-list
                    # level; individual filings stream in unverified.
                    verified=False,
                )
                session.add(event)
                session.flush()
                risk_events_inserted += 1

                # ── 3. Material attribution via MaterialCache (NEW vs pipeline) ──
                # The old pipeline.py path ran no MaterialCache and produced
                # zero material/HS junctions.  Run detect over the filing's
                # narrative excerpt — typically the part of the 10-K /
                # 8-K that mentions specific commodity exposures, supply
                # chain risk, or material-cost callouts.
                search_text = " ".join(
                    filter(None, [event.title, event.summary])
                )
                if search_text.strip():
                    detected = material_cache.detect(search_text)
                    mat_w, hs_w = _persist_material_links(
                        session, event, detected
                    )
                    mat_links_total += mat_w
                    hs_links_total += hs_w

                # ── 4. Company linkage via existing entity_resolution ──────
                matches = resolve_companies_for_event(
                    session, event, company_cache
                )
                persist_company_links(session, event, matches)
                company_links_total += len(matches)

        session.commit()

    finally:
        if owns_client:
            http_client.close()

    result = {
        "ciks_processed": len(cik_list),
        "filings_seen": filings_seen,
        "filings_inserted": filings_inserted,
        "risk_events_inserted": risk_events_inserted,
        "material_links_written": mat_links_total,
        "hs_links_written": hs_links_total,
        "company_links_written": company_links_total,
    }
    log.info("sec_edgar.ingest.done", **result)
    return result
