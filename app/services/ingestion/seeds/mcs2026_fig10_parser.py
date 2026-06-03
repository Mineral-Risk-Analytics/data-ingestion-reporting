"""Parser for the USGS MCS 2026 Fig 10 — Price Growth Rates supplementary CSV.

Phase B refactor (May 2026): this parser is now session-free and
delegates all canonical-name resolution to ``material_source_aliases``
via ``material_resolver``.  The CLI receives raw records keyed by
``source_name`` (the verbatim Fig 10 commodity string), resolves them
against the alias table, then aggregates rows that share a canonical.

Output contract
---------------
Returns a list of *raw* records, one per Fig 10 CSV row, in the shape::

    {
      "source_system": "fig10_prices",
      "source_name":   str,    # verbatim "Aluminum, bauxite", "Lithium, battery-grade lithium carbonate", etc.
      "price_yoy_pct":      float | None,   # signed fraction (e.g. -0.24 for -24%)
      "price_cagr_5yr_pct": float | None,   # signed fraction CAGR 2021–2025
      "raw_pch":  str,         # original cell value, for audit
      "raw_cagr": str,
    }

Aggregation across multiple rows mapping to the same canonical
(e.g. ``"Fluorspar, acid grade"`` + ``"Fluorspar, metallurgical grade"``
both → ``"Fluorspar"``) happens in the CLI after resolution.

Encoding
--------
The MCS 2026 Fig 10 file is published as UTF-8 with BOM (``utf-8-sig``
strips the BOM cleanly).  The parser falls back to ``cp1252`` on
``UnicodeDecodeError`` for consistency with the main commodity CSV
parser (see ``mcs2026_parser.parse_mcs2026_csv`` for the same pattern).
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Optional

import structlog

# Reuse the no-data sentinel set from the main parser so a future Fig 10
# file using USGS sentinels (W, NA, em-dash, etc.) gets the same explicit
# treatment as the commodity-data CSV.  Issue 9.1 fix (2026-05-31).
from app.services.ingestion.seeds.mcs2026_parser import _NO_DATA_SENTINELS

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Value parsing
# ---------------------------------------------------------------------------

def _parse_pct(value: str) -> Optional[float]:
    """Parse a Fig 10 percent cell.  Returns a signed fraction (e.g.
    ``"-24"`` → ``-0.24``).  Returns ``None`` for blank / unparseable /
    sentinel cells.

    Fig 10 publishes whole-number percents (no decimals); the signed
    fraction representation matches how production_yoy_pct is stored.

    Handles:
      * blank / None                                  → None
      * USGS no-data sentinels (W, NA, E, s, XX,
        em-dash, en-dash, hyphen)                     → None
      * bounded estimates ``">95"`` / ``"<10"``       → bound value as
        signed fraction (direction-lossy, matching ``_parse_value`` in
        the main parser).  Defensive; not observed in Fig 10 today.

    Note: a bare ``"-"`` is treated as a no-data sentinel, NOT a negative
    sign — that comes from the sentinel set.  Negative numbers like
    ``"-24"`` parse correctly because float() handles the leading minus
    while the sentinel check requires exact membership match.
    """
    if value is None:
        return None
    v = value.strip()
    if not v or v in _NO_DATA_SENTINELS:
        return None
    # Strip bounded-estimate prefixes — direction-lossy but recovers the
    # coarse signal value.  Real Fig 10 data doesn't use these today;
    # included for parity with the main parser's _parse_value.
    if v.startswith(">") or v.startswith("<"):
        v = v[1:].strip()
    try:
        return float(v) / 100.0
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def parse_mcs2026_fig10_csv(filepath: str | Path) -> list[dict]:
    """Parse Fig 10 price growth CSV and return raw price records.

    See module docstring for the output contract.  This function does
    NOT resolve commodity names to canonical materials — that's the
    CLI's job via ``material_resolver``.  Skipping (e.g. Iridium,
    Asbestos) and aggregation (Fluorspar grades, REE oxides) also
    happen downstream of resolution.

    Encoding: tries ``utf-8-sig`` first (the format USGS publishes
    today, BOM stripped), falls back to ``cp1252`` on ``UnicodeDecodeError``
    to future-proof against a publication format switch.  Issue 9.6
    fix (2026-05-31).
    """
    filepath = Path(filepath)

    # Issue 9.6 (2026-05-31): try utf-8-sig first, cp1252 fallback.
    try:
        with open(filepath, encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
    except UnicodeDecodeError:
        log.debug("fig10_parser.encoding_fallback_cp1252", path=str(filepath))
        with open(filepath, encoding="cp1252") as f:
            rows = list(csv.DictReader(f))

    records: list[dict] = []
    skipped_no_signal = 0
    for r in rows:
        # Issue 9.2 (2026-05-31): strip whitespace as defensive
        # normalization.  Earlier MCS editions had occasional trailing-
        # space rows; the current 2026 file has none.  The alias resolver
        # normalizes via btrim+lower on lookup so storing the trimmed
        # form here avoids subtle dedupe issues regardless.
        raw_name = (r.get("critical_mineral_priced") or "").strip()
        if not raw_name:
            continue

        raw_pch = (r.get("PCH_2024_2025") or "").strip()
        raw_cagr = (r.get("CAGR_2021_2025") or "").strip()
        yoy = _parse_pct(raw_pch)
        cagr = _parse_pct(raw_cagr)

        # Skip rows where both metrics are missing — they carry no signal.
        # Issue 9.5 (2026-05-31): log the skip for observability.
        if yoy is None and cagr is None:
            skipped_no_signal += 1
            log.debug(
                "fig10_parser.row_skipped_no_signal",
                source_name=raw_name,
                raw_pch=raw_pch,
                raw_cagr=raw_cagr,
            )
            continue

        records.append({
            "source_system":      "fig10_prices",
            "source_name":        raw_name,
            "price_yoy_pct":      yoy,
            "price_cagr_5yr_pct": cagr,
            "raw_pch":            raw_pch,
            "raw_cagr":           raw_cagr,
        })

    log.info(
        "fig10_parser.run_summary",
        records_emitted=len(records),
        rows_skipped_no_signal=skipped_no_signal,
    )
    return records


__all__ = ["parse_mcs2026_fig10_csv"]
