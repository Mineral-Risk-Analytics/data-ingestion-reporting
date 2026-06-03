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
    a tracked material (previously lost — the audit's N3 follow-up gap;
    currently gated off until Workstream B lands real body-text fetch)
  * sets ``event_subtype='FINANCIAL_PRESSURE'`` on the typed column
    (migration 040) so downstream filters fire correctly
  * removes one source of architectural inconsistency

Workstream A (2026-05-23) additions
-----------------------------------
* Issuer enrichment per CIK: refreshes ``Company.sic``, ``sic_description``,
  ``exchanges``, ``sec_metadata`` (addresses + fiscalYearEnd + etc.) from
  the submissions JSON, and upserts ``CompanyAlias`` rows for formerNames
  and dual-class tickers.  Looked up by ``Company.cik`` (migration 044);
  CIKs with no matching Company row are listed in ``companies_missing``
  in the result dict so partner curation can fill the gap.
* Per-filing metadata threaded into ``SourceDocument.metadata_json``:
  ``items`` (8-K item codes parsed from the comma-separated string),
  ``primary_doc_description``, ``is_xbrl`` / ``is_inline_xbrl``,
  ``size_bytes``, ``file_number``.  8-K items are the highest-value
  addition — they enable the deferred 8-K-item → event_subtype mapping.
* Material attribution gated off when ``ParsedFiling.is_narrative_placeholder``
  is True (the current default for all rows) so the Haiku classifier
  doesn't burn API calls on stub narrative text.

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

from datetime import datetime, timezone
from typing import Optional

import httpx
import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.utils.hashing import sha256_bytes
from app.models import (
    Company,
    CompanyAlias,
    RiskEvent,
    RiskEventHsMapping,
    RiskEventMaterial,
    Source,
    SourceDocument,
)
from app.models.enums import DocumentType, ImplementationPhase, SourceType
from app.services.ingestion import feature_flags
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
    metadata = {
        "cik": filing.cik,
        "form": filing.form,
        "ticker": filing.ticker,
        "accession": filing.accession_number,
        # Workstream A #3 (2026-05-23): per-filing metadata threaded from
        # filings.recent.* arrays.  Carries 8-K item codes (the highest-
        # value addition — drives event_subtype once that mapping lands),
        # plus XBRL availability flags for future structured-data ingest
        # priority, plus primaryDocDescription for human-readable filing
        # context, plus size/fileNumber for audit.  See filing_parser.py
        # for source field names.
        "items": filing.items,
        "primary_doc_description": filing.primary_doc_description,
        "is_xbrl": filing.is_xbrl,
        "is_inline_xbrl": filing.is_inline_xbrl,
        "size_bytes": filing.size_bytes,
        "file_number": filing.file_number,
    }
    if existing is not None:
        existing.title = title
        existing.url = filing.primary_document_url
        existing.published_at = filing.filed_at
        existing.document_type = DocumentType.FILING.value
        existing.raw_text = filing.narrative_excerpt
        existing.metadata_json = metadata
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
        metadata_json=metadata,
        checksum=checksum,
    )
    session.add(doc)
    session.flush()
    return doc


def _upsert_alias(
    session: Session,
    company_id,
    alias: str,
    alias_type: str,
) -> bool:
    """Insert ``CompanyAlias`` if (company_id, alias) doesn't exist yet.

    Returns True when a new row is written, False when the alias already
    existed.  Idempotent so the SEC enrichment can be re-run safely without
    creating duplicates on every CIK refresh.
    """
    cleaned = (alias or "").strip()
    if not cleaned:
        return False
    existing = session.scalar(
        select(CompanyAlias)
        .where(
            CompanyAlias.company_id == company_id,
            CompanyAlias.alias == cleaned,
        )
        .limit(1)
    )
    if existing is not None:
        return False
    session.add(
        CompanyAlias(
            company_id=company_id,
            alias=cleaned[:512],
            alias_type=alias_type,
        )
    )
    return True


def _enrich_company_from_submissions(
    session: Session,
    company: Company,
    data: dict,
) -> dict[str, int]:
    """Refresh issuer-level ``Company`` fields from SEC submissions JSON.

    Field policy:
      * SEC-canonical fields (sic, sic_description, exchanges, sec_metadata)
        are **refreshed on every call** — SEC is the source of truth.
      * Partner-curatable fields (legal_name, public_ticker) are only set
        when currently NULL — partner edits win.
      * ``is_public`` is flipped to True if currently False (presence in
        SEC submissions is proof of public-issuer status).
      * Aliases (formerNames + extra tickers beyond the first) are upserted
        — existing rows preserved, missing rows added.

    Returns a counts dict for caller reporting.  Safe to call repeatedly.
    """
    counts = {"fields_refreshed": 0, "fields_set": 0, "aliases_added": 0}

    # ── SEC-canonical fields (always refresh) ──────────────────────────
    sic = data.get("sic")
    if sic:
        sic_str = str(sic)[:4]
        if company.sic != sic_str:
            company.sic = sic_str
            counts["fields_refreshed"] += 1

    sic_desc = data.get("sicDescription")
    if sic_desc:
        trimmed = str(sic_desc)[:256]
        if company.sic_description != trimmed:
            company.sic_description = trimmed
            counts["fields_refreshed"] += 1

    exchanges = data.get("exchanges") or []
    if exchanges:
        normalized = [str(e) for e in exchanges]
        if (company.exchanges or []) != normalized:
            company.exchanges = normalized
            counts["fields_refreshed"] += 1

    # sec_metadata snapshot — overwrite every run.  This is our bag of
    # SEC-derived fields that don't warrant first-class columns yet
    # (addresses, fiscal year end, category, entity type, phones,
    # description URL, EIN).  The synced_at timestamp makes staleness
    # visible without an extra column.
    addresses = data.get("addresses") or {}
    sec_metadata = {
        "name": data.get("name"),
        "ein": data.get("ein"),
        "category": data.get("category"),
        "entity_type": data.get("entityType"),
        "fiscal_year_end": data.get("fiscalYearEnd"),
        "phone": data.get("phone"),
        "description": data.get("description"),
        "website": data.get("website"),
        "investor_website": data.get("investorWebsite"),
        "flags": data.get("flags"),
        "former_names_raw": data.get("formerNames"),
        "business_address": addresses.get("business"),
        "mailing_address": addresses.get("mailing"),
        "synced_at": datetime.now(timezone.utc).isoformat(),
    }
    company.sec_metadata = sec_metadata
    counts["fields_refreshed"] += 1

    # ── Partner-curatable fields (only set when NULL) ─────────────────
    if company.is_public is False:
        company.is_public = True
        counts["fields_set"] += 1

    name = data.get("name")
    if company.legal_name is None and isinstance(name, str) and name:
        company.legal_name = name[:512]
        counts["fields_set"] += 1

    tickers = data.get("tickers") or []
    if tickers and company.public_ticker is None:
        company.public_ticker = str(tickers[0])[:32]
        counts["fields_set"] += 1

    # ── Aliases (upsert) ──────────────────────────────────────────────
    # Additional tickers beyond [0] (dual-class shares, secondary listings,
    # ADRs).  alias_type="ticker" so the resolver can disambiguate later.
    for extra_ticker in tickers[1:]:
        if _upsert_alias(session, company.id, str(extra_ticker), "ticker"):
            counts["aliases_added"] += 1

    # formerNames — SEC publishes as list of {"name": "...", "from": "...",
    # "to": "..."}.  Take .name; the from/to dates live in
    # sec_metadata.former_names_raw for audit.
    for fn in data.get("formerNames") or []:
        former = fn.get("name") if isinstance(fn, dict) else fn
        if former and isinstance(former, str):
            if _upsert_alias(session, company.id, former, "former_name"):
                counts["aliases_added"] += 1

    return counts


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

    # Material attribution caches: only built when the per-event gate has
    # a chance of opening.  Today every ParsedFiling is a narrative-stub
    # placeholder (Workstream B will land real body-text extraction), so
    # we lazy-init these to None and rebuild only inside the per-event
    # branch.  This avoids the per-run MaterialCache.build() scan and the
    # one-time materials_by_id load for what would be guaranteed-skip runs.
    material_cache = None
    material_classifier = None

    # Gated by feature_flags.LINK_EVENTS_TO_COMPANIES — when False (the
    # Foundation phase 3 default), persist_company_links no-ops, so there
    # is no value in paying for the cache build or the per-event resolver
    # loop.  Setting to None makes the per-event flow short-circuit.
    company_cache = (
        build_company_cache(session)
        if feature_flags.LINK_EVENTS_TO_COMPANIES
        else None
    )

    filings_seen = 0
    filings_inserted = 0
    risk_events_inserted = 0
    mat_links_total = 0
    hs_links_total = 0
    company_links_total = 0
    companies_enriched = 0
    companies_missing: list[str] = []
    issuer_fields_refreshed = 0
    issuer_fields_set = 0
    issuer_aliases_added = 0

    try:
        for raw_cik in cik_list:
            data = _fetch_submissions(http_client, raw_cik)
            if not data:
                continue

            # ── Workstream A #2 (2026-05-23): issuer enrichment ───────
            # Look up the Company row by CIK and refresh issuer-level
            # fields from the submissions JSON (sic, exchanges, addresses,
            # formerNames, extra tickers).  Best-effort: a missing Company
            # is logged but does NOT block the per-filing event creation
            # below — partner curation is the source of truth for which
            # companies are tracked, and we don't auto-create Company
            # stubs from CIK alone.  Run backfill-cik-map first if you
            # haven't, otherwise issuers will skip with companies_missing.
            cik_padded = str(raw_cik).strip().zfill(10)
            company = session.scalar(
                select(Company).where(Company.cik == cik_padded).limit(1)
            )
            if company is None:
                companies_missing.append(cik_padded)
                log.warning(
                    "sec_edgar.enrich.company_missing",
                    cik=cik_padded,
                    issuer_name=data.get("name"),
                    note=(
                        "No Company row has this CIK.  Run "
                        "`bdi-ingest backfill-cik-map` or add the "
                        "company to the partner-curated seed template."
                    ),
                )
            else:
                enrich_counts = _enrich_company_from_submissions(
                    session, company, data
                )
                companies_enriched += 1
                issuer_fields_refreshed += enrich_counts["fields_refreshed"]
                issuer_fields_set += enrich_counts["fields_set"]
                issuer_aliases_added += enrich_counts["aliases_added"]

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
                    session, source=source, filing=pf
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
                    items=pf.items,
                )
                event = RiskEvent(
                    source_document_id=doc.id,
                    event_type=draft.event_type,
                    # Migration 040: typed event_subtype column.  Pre-2026-
                    # 05-23 we hardcoded "FINANCIAL_PRESSURE" here which
                    # collapsed the full 8-K item space into one bucket.
                    # Now draft.event_subtype carries the granular value
                    # mapped by sec_subtype_map.map_sec_filing — see that
                    # module for the form + 8-K-item priority logic.
                    event_subtype=draft.event_subtype,
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

                # ── 3. Material attribution: gated on real narrative ──────
                # Two-step attribution (Tier 3 audit, 2026-05-09):
                #   a) MaterialCache.detect = cheap keyword-based candidate
                #      set (capped at top-3 by Tier 1.3).
                #   b) MaterialClassifier.classify = optional Haiku refiner
                #      that decides whether each candidate is the filing's
                #      actual subject vs an incidental risk-factor mention.
                #
                # GATE (2026-05-23): the submissions endpoint only returns
                # metadata, so ParsedFiling.narrative_excerpt is a hardcoded
                # stub like "10-K filing for Tesla, Inc. (CIK 0001318605).".
                # Running detect+classify on the stub means:
                #   - keyword match only ever fires when the issuer NAME
                #     contains a material keyword (handful of names like
                #     "Lithium Americas Corp" — useless for 99% of filers);
                #   - every event still issues a Haiku call against
                #     trivial text, burning API budget for ~zero signal.
                # We therefore skip the whole block when the narrative is a
                # placeholder.  Once Workstream B lands real filing-body
                # extraction (Items 1A / 2 / 7 for 10-K, full body for 8-K),
                # ParsedFiling.is_narrative_placeholder will be False and
                # this gate will auto-open.
                if not pf.is_narrative_placeholder:
                    # Lazy-init the caches on first real narrative — see
                    # block-2 comment near `material_cache = None` above
                    # for why these are deferred until needed.
                    if material_cache is None:
                        material_cache = MaterialCache.build(session)
                    if material_classifier is None:
                        from app.models.supply import Material
                        from app.services.ingestion.material_classifier import (
                            MaterialClassifier,
                        )
                        materials_by_id = {
                            m.id: m.canonical_name
                            for m in session.scalars(select(Material)).all()
                        }
                        material_classifier = MaterialClassifier(
                            materials_by_id=materials_by_id
                        )
                        log.info(
                            "sec_edgar.classifier_status",
                            enabled=material_classifier.enabled,
                            note=(
                                "Haiku refinement active"
                                if material_classifier.enabled
                                else "keyword-only attribution "
                                "(set ANTHROPIC_API_KEY to enable)"
                            ),
                        )

                    search_text = " ".join(
                        filter(None, [event.title, event.summary])
                    )
                    if search_text.strip():
                        detected = material_cache.detect(search_text)
                        refined = material_classifier.classify(
                            search_text, detected
                        )
                        mat_w, hs_w = _persist_material_links(
                            session, event, refined
                        )
                        mat_links_total += mat_w
                        hs_links_total += hs_w

                # ── 4. Company linkage via existing entity_resolution ──────
                # Gated by feature_flags.LINK_EVENTS_TO_COMPANIES (default
                # False — Foundation phase 3).  Skip the resolver loop
                # entirely when the flag is off; persist_company_links
                # would no-op anyway, and the resolver cost is a per-event
                # scan over every Company row.
                if feature_flags.LINK_EVENTS_TO_COMPANIES and company_cache is not None:
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
        # Workstream A #2 issuer-enrichment counts (added 2026-05-23).
        # companies_missing surfaces CIKs that need partner curation or
        # backfill; non-empty list is a soft warning, not a failure.
        "companies_enriched": companies_enriched,
        "companies_missing": companies_missing,
        "issuer_fields_refreshed": issuer_fields_refreshed,
        "issuer_fields_set": issuer_fields_set,
        "issuer_aliases_added": issuer_aliases_added,
    }
    log.info("sec_edgar.ingest.done", **result)
    return result
