"""
Extract text and tables from OEM / NGO PDF reports.

Phase 2+: full implementation with pdfplumber or unstructured.
Phase 1: structured placeholder returning empty sections with explicit TODOs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class PdfReportSection:
    title: str | None
    text: str
    page_start: int | None = None


@dataclass
class PdfReportParseResult:
    """Normalized output for downstream material / supplier resolvers."""

    document_title: str | None
    sections: list[PdfReportSection]
    tables: list[dict[str, Any]]
    metadata: dict[str, Any] = field(default_factory=dict)


def parse_pdf_report(_pdf_bytes: bytes, *, filename: str = "report.pdf") -> PdfReportParseResult:
    # TODO(Phase 2): stream-parse PDF; detect TOC; extract tables for battery chemistry mentions.
    return PdfReportParseResult(
        document_title=filename,
        sections=[],
        tables=[],
        metadata={"placeholder": True, "phase": "2"},
    )
