"""
Extract text and tables from IEA (and other) critical minerals PDF reports.

Uses pdfplumber for table and text extraction.  The parser is intentionally
conservative: it returns raw sections and tables for the caller to interpret,
rather than trying to build a universal "understand any PDF" system.

pdfplumber dependency: ``pip install pdfplumber``
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


@dataclass
class PdfReportSection:
    title: str | None
    text: str
    page_start: int | None = None


@dataclass
class PdfReportParseResult:
    """Normalised output for downstream material / supplier resolvers."""

    document_title: str | None
    sections: list[PdfReportSection]
    tables: list[dict[str, Any]]
    metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Heading-detection heuristics
# ---------------------------------------------------------------------------

# Sections we want to isolate in IEA Critical Minerals reports.
# Patterns are matched case-insensitively against line text.
_SECTION_HEADING_PATTERNS: list[str] = [
    r"demand\s+outlook",
    r"supply\s+outlook",
    r"market\s+balance",
    r"supply\s+concentration",
    r"critical\s+mineral",
    r"key\s+findings",
    r"executive\s+summary",
    r"production\s+and\s+supply",
    r"demand\s+scenario",
    r"net\s+zero",
    r"announced\s+pledges",
    r"stated\s+policies",
]
_HEADING_RE = re.compile(
    "|".join(f"(?:{p})" for p in _SECTION_HEADING_PATTERNS),
    re.IGNORECASE,
)

# Numeric patterns for values we want to capture from tables.
_NUMBER_RE = re.compile(r"[-+]?\d[\d,]*(?:\.\d+)?")


def _is_likely_heading(line: str) -> bool:
    """Heuristic: short lines (≤ 80 chars) matching known section keywords."""
    stripped = line.strip()
    return bool(stripped and len(stripped) <= 80 and _HEADING_RE.search(stripped))


def _clean_cell(value: Any) -> str | None:
    """Normalise a table cell to a clean string, None if empty."""
    if value is None:
        return None
    s = str(value).strip()
    return s if s else None


def _table_to_dict_rows(raw_table: list[list[Any]]) -> list[dict[str, str | None]]:
    """
    Convert pdfplumber's raw table (list-of-lists) to list-of-dicts.

    Uses the first non-empty row as the header.  Skips rows where every
    cell is None or empty.
    """
    if not raw_table:
        return []

    # Find first non-empty row to use as header
    header: list[str] | None = None
    data_start = 0
    for i, row in enumerate(raw_table):
        cells = [_clean_cell(c) for c in row]
        non_empty = [c for c in cells if c]
        if non_empty:
            header = [c or f"col_{j}" for j, c in enumerate(cells)]
            data_start = i + 1
            break

    if header is None:
        return []

    rows = []
    for row in raw_table[data_start:]:
        cells = [_clean_cell(c) for c in row]
        if not any(cells):
            continue
        # Pad or truncate to match header length
        while len(cells) < len(header):
            cells.append(None)
        rows.append(dict(zip(header, cells[:len(header)])))

    return rows


# ---------------------------------------------------------------------------
# Public parse function
# ---------------------------------------------------------------------------

def parse_pdf_report(pdf_bytes: bytes, *, filename: str = "report.pdf") -> PdfReportParseResult:
    """
    Parse a PDF report and return structured sections and tables.

    Sections:
        Text is split on heading-like lines (short lines matching known
        section keywords).  Each section captures all text until the next
        heading.  This gives the caller named chunks to search rather than
        one huge string.

    Tables:
        Every table pdfplumber detects is converted to list-of-dicts and
        returned in ``tables``.  The ``_page`` and ``_table_index`` keys
        record provenance so callers can log which page a figure came from.

    Metadata:
        ``page_count``, ``char_count``, and ``has_tables`` are always set.
        ``placeholder`` is False once this function is fully implemented.

    pdfplumber is imported lazily so the rest of the ingestion stack doesn't
    require it unless this function is called.
    """
    try:
        import pdfplumber  # type: ignore[import]
    except ImportError as exc:
        raise ImportError(
            "pdfplumber is required for PDF parsing.  "
            "Install it with: pip install pdfplumber"
        ) from exc

    import io

    sections: list[PdfReportSection] = []
    tables: list[dict[str, Any]] = []
    total_chars = 0

    current_heading: str | None = None
    current_lines: list[str] = []

    def _flush_section() -> None:
        text = "\n".join(current_lines).strip()
        if text:
            sections.append(PdfReportSection(
                title=current_heading,
                text=text,
                page_start=None,  # page tracking omitted for brevity
            ))

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        page_count = len(pdf.pages)

        for page_num, page in enumerate(pdf.pages, start=1):
            # ── Text extraction ──────────────────────────────────────────────
            raw_text = page.extract_text(x_tolerance=3, y_tolerance=3) or ""
            total_chars += len(raw_text)

            for line in raw_text.splitlines():
                if _is_likely_heading(line):
                    _flush_section()
                    current_heading = line.strip()
                    current_lines = []
                else:
                    current_lines.append(line)

            # ── Table extraction ─────────────────────────────────────────────
            raw_tables = page.extract_tables(
                table_settings={
                    "vertical_strategy": "lines_strict",
                    "horizontal_strategy": "lines_strict",
                    "snap_tolerance": 3,
                    "join_tolerance": 3,
                }
            ) or []
            # Fallback to text-based strategy if line-based finds nothing
            if not raw_tables:
                raw_tables = page.extract_tables() or []

            for t_idx, raw_table in enumerate(raw_tables):
                dict_rows = _table_to_dict_rows(raw_table)
                if dict_rows:
                    tables.append({
                        "_page": page_num,
                        "_table_index": t_idx,
                        "rows": dict_rows,
                    })

    # Flush final section
    _flush_section()

    return PdfReportParseResult(
        document_title=filename,
        sections=sections,
        tables=tables,
        metadata={
            "placeholder": False,
            "page_count": page_count,
            "char_count": total_chars,
            "has_tables": len(tables) > 0,
            "section_count": len(sections),
        },
    )
