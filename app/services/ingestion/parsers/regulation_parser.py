"""
Parse Federal Register API document objects into normalized regulation fields.

Phase 1: maps API fields into internal `ParsedRegulation` for DB `regulations` + `source_documents`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any


@dataclass
class ParsedRegulation:
    external_id: str
    title: str | None
    abstract_text: str | None
    html_url: str | None
    pdf_url: str | None
    # Direct URL to the document's full plain-text body (Federal Register
    # exposes this as `raw_text_url`).  Kept separate from `pdf_url`
    # because the FR ingester's Haiku extraction path needs a text-not-
    # PDF endpoint; collapsing the two would either force a PDF download
    # or skip the extraction entirely.  Added 2026-05-12 as part of the
    # Phase 2 per-query attribution rewrite.
    raw_text_url: str | None
    document_number: str | None
    publication_date: date | None
    effective_date: date | None
    agencies: list[str]
    doc_type: str | None
    topics: list[str]
    raw_metadata: dict[str, Any] = field(default_factory=dict)


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def parse_federal_register_document(doc: dict[str, Any]) -> ParsedRegulation:
    agencies = [a.get("name", "") for a in doc.get("agencies", []) if isinstance(a, dict)]
    topics = []
    for t in doc.get("topics", []) or []:
        if isinstance(t, dict) and t.get("name"):
            topics.append(str(t["name"]))
        elif isinstance(t, str):
            topics.append(t)

    html_url = None
    if doc.get("html_url"):
        html_url = str(doc["html_url"])
    elif doc.get("document_number"):
        html_url = f"https://www.federalregister.gov/d/{doc['document_number']}"

    pdf_url = None
    if isinstance(doc.get("raw_text_url"), str):
        pdf_url = doc["raw_text_url"]
    if doc.get("pdf_url"):
        pdf_url = str(doc["pdf_url"])

    # Separate field: only the literal raw_text_url, not the pdf fallback.
    # The FR ingester's Haiku extraction path needs a text endpoint —
    # downloading a PDF here would either silently fail Haiku scoring or
    # waste bandwidth.  When the API doesn't return raw_text_url (rare —
    # very-recently-published docs), this stays None and the caller falls
    # back to title+abstract.
    raw_text_url = doc.get("raw_text_url") if isinstance(doc.get("raw_text_url"), str) else None

    abstract = doc.get("abstract") or doc.get("abstract_text")
    body = None
    if doc.get("body_html_url"):
        body = str(doc["body_html_url"])

    return ParsedRegulation(
        external_id=str(doc.get("document_number") or doc.get("id") or ""),
        title=doc.get("title"),
        abstract_text=abstract if isinstance(abstract, str) else None,
        html_url=html_url,
        pdf_url=pdf_url,
        raw_text_url=raw_text_url,
        document_number=doc.get("document_number"),
        publication_date=_parse_date(doc.get("publication_date")),
        effective_date=_parse_date(doc.get("effective_on")),
        agencies=[a for a in agencies if a],
        doc_type=doc.get("type"),
        topics=topics,
        raw_metadata={
            "signing_date": doc.get("signing_date"),
            "body_html_url": body,
            "docket_ids": doc.get("docket_ids"),
            "citation": doc.get("citation"),
        },
    )
