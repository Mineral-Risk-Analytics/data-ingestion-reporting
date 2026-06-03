"""
Parse SEC EDGAR submission / filing metadata into internal structures.

Phase 1: works off `submissions` JSON (`filings.recent` arrays and company metadata).

Note on ``narrative_excerpt``: today this is a hardcoded placeholder of the
form ``"<FORM> filing for <ISSUER> (CIK <CIK>)."`` because the submissions
endpoint only returns metadata, not filing text.  Workstream B is the
follow-up to fetch and parse ``primary_document_url`` (Items 1A / 2 / 7
from 10-K, full body from 8-K).  Until then ``is_narrative_placeholder``
is True for every row this parser emits — the ingester uses that flag
to gate off material attribution that would otherwise run against
useless stub text.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class ParsedFiling:
    cik: str
    accession_number: str
    form: str
    filed_at: datetime | None
    primary_document: str | None
    primary_document_url: str | None
    company_name: str | None
    ticker: str | None
    narrative_excerpt: str | None
    is_narrative_placeholder: bool = True
    # Per-filing metadata pulled from filings.recent.* arrays (added
    # 2026-05-23 as part of SEC Workstream A).  Lands on
    # SourceDocument.metadata_json downstream so future scoring code can
    # filter by 8-K item code, prioritise XBRL filings for downstream
    # extraction, etc.
    items: list[str] = field(default_factory=list)
    primary_doc_description: str | None = None
    is_xbrl: bool = False
    is_inline_xbrl: bool = False
    size_bytes: int | None = None
    file_number: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


def _recent_filings(submissions: dict[str, Any]) -> dict[str, list[Any]]:
    recent = submissions.get("filings", {}).get("recent", {})
    return recent if isinstance(recent, dict) else {}


def _index_filing(
    submissions: dict[str, Any], recent: dict[str, list[Any]], idx: int
) -> ParsedFiling | None:
    forms = recent.get("form", [])
    acc = recent.get("accessionNumber", [])
    filing_date = recent.get("filingDate", [])
    primary_doc = recent.get("primaryDocument", [])
    if idx >= len(acc):
        return None
    cik = str(submissions.get("cik", "")).zfill(10)
    accession = str(acc[idx])
    form = str(forms[idx]) if idx < len(forms) else ""
    filed_raw = str(filing_date[idx]) if idx < len(filing_date) else None
    filed_at = None
    if filed_raw:
        try:
            # SEC publishes filing dates as ``YYYY-MM-DD``.  Anchor to
            # UTC midnight so downstream recency-decay / event-date
            # comparisons don't mix naive + aware datetimes.
            filed_at = datetime.strptime(filed_raw, "%Y-%m-%d").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            filed_at = None
    primary = str(primary_doc[idx]) if idx < len(primary_doc) else None
    base = f"https://www.sec.gov/Archives/edgar/data/{int(cik.lstrip('0') or 0)}"
    acc_clean = accession.replace("-", "")
    url = None
    if primary:
        url = f"{base}/{acc_clean}/{primary}"

    name = submissions.get("name")
    tickers = submissions.get("tickers") or []
    ticker = tickers[0] if tickers else None

    # Narrative placeholder until Phase 1+ fetches full filing text
    narrative = f"{form} filing for {name or 'issuer'} (CIK {cik})."

    # Per-filing metadata from the parallel filings.recent.* arrays.
    # SEC publishes ``items`` for 8-K/6-K as a comma-separated string like
    # "1.01,2.04,9.01" — split into a list so downstream code can filter
    # by item code without re-parsing.  Other fields are best-effort: any
    # missing array (older submissions, partial responses) falls back to
    # the dataclass default.
    items_raw = recent.get("items", [])
    items_str = str(items_raw[idx]) if idx < len(items_raw) else ""
    items = [s.strip() for s in items_str.split(",") if s.strip()] if items_str else []

    pd_desc_arr = recent.get("primaryDocDescription", [])
    primary_doc_description = (
        str(pd_desc_arr[idx]) if idx < len(pd_desc_arr) and pd_desc_arr[idx] else None
    )

    is_xbrl_arr = recent.get("isXBRL", [])
    is_xbrl = bool(is_xbrl_arr[idx]) if idx < len(is_xbrl_arr) else False

    is_inline_xbrl_arr = recent.get("isInlineXBRL", [])
    is_inline_xbrl = (
        bool(is_inline_xbrl_arr[idx]) if idx < len(is_inline_xbrl_arr) else False
    )

    size_arr = recent.get("size", [])
    size_bytes: int | None = None
    if idx < len(size_arr):
        try:
            size_bytes = int(size_arr[idx])
        except (TypeError, ValueError):
            size_bytes = None

    file_num_arr = recent.get("fileNumber", [])
    file_number = (
        str(file_num_arr[idx]) if idx < len(file_num_arr) and file_num_arr[idx] else None
    )

    return ParsedFiling(
        cik=cik,
        accession_number=accession,
        form=form,
        filed_at=filed_at,
        primary_document=primary,
        primary_document_url=url,
        company_name=name if isinstance(name, str) else None,
        ticker=ticker if isinstance(ticker, str) else None,
        narrative_excerpt=narrative[:2000],
        items=items,
        primary_doc_description=primary_doc_description,
        is_xbrl=is_xbrl,
        is_inline_xbrl=is_inline_xbrl,
        size_bytes=size_bytes,
        file_number=file_number,
        raw={"filing_index": idx},
    )


def parse_sec_filing(submissions_json: dict[str, Any], *, max_filings: int = 12) -> list[ParsedFiling]:
    """Expand recent filings from a SEC `submissions` response into `ParsedFiling` rows."""
    recent = _recent_filings(submissions_json)
    acc = recent.get("accessionNumber", [])
    out: list[ParsedFiling] = []
    for i in range(min(len(acc), max_filings)):
        row = _index_filing(submissions_json, recent, i)
        if row:
            out.append(row)
    return out
