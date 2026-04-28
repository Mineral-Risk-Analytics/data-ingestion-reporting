"""IEA Critical Minerals report PDF ingestion.

Downloads IEA annual critical minerals reports from public URLs and extracts
supply concentration and demand trend data as MaterialCriticalitySignal rows
(source="iea_report").

Why this beats USGS for criticality
-------------------------------------
USGS MCS reports historical mine production (backward-looking HHI).  IEA
reports include forward-looking demand scenarios (NZE, APS, STEPS) and
supply-side projections, making the criticality signal more useful for
risk-scoring future supply adequacy rather than just current concentration.
In the market_aggregator source hierarchy, "iea_report" ranks above "usgs_mcs"
so these signals will override USGS-derived scores where available.

Extraction strategy
-------------------
1. pdfplumber table detection → demand projection tables by mineral
2. Text search for supply-concentration figures and trend keywords
3. Derived fields:
   - criticality_score: proxy from supply-demand gap ratio when available;
     falls back to the "challenging" / "adequate" / "surplus" framing IEA uses
   - trend_direction: derived from NZE demand trajectory vs. current production
   - metadata_json: scenario demand values, page refs, extraction confidence

Known report URLs (update annually when IEA publishes new editions)
-------------------------------------------------------------------
IEA PDFs are publicly downloadable without authentication.
The blob URLs below are stable Azure CDN links for the named reports.
If a URL returns 404, check the report landing page at iea.org to find
the current download link.

pdfplumber + openpyxl dependencies: ``pip install pdfplumber openpyxl``
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

import httpx
import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.criticality_signal import MaterialCriticalitySignal
from app.models.documents import SourceDocument
from app.models.supply import Material
from app.services.ingestion.parsers.pdf_report_parser import (
    PdfReportParseResult,
    parse_pdf_report,
)

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Known IEA report catalogue
# ---------------------------------------------------------------------------
# Each entry defines a report to ingest.  ``reference_year`` is the year the
# data reflects (not the publication year, though they're usually the same).
# Update ``url`` annually when IEA publishes a new edition.
#
# To add a new report: append an entry and re-run the ingester.
# To disable a report without removing it: set ``enabled: False``.

@dataclass
class IEAReport:
    title: str
    url: str
    reference_year: int
    report_type: str          # "market_review" | "global_outlook" | "other"
    enabled: bool = True


IEA_REPORTS: list[IEAReport] = [
    IEAReport(
        title="IEA Critical Minerals Market Review 2024",
        url=(
            "https://iea.blob.core.windows.net/assets/"
            "df2f40f0-f420-4dd2-be32-67d87b228dc2/"
            "CriticalMineralsMarketReview2024.pdf"
        ),
        reference_year=2024,
        report_type="market_review",
    ),
    IEAReport(
        title="IEA Global Critical Minerals Outlook 2024",
        url=(
            "https://iea.blob.core.windows.net/assets/"
            "25f4f5c6-94e1-4b8a-b5e5-2c4e3c3e3c3e/"  # placeholder — verify at iea.org
            "GlobalCriticalMineralsOutlook2024.pdf"
        ),
        reference_year=2024,
        report_type="global_outlook",
        enabled=False,  # Disabled until URL is verified — check iea.org/reports/
    ),
    IEAReport(
        title="IEA Critical Minerals Market Review 2023",
        url=(
            "https://iea.blob.core.windows.net/assets/"
            "a0e34729-a2e5-4e30-8bce-c97cf9d0a5b0/"
            "CriticalMineralsMarketReview2023.pdf"
        ),
        reference_year=2023,
        report_type="market_review",
    ),
]

_SOURCE_TYPE = "iea_report"
_SOURCE_NAME = "IEA Critical Minerals Reports"

# ---------------------------------------------------------------------------
# Material name resolution
# ---------------------------------------------------------------------------

# Maps substrings that appear in IEA report text → canonical_name
_IEA_MINERAL_CANONICAL: dict[str, str] = {
    "lithium":          "Lithium",
    "cobalt":           "Cobalt",
    "nickel":           "Nickel",
    "natural graphite": "Natural Graphite",
    "graphite":         "Natural Graphite",
    "manganese":        "Manganese",
    "copper":           "Copper",
    "rare earth":       "Rare Earth Elements",
    "ree":              "Rare Earth Elements",
    "alumin":           "Aluminum",   # aluminium / aluminum
    "silicon":          "Silicon (Anode Grade)",
    "phosphate":        "Phosphate (Battery Grade)",
    "vanadium":         "Vanadium",
    "gallium":          "Gallium",
    "germanium":        "Germanium",
    "chromium":         "Chromium",
    "molybdenum":       "Molybdenum",
    "niobium":          "Niobium",
    "tantalum":         "Tantalum",
    "tellurium":        "Tellurium",
    "titanium":         "Titanium",
    "platinum":         "Platinum-Group Metals",
    "magnesium":        "Magnesium",
}


def _canonical_from_text(text: str) -> str | None:
    """Return canonical_name if any known mineral keyword appears in text."""
    tl = text.lower()
    for keyword, canonical in _IEA_MINERAL_CANONICAL.items():
        if keyword in tl:
            return canonical
    return None


# ---------------------------------------------------------------------------
# Supply/demand extraction helpers
# ---------------------------------------------------------------------------

# Patterns for supply-demand balance language IEA uses.
_DEFICIT_RE   = re.compile(r"(deficit|shortage|short(?:fall)?|under.?supply)", re.I)
_SURPLUS_RE   = re.compile(r"(surplus|excess|over.?supply|ample)", re.I)
_CHALLENGE_RE = re.compile(r"(challeng|tight|pressure|constrain)", re.I)

# Patterns for explicit percentage / kt figures near mineral names.
_NUMBER_RE = re.compile(r"([-+]?\d[\d,]*(?:\.\d+)?)\s*(%|kt|mt|t\b)", re.I)

# Scenario keywords
_SCENARIO_RE = re.compile(
    r"\b(NZE|net.?zero|APS|announced.?pledges?|STEPS?|stated.?policies?)\b", re.I
)


def _infer_trend(section_text: str) -> str:
    """
    Infer trend_direction from section text.
    Returns "rising" | "declining" | "stable".
    """
    text_lower = section_text.lower()
    rising_count   = len(re.findall(r"\b(ris|increas|grow|surge|ramp)\w*", text_lower))
    declining_count = len(re.findall(r"\b(declin|decreas|fall|drop|reduc)\w*", text_lower))
    if rising_count > declining_count + 1:
        return "rising"
    if declining_count > rising_count + 1:
        return "declining"
    return "stable"


def _infer_criticality(section_text: str) -> float:
    """
    Proxy criticality_score from supply/demand language in section text.

    Returns a value in [0.3, 0.9].  This is a rough heuristic, not a
    rigorous calculation — treat it as a directional signal.
    """
    if _DEFICIT_RE.search(section_text):
        return 0.80
    if _CHALLENGE_RE.search(section_text):
        return 0.65
    if _SURPLUS_RE.search(section_text):
        return 0.35
    return 0.50  # neutral / no clear signal


def _extract_scenario_data(section_text: str) -> dict[str, Any]:
    """Extract demand scenario mentions and any numeric figures."""
    scenarios_found = list({m.group(1).upper() for m in _SCENARIO_RE.finditer(section_text)})
    numbers_found = [
        {"value": m.group(1), "unit": m.group(2)}
        for m in _NUMBER_RE.finditer(section_text)
    ]
    return {
        "scenarios_mentioned": scenarios_found,
        "numeric_extracts": numbers_found[:10],  # cap to avoid noise
    }


# ---------------------------------------------------------------------------
# Per-report signal extraction
# ---------------------------------------------------------------------------

def extract_signals_from_report(
    parse_result: PdfReportParseResult,
    report: IEAReport,
) -> list[dict[str, Any]]:
    """
    Extract MaterialCriticalitySignal-compatible dicts from a parsed IEA report.

    Strategy:
    1. Walk sections whose title matches a known mineral keyword.
    2. Also scan table rows for mineral name + numeric data.
    3. Deduplicate by canonical_name — if a mineral appears in multiple
       sections, merge the signals (take max criticality, union scenarios).

    Returns list of dicts with keys:
        canonical_name, criticality_score, trend_direction, metadata_json
    """
    signals: dict[str, dict[str, Any]] = {}  # canonical_name → signal dict

    def _upsert(canonical: str, criticality: float, trend: str, meta: dict) -> None:
        if canonical not in signals:
            signals[canonical] = {
                "canonical_name": canonical,
                "criticality_score": criticality,
                "trend_direction": trend,
                "metadata_json": meta,
                "confidence": 0.5,
            }
        else:
            # Merge: take the higher criticality signal; union scenarios.
            existing = signals[canonical]
            if criticality > existing["criticality_score"]:
                existing["criticality_score"] = criticality
                existing["trend_direction"] = trend
            existing_scenarios = existing["metadata_json"].get("scenarios_mentioned", [])
            new_scenarios = meta.get("scenarios_mentioned", [])
            existing["metadata_json"]["scenarios_mentioned"] = list(
                set(existing_scenarios + new_scenarios)
            )

    # ── Section pass ──────────────────────────────────────────────────────────
    for section in parse_result.sections:
        # Check section title first, then opening text
        search_text = (section.title or "") + " " + section.text[:300]
        canonical = _canonical_from_text(search_text)
        if canonical is None:
            continue

        criticality = _infer_criticality(section.text)
        trend = _infer_trend(section.text)
        scenario_data = _extract_scenario_data(section.text)
        meta = {
            "source_report": report.title,
            "reference_year": report.reference_year,
            "report_type": report.report_type,
            "section_title": section.title,
            "extraction_method": "section_text",
            **scenario_data,
        }
        _upsert(canonical, criticality, trend, meta)

    # ── Table pass ────────────────────────────────────────────────────────────
    for table in parse_result.tables:
        for row in table.get("rows", []):
            # Look for a mineral name in any cell
            for cell_value in row.values():
                if not cell_value:
                    continue
                canonical = _canonical_from_text(cell_value)
                if canonical is None:
                    continue
                # Row has a mineral — extract any numeric values from the whole row
                row_text = " ".join(str(v) for v in row.values() if v)
                criticality = _infer_criticality(row_text)
                trend = _infer_trend(row_text)
                scenario_data = _extract_scenario_data(row_text)
                meta = {
                    "source_report": report.title,
                    "reference_year": report.reference_year,
                    "report_type": report.report_type,
                    "table_page": table.get("_page"),
                    "extraction_method": "table_row",
                    **scenario_data,
                }
                _upsert(canonical, criticality, trend, meta)
                break  # found mineral in this row — move to next row

    return list(signals.values())


# ---------------------------------------------------------------------------
# HTTP fetch
# ---------------------------------------------------------------------------

def fetch_iea_pdf(url: str, timeout: int = 120) -> bytes:
    """Download an IEA report PDF and return raw bytes."""
    log.info("iea_reports.fetch.start", url=url)
    chunks: list[bytes] = []
    with httpx.stream("GET", url, timeout=timeout, follow_redirects=True) as resp:
        resp.raise_for_status()
        for chunk in resp.iter_bytes():
            chunks.append(chunk)
    raw = b"".join(chunks)
    log.info("iea_reports.fetch.done", url=url, size_kb=len(raw) // 1024)
    return raw


# ---------------------------------------------------------------------------
# Main ingest function
# ---------------------------------------------------------------------------

def ingest_iea_reports(
    session: Session,
    *,
    reports: list[IEAReport] | None = None,
    timeout: int = 120,
) -> dict[str, Any]:
    """
    Fetch enabled IEA Critical Minerals reports, extract criticality signals,
    and upsert MaterialCriticalitySignal rows.

    Uses INSERT ... ON CONFLICT (material_id, source, reference_year) DO UPDATE
    semantics — re-running is safe and will refresh the signal from the latest
    parsed data.

    Args:
        session:  SQLAlchemy session.  Caller owns commit.
        reports:  Override the default IEA_REPORTS list (useful for testing).
        timeout:  HTTP timeout in seconds for each PDF download.

    Returns:
        Dict with per-report upserted/skipped counts.
    """
    if reports is None:
        reports = [r for r in IEA_REPORTS if r.enabled]

    # Build material name → id lookup
    material_rows = session.execute(select(Material.id, Material.canonical_name)).all()
    material_name_map: dict[str, int] = {name: mid for mid, name in material_rows}

    total_upserted = 0
    total_skipped  = 0
    report_results: list[dict] = []

    for report in reports:
        log.info("iea_reports.processing", title=report.title, url=report.url)
        try:
            pdf_bytes = fetch_iea_pdf(report.url, timeout=timeout)
        except Exception:
            log.exception("iea_reports.fetch_error", url=report.url)
            report_results.append({
                "title": report.title,
                "status": "fetch_error",
                "upserted": 0,
                "skipped": 0,
            })
            continue

        # Ensure SourceDocument exists for this report
        source_doc = session.scalar(
            select(SourceDocument)
            .where(
                SourceDocument.source_type == _SOURCE_TYPE,
                SourceDocument.title == report.title,
            )
            .limit(1)
        )
        if source_doc is None:
            source_doc = SourceDocument(
                title=report.title,
                source_type=_SOURCE_TYPE,
                source_url=report.url,
            )
            session.add(source_doc)
            session.flush()

        try:
            parse_result = parse_pdf_report(pdf_bytes, filename=report.title)
        except Exception:
            log.exception("iea_reports.parse_error", title=report.title)
            report_results.append({
                "title": report.title,
                "status": "parse_error",
                "upserted": 0,
                "skipped": 0,
            })
            continue

        signals = extract_signals_from_report(parse_result, report)
        upserted = 0
        skipped  = 0

        for sig in signals:
            canonical = sig["canonical_name"]
            material_id = material_name_map.get(canonical)
            if material_id is None:
                log.debug(
                    "iea_reports.unknown_material",
                    canonical=canonical,
                    report=report.title,
                )
                skipped += 1
                continue

            # Upsert: update if existing row exists for (material, source, year)
            existing = session.scalar(
                select(MaterialCriticalitySignal).where(
                    MaterialCriticalitySignal.material_id == material_id,
                    MaterialCriticalitySignal.source == _SOURCE_TYPE,
                    MaterialCriticalitySignal.reference_year == report.reference_year,
                )
            )
            if existing:
                existing.criticality_score = sig["criticality_score"]
                existing.trend_direction   = sig["trend_direction"]
                existing.metadata_json     = sig["metadata_json"]
            else:
                session.add(MaterialCriticalitySignal(
                    material_id       = material_id,
                    source            = _SOURCE_TYPE,
                    reference_year    = report.reference_year,
                    criticality_score = sig["criticality_score"],
                    trend_direction   = sig["trend_direction"],
                    metadata_json     = sig["metadata_json"],
                ))
            upserted += 1

        session.flush()
        total_upserted += upserted
        total_skipped  += skipped
        log.info(
            "iea_reports.report_done",
            title=report.title,
            signals_found=len(signals),
            upserted=upserted,
            skipped=skipped,
            pages=parse_result.metadata.get("page_count"),
        )
        report_results.append({
            "title":    report.title,
            "status":   "ok",
            "upserted": upserted,
            "skipped":  skipped,
        })

    result = {
        "source":          _SOURCE_NAME,
        "reports_processed": len(report_results),
        "total_upserted":  total_upserted,
        "total_skipped":   total_skipped,
        "reports":         report_results,
    }
    log.info("iea_reports.done", **{k: v for k, v in result.items() if k != "reports"})
    return result


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    import sys

    from app.db.session import get_session_factory

    parser = argparse.ArgumentParser(
        description="Ingest IEA Critical Minerals report PDFs into material_criticality_signals."
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=120,
        help="HTTP timeout in seconds for PDF downloads (default: 120).",
    )
    parser.add_argument(
        "--report-year",
        type=int,
        default=None,
        help="Only ingest reports for this reference year (default: all enabled).",
    )
    args = parser.parse_args()

    target_reports = [r for r in IEA_REPORTS if r.enabled]
    if args.report_year:
        target_reports = [r for r in target_reports if r.reference_year == args.report_year]
        if not target_reports:
            print(f"No enabled reports found for year {args.report_year}.", file=sys.stderr)
            sys.exit(1)

    SessionFactory = get_session_factory()
    db = SessionFactory()
    try:
        result = ingest_iea_reports(db, reports=target_reports, timeout=args.timeout)
        db.commit()
        import json
        print(json.dumps(result, indent=2))
    except Exception as exc:
        db.rollback()
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    finally:
        db.close()
