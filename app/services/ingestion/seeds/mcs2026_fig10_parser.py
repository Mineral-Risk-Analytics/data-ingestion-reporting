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

Encoding: ``utf-8-sig`` to strip Fig 10's UTF-8 BOM.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Value parsing
# ---------------------------------------------------------------------------

def _parse_pct(value: str) -> Optional[float]:
    """Parse a Fig 10 percent cell.  Returns a signed fraction (e.g.
    ``"-24"`` → ``-0.24``).  Returns None for blank / unparseable cells.

    Fig 10 publishes whole-number percents (no decimals); the signed
    fraction representation matches how production_yoy_pct is stored.
    """
    if value is None:
        return None
    v = value.strip()
    if not v:
        return None
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
    """
    filepath = Path(filepath)
    # utf-8-sig strips the UTF-8 BOM that Fig 10 ships with.
    with open(filepath, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))

    records: list[dict] = []
    for r in rows:
        # Strip whitespace from the keying field — Fig 10 occasionally
        # ships rows with trailing spaces ("Nickel ", "Platinum ",
        # "Tungsten, concentrate ").  The alias resolver normalises with
        # btrim+lower on lookup, so storing the trimmed form here is
        # safe and avoids subtle dedupe issues.
        raw_name = (r.get("critical_mineral_priced") or "").strip()
        if not raw_name:
            continue

        raw_pch = (r.get("PCH_2024_2025") or "").strip()
        raw_cagr = (r.get("CAGR_2021_2025") or "").strip()
        yoy = _parse_pct(raw_pch)
        cagr = _parse_pct(raw_cagr)

        # Skip rows where both metrics are missing — they carry no signal.
        if yoy is None and cagr is None:
            continue

        records.append({
            "source_system":      "fig10_prices",
            "source_name":        raw_name,
            "price_yoy_pct":      yoy,
            "price_cagr_5yr_pct": cagr,
            "raw_pch":            raw_pch,
            "raw_cagr":           raw_cagr,
        })

    return records


__all__ = ["parse_mcs2026_fig10_csv"]
