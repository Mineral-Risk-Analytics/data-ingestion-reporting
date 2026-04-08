"""
Parse SEC EDGAR submission / filing metadata into internal structures.

Phase 1: works off `submissions` JSON (`filings.recent` arrays and company metadata).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
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
            filed_at = datetime.strptime(filed_raw, "%Y-%m-%d")
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
