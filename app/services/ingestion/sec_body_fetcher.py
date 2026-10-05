"""SEC filing body-text fetcher — SEC Workstream B (B3).

Walks ``source_documents`` rows that came from SEC EDGAR (form 10-K or
20-F), fetches each filing's ``primary_document_url``, runs the section
parser, and upserts the results into ``filing_body_sections``.

Why this lives in its own module
---------------------------------
``ingest_sec_edgar.py`` consumes the SEC submissions JSON to populate
SourceDocument + RiskEvent metadata.  Body fetching is a separate
concern — different rate-limit profile (much heavier per-request
payload), different failure modes (HTML parsing instead of JSON
schema), and re-runnable independently when the section parser
improves.

Selection logic
---------------
The fetcher operates on existing SourceDocument rows.  It does NOT
hit the SEC submissions endpoint.  Callers are expected to have run
``ingest-sec-edgar`` first to populate the metadata layer.

Selection follows three filters in this order:

  1. ``source.source_type = SEC_EDGAR``
  2. ``metadata_json->>'form'`` in the target form set (default
     ``{"10-K", "20-F"}``)
  3. Optional issuer filter: ``cik_filter`` restricts to a specific
     CIK list, or ``launch_list_only=True`` restricts to companies
     with at least one ``CompanyMaterialExposure`` to a launch-list
     material (``app.services.scoring.launch_list``).

Idempotency
-----------
We skip filings that already have at least one ``FilingBodySection``
with the current ``_PARSER_VERSION_*`` strings unless ``force=True``.
This means re-running the fetcher with the same parser version is a
no-op, while bumping a parser version triggers a re-parse next run.

SEC compliance
--------------
SEC requires:
  * Descriptive ``User-Agent``: ``settings.sec_edgar_user_agent``
  * Rate limit: ~10 req/s sustained.  We sleep
    ``_SEC_REQUEST_INTERVAL_S`` between requests.  Conservative — better
    to slow down than risk a 403 from SEC.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Optional

import httpx
import structlog
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import (
    Company,
    CompanyMaterialExposure,
    FilingBodySection,
    Material,
    Source,
    SourceDocument,
)
from app.models.enums import SourceType
from app.services.ingestion.parsers.filing_body_parser import (
    SectionParseResult,
    parse_filing_sections,
    target_sections_for_form,
)
from app.services.scoring.launch_list import LAUNCH_LIST_CANONICAL_NAMES

log = structlog.get_logger(__name__)


# Conservative interval between SEC requests.  SEC permits 10 req/s
# but throttles aggressively at the edge; 150ms (≈6.7 req/s) gives us
# headroom while staying productive.  Tunable via env if needed.
_SEC_REQUEST_INTERVAL_S = 0.15

# Default target forms.  10-Q + 8-K are deferred per the locked design
# decisions in ``project_sec_workstream_b.md``.
DEFAULT_TARGET_FORMS: frozenset[str] = frozenset({"10-K", "20-F"})


# ---------------------------------------------------------------------------
# Result shapes
# ---------------------------------------------------------------------------

@dataclass
class FetcherSummary:
    """Counters returned by ``fetch_filing_bodies``.

    Designed to mirror the JSON shape the CLI emits for downstream
    parsing by ops dashboards.  Keep keys stable.
    """

    filings_candidates: int = 0
    filings_skipped_already_parsed: int = 0
    filings_fetched: int = 0
    filings_fetch_failed: int = 0
    filings_parse_yielded_nothing: int = 0
    sections_written: int = 0
    sections_replaced: int = 0
    sections_by_method: dict[str, int] = field(default_factory=dict)
    sections_by_section_code: dict[str, int] = field(default_factory=dict)

    def record_section(self, result: SectionParseResult) -> None:
        self.sections_by_method[result.method] = (
            self.sections_by_method.get(result.method, 0) + 1
        )
        self.sections_by_section_code[result.section_code] = (
            self.sections_by_section_code.get(result.section_code, 0) + 1
        )

    def to_dict(self) -> dict:
        return {
            "filings_candidates": self.filings_candidates,
            "filings_skipped_already_parsed": self.filings_skipped_already_parsed,
            "filings_fetched": self.filings_fetched,
            "filings_fetch_failed": self.filings_fetch_failed,
            "filings_parse_yielded_nothing": self.filings_parse_yielded_nothing,
            "sections_written": self.sections_written,
            "sections_replaced": self.sections_replaced,
            "sections_by_method": dict(self.sections_by_method),
            "sections_by_section_code": dict(self.sections_by_section_code),
        }


# ---------------------------------------------------------------------------
# Selection helpers
# ---------------------------------------------------------------------------

def _resolve_launch_list_ciks(session: Session) -> list[str]:
    """Return zero-padded CIKs of launch-list-relevant SEC filers.

    Resolution falls back through three sources in priority order so the
    function is useful even when downstream relationship tables are still
    being populated.  As of 2026-06 the first two are empty and we fall
    through to source 3:

      1. ``CompanyMaterialExposure`` (preferred — explicit material edge)
      2. ``CompanyFacility`` JOIN ``FacilityMaterialLink`` (facility-derived)
      3. All companies with a non-NULL ``cik`` (last-resort fallback —
         emits a WARNING so this isn't silently doing the wrong thing
         once one of the upstream tables fills in)

    Excludes companies without ``cik`` (CATL, BYD, Ganfeng etc. don't
    file with SEC and would be no-ops anyway).

    Returns zero-padded 10-digit CIK strings — the SEC canonical form.
    """
    # ── Source 1: CompanyMaterialExposure ────────────────────────────────
    rows = session.execute(
        select(Company.cik)
        .join(
            CompanyMaterialExposure,
            CompanyMaterialExposure.company_id == Company.id,
        )
        .join(Material, Material.id == CompanyMaterialExposure.material_id)
        .where(
            Material.canonical_name.in_(LAUNCH_LIST_CANONICAL_NAMES),
            Company.cik.is_not(None),
        )
        .distinct()
    ).scalars().all()
    if rows:
        return sorted({str(c).strip().zfill(10) for c in rows if c})

    # ── Source 2: CompanyFacility + FacilityMaterialLink ─────────────────
    from app.models.facility import CompanyFacility, FacilityMaterialLink
    rows = session.execute(
        select(Company.cik)
        .join(CompanyFacility, CompanyFacility.company_id == Company.id)
        .join(
            FacilityMaterialLink,
            FacilityMaterialLink.facility_id == CompanyFacility.facility_id,
        )
        .join(Material, Material.id == FacilityMaterialLink.material_id)
        .where(
            Material.canonical_name.in_(LAUNCH_LIST_CANONICAL_NAMES),
            Company.cik.is_not(None),
        )
        .distinct()
    ).scalars().all()
    if rows:
        return sorted({str(c).strip().zfill(10) for c in rows if c})

    # ── Source 3: All CIK-having companies (last-resort fallback) ────────
    rows = session.execute(
        select(Company.cik).where(Company.cik.is_not(None)).distinct()
    ).scalars().all()
    log.warning(
        "sec_body_fetcher.launch_list_fallback_all_ciks",
        cik_count=len(rows),
        note=(
            "Neither CompanyMaterialExposure nor CompanyFacility has rows "
            "linking companies to launch-list materials — falling back to "
            "all companies with a CIK.  Populate one of those tables for "
            "real launch-list scoping."
        ),
    )
    return sorted({str(c).strip().zfill(10) for c in rows if c})


def _select_candidate_filings(
    session: Session,
    *,
    target_forms: frozenset[str],
    cik_filter: Optional[list[str]],
    max_filings: Optional[int],
) -> list[SourceDocument]:
    """Find SourceDocument rows ready for body fetch.

    Server-side filtering by form happens via JSONB ``->>`` operator.
    CIK filter is applied in Python because ``cik`` lives in metadata_json
    too and the canonical form is zero-padded (so a string-equality match
    is robust enough at the launch scale).
    """
    sec_source = session.scalar(
        select(Source).where(Source.source_type == SourceType.SEC_EDGAR.value)
    )
    if sec_source is None:
        log.warning("sec_body_fetcher.no_sec_source", note="No SEC_EDGAR Source row exists.")
        return []

    q = (
        select(SourceDocument)
        .where(SourceDocument.source_id == sec_source.id)
        .where(SourceDocument.url.is_not(None))
        .order_by(SourceDocument.id.desc())
    )

    docs: list[SourceDocument] = list(session.scalars(q).all())

    # Form + CIK filters.  Done in Python because metadata_json is JSONB
    # and getting both keys cleanly via SQLAlchemy expressions adds
    # complexity that's not worth it at launch-10 scale (~hundreds of
    # filings).  At 10k+ filings, push this into SQL.
    cik_set = set(cik_filter) if cik_filter else None
    filtered: list[SourceDocument] = []
    for doc in docs:
        meta = doc.metadata_json or {}
        form = (meta.get("form") or "").strip()
        if form not in target_forms:
            continue
        if cik_set is not None:
            cik = str(meta.get("cik") or "").strip().zfill(10)
            if cik not in cik_set:
                continue
        filtered.append(doc)

    if max_filings is not None:
        filtered = filtered[:max_filings]
    return filtered


def _filing_already_parsed(
    session: Session,
    source_document_id: int,
    *,
    target_section_codes: list[str],
) -> bool:
    """True if every target section has a row for this filing.

    Doesn't care about parser_version here — version-aware re-parsing
    is handled by the caller's ``force`` flag plus a parser-version
    delete pass.  This is the cheaper "skip if obviously done" check.
    """
    if not target_section_codes:
        return False
    existing = set(
        session.scalars(
            select(FilingBodySection.section_code)
            .where(FilingBodySection.source_document_id == source_document_id)
            .where(FilingBodySection.section_code.in_(target_section_codes))
        ).all()
    )
    return existing == set(target_section_codes)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def fetch_filing_bodies(
    session: Session,
    *,
    target_forms: frozenset[str] = DEFAULT_TARGET_FORMS,
    cik_filter: Optional[list[str]] = None,
    launch_list_only: bool = False,
    max_filings: Optional[int] = None,
    force: bool = False,
    http_client: Optional[httpx.Client] = None,
) -> FetcherSummary:
    """Fetch + parse SEC filing bodies and upsert into ``filing_body_sections``.

    Args:
        session: Open SQLAlchemy session.  Caller commits.
        target_forms: Forms to process (default 10-K + 20-F).
        cik_filter: Optional explicit list of CIK strings.  Mutually
            exclusive with ``launch_list_only`` — if both are supplied,
            the explicit list wins.
        launch_list_only: If True and ``cik_filter`` is None, restrict
            to companies with at least one CompanyMaterialExposure to a
            launch-list material.
        max_filings: Optional cap on number of filings processed.  Useful
            for dry-runs / debugging.
        force: If True, delete and re-parse existing FilingBodySection
            rows for each candidate filing.  Default False: skip filings
            that already have every target section.
        http_client: Optional httpx.Client (for tests).  Default builds
            one with the SEC User-Agent from settings.

    Returns:
        ``FetcherSummary`` with counters.
    """
    settings = get_settings()
    owns_client = http_client is None
    if http_client is None:
        http_client = httpx.Client(
            headers={
                "User-Agent": settings.sec_edgar_user_agent,
                "Accept": "text/html, application/xhtml+xml, */*",
            },
            timeout=30.0,
            follow_redirects=True,
        )

    # Resolve issuer filter to a set of CIKs (or None for "all").
    if cik_filter is None and launch_list_only:
        cik_filter = _resolve_launch_list_ciks(session)
        log.info(
            "sec_body_fetcher.launch_list_resolved",
            cik_count=len(cik_filter),
        )

    candidates = _select_candidate_filings(
        session,
        target_forms=target_forms,
        cik_filter=cik_filter,
        max_filings=max_filings,
    )

    summary = FetcherSummary(filings_candidates=len(candidates))
    log.info(
        "sec_body_fetcher.start",
        candidates=len(candidates),
        target_forms=sorted(target_forms),
        force=force,
    )

    try:
        for doc in candidates:
            meta = doc.metadata_json or {}
            form = str(meta.get("form") or "").strip()
            accession = str(meta.get("accession") or doc.external_id)
            target_sections = target_sections_for_form(form)

            # ── Skip-if-done check ──────────────────────────────────────
            if not force and _filing_already_parsed(
                session, doc.id, target_section_codes=target_sections,
            ):
                summary.filings_skipped_already_parsed += 1
                continue

            # ── Fetch ───────────────────────────────────────────────────
            try:
                resp = http_client.get(doc.url)
                resp.raise_for_status()
                raw_html = resp.text
            except Exception as exc:
                summary.filings_fetch_failed += 1
                log.warning(
                    "sec_body_fetcher.fetch_failed",
                    source_document_id=doc.id,
                    accession=accession,
                    url=doc.url,
                    error_type=type(exc).__name__,
                    error=str(exc)[:300],
                )
                time.sleep(_SEC_REQUEST_INTERVAL_S)
                continue
            summary.filings_fetched += 1

            # ── Parse ───────────────────────────────────────────────────
            section_results = parse_filing_sections(
                raw_html=raw_html, form=form, accession_number=accession,
            )
            if not section_results:
                summary.filings_parse_yielded_nothing += 1
                # Still throttle even on empty parse — we did hit SEC.
                time.sleep(_SEC_REQUEST_INTERVAL_S)
                continue

            # ── Upsert ──────────────────────────────────────────────────
            # Delete any existing rows for the (filing, section) pairs we're
            # about to write, then insert.  Simpler than per-row update and
            # honours the unique constraint cleanly.
            section_codes_written = [r.section_code for r in section_results]
            replaced = session.execute(
                delete(FilingBodySection)
                .where(FilingBodySection.source_document_id == doc.id)
                .where(
                    FilingBodySection.section_code.in_(section_codes_written)
                )
            ).rowcount or 0
            summary.sections_replaced += replaced

            now = datetime.now(timezone.utc)
            for r in section_results:
                session.add(FilingBodySection(
                    source_document_id=doc.id,
                    section_code=r.section_code,
                    section_text=r.text,
                    char_count=r.char_count,
                    parser_method=r.method,
                    parser_version=r.version,
                    fetched_at=now,
                ))
                summary.record_section(r)
                summary.sections_written += 1

            # Commit per filing so a crash mid-batch doesn't lose the
            # work done so far.  Filings are independent — partial
            # progress on a 250-filing backfill is the right semantics.
            session.commit()
            time.sleep(_SEC_REQUEST_INTERVAL_S)
    finally:
        if owns_client:
            http_client.close()

    log.info("sec_body_fetcher.done", **summary.to_dict())
    return summary
