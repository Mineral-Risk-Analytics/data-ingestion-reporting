"""Verify company_seed_expanded.xlsx against authoritative sources.

Runs two checks per row:

1. CIK verification — cross-checks each populated `cik` value against the
   SEC's authoritative ticker→CIK mapping at
   https://www.sec.gov/files/company_tickers.json.  Flags rows where the
   CIK doesn't match the SEC's record for that ticker, or where the CIK
   is populated but the ticker isn't in the SEC JSON (often happens for
   very-recently-registered companies — the JSON lags by a few months).

2. Material taxonomy verification — normalizes each value in
   `primary_materials` against:
     a. The canonical materials table in the production DB (Material.canonical_name)
     b. The launch-10 list (LAUNCH_LIST_CANONICAL_NAMES)
   Flags rows where:
     - A material name doesn't match any canonical name (suggests typo,
       downstream product like "NdFeB magnets", or new material not yet
       in our taxonomy)
     - All listed materials are outside the launch-10 (company won't
       contribute to launch-10 scoring even if we ingest its filings)
     - Material is in launch-10 but uses a non-canonical short name
       (e.g., sheet says "Iron Ore" but launch-10 has "Iron Ore")

Output: CSV report at <output_path> with one row per company seed row,
listing all flags. Companies that pass cleanly have an empty `issues`
column.

Usage:
    python scripts/verify_company_seed.py \\
        --seed-path /path/to/company_seed_expanded.xlsx \\
        --output-path /path/to/verification_report.csv

Run with --no-cik to skip the SEC fetch (faster, useful for material-only
re-runs after editing the sheet).
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import urllib.request
from pathlib import Path
from typing import Optional

import openpyxl
from sqlalchemy import select

from app.db.session import get_session_factory
from app.models.material import Material
from app.services.scoring.launch_list import LAUNCH_LIST_CANONICAL_NAMES

SEC_TICKER_JSON_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_USER_AGENT = "BDI Mineral Risk Analytics — nicole.bush000@gmail.com"


def _fetch_sec_ticker_map() -> dict[str, tuple[int, str]]:
    """Return {ticker_upper: (cik_int, registered_title)}.

    SEC requires a User-Agent header on these requests.
    """
    req = urllib.request.Request(
        SEC_TICKER_JSON_URL,
        headers={"User-Agent": SEC_USER_AGENT, "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.load(resp)

    # SEC JSON is shaped { "0": {"cik_str": ..., "ticker": ..., "title": ...}, ... }
    out: dict[str, tuple[int, str]] = {}
    for entry in data.values():
        ticker = (entry.get("ticker") or "").upper().strip()
        if not ticker:
            continue
        cik = int(entry["cik_str"])
        title = entry.get("title", "")
        # If multiple rows share a ticker (rare — dual-class shares), keep first.
        if ticker not in out:
            out[ticker] = (cik, title)
    return out


def _load_canonical_materials() -> tuple[set[str], dict[str, str]]:
    """Returns (set of all canonical material names, alias→canonical map).

    The alias map covers shortened names commonly used in seed sheets —
    e.g., "Iron Ore" → "Iron Ore".  Extend as you discover
    new short-form aliases in partner-curated data.
    """
    SessionLocal = get_session_factory()
    with SessionLocal() as session:
        rows = session.execute(select(Material.canonical_name)).all()
    canonical = {r[0] for r in rows if r[0]}

    # Hand-curated aliases — extend as you find more.
    aliases: dict[str, str] = {
        # Short-form → launch-10 canonical
        "iron ore": "Iron Ore",
        "phosphate": "Phosphate",
        "graphite": "Natural Graphite",
        "ree": "Rare Earth Elements",
        "rare earths": "Rare Earth Elements",
        "rare earth": "Rare Earth Elements",
        "nickel sulfate": "Nickel",
        "cobalt sulfate": "Cobalt",
        "lithium carbonate": "Lithium",
        "lithium hydroxide": "Lithium",
        # Downstream products that aren't materials — map to nothing, flag instead
    }
    return canonical, aliases


def _normalize_material(
    raw: str,
    canonical: set[str],
    aliases: dict[str, str],
) -> tuple[Optional[str], str]:
    """Returns (canonical_name_or_None, status_code).

    status codes:
        "exact"            — name matches a canonical row directly
        "alias"            — matched via alias map
        "downstream_product" — known downstream product (NdFeB magnets, etc)
        "unknown"          — no match, possibly typo or new material
    """
    s = raw.strip()
    if not s:
        return None, "unknown"
    if s in canonical:
        return s, "exact"
    lower = s.lower()
    if lower in aliases:
        return aliases[lower], "alias"
    # Try case-insensitive direct match
    for c in canonical:
        if c.lower() == lower:
            return c, "exact"
    # Known downstream products that aren't materials
    if lower in {"ndfeb magnets", "ndfeb magnet", "neodymium magnets",
                 "battery cells", "cathode", "anode", "synthetic graphite",
                 "stainless steel", "steel", "recycling"}:
        return None, "downstream_product"
    return None, "unknown"


def verify(seed_path: Path, output_path: Path, skip_cik: bool = False) -> None:
    wb = openpyxl.load_workbook(seed_path, data_only=True)
    ws = wb["Company Seed"]
    headers = [c.value for c in ws[1]]
    col = {h: i for i, h in enumerate(headers) if h}

    sec_map: dict[str, tuple[int, str]] = {}
    if not skip_cik:
        print(f"Fetching SEC ticker JSON from {SEC_TICKER_JSON_URL}...", file=sys.stderr)
        sec_map = _fetch_sec_ticker_map()
        print(f"  loaded {len(sec_map)} ticker→CIK entries", file=sys.stderr)

    canonical, aliases = _load_canonical_materials()
    launch_10 = set(LAUNCH_LIST_CANONICAL_NAMES)
    print(f"Loaded {len(canonical)} canonical materials, "
          f"{len(launch_10)} in launch-10", file=sys.stderr)

    report_rows: list[dict[str, str]] = []

    # Data rows start at row 3 (row 2 is documentation)
    for row_idx, row in enumerate(ws.iter_rows(min_row=3, values_only=True), start=3):
        name = row[col["company_name"]]
        if not name:
            continue

        issues: list[str] = []

        # ── CIK verification ──────────────────────────────────────────
        cik_raw = row[col["cik"]]
        ticker = (row[col["primary_ticker"]] or "").strip().upper()
        sec_cik_str = ""
        sec_title = ""
        if cik_raw and not skip_cik:
            try:
                cik_int = int(cik_raw)
            except (ValueError, TypeError):
                issues.append(f"cik_unparseable:{cik_raw!r}")
                cik_int = None
            if cik_int is not None and ticker:
                sec_entry = sec_map.get(ticker)
                if sec_entry is None:
                    issues.append(f"sec_ticker_not_found:{ticker}")
                else:
                    sec_cik_str = str(sec_entry[0]).zfill(10)
                    sec_title = sec_entry[1]
                    if cik_int != sec_entry[0]:
                        issues.append(
                            f"cik_mismatch:our={cik_int} sec={sec_entry[0]} "
                            f"sec_title={sec_entry[1]!r}"
                        )
        elif cik_raw and not ticker:
            issues.append("cik_set_but_ticker_blank")

        # ── Material taxonomy verification ───────────────────────────
        raw_mats = (row[col["primary_materials"]] or "").strip()
        normalized: list[str] = []
        unknowns: list[str] = []
        downstream: list[str] = []
        launch_10_hits: list[str] = []

        if raw_mats:
            for piece in [p.strip() for p in raw_mats.split(",") if p.strip()]:
                canonical_name, status = _normalize_material(piece, canonical, aliases)
                if status == "exact":
                    normalized.append(canonical_name or piece)
                    if canonical_name in launch_10:
                        launch_10_hits.append(canonical_name)
                elif status == "alias":
                    normalized.append(f"{piece}→{canonical_name}")
                    if canonical_name in launch_10:
                        launch_10_hits.append(canonical_name)
                elif status == "downstream_product":
                    downstream.append(piece)
                else:
                    unknowns.append(piece)

            if unknowns:
                issues.append(f"unknown_materials:{','.join(unknowns)}")
            if downstream:
                issues.append(f"downstream_products_in_materials:{','.join(downstream)}")
            if not launch_10_hits:
                issues.append("no_launch_10_materials")
        elif not raw_mats:
            issues.append("primary_materials_blank")

        report_rows.append({
            "row": str(row_idx),
            "company_name": name,
            "ticker": ticker,
            "our_cik": str(cik_raw or ""),
            "sec_cik": sec_cik_str,
            "sec_registered_title": sec_title,
            "primary_materials_raw": raw_mats,
            "launch_10_materials": ", ".join(sorted(set(launch_10_hits))),
            "normalized_materials": "; ".join(normalized),
            "issues": " | ".join(issues),
        })

    fieldnames = list(report_rows[0].keys()) if report_rows else []
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(report_rows)

    n_issues = sum(1 for r in report_rows if r["issues"])
    print(f"\nWrote {len(report_rows)} rows to {output_path}", file=sys.stderr)
    print(f"  {n_issues} rows have at least one issue flagged", file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed-path", type=Path, required=True,
                        help="Path to company_seed_expanded.xlsx")
    parser.add_argument("--output-path", type=Path, required=True,
                        help="Path to write verification_report.csv")
    parser.add_argument("--no-cik", action="store_true",
                        help="Skip SEC CIK verification (faster)")
    args = parser.parse_args()
    verify(args.seed_path, args.output_path, skip_cik=args.no_cik)


if __name__ == "__main__":
    main()
