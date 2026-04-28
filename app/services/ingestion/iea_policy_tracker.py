"""IEA Critical Minerals Policy Tracker ingestion.

Parses the downloadable export from the IEA Critical Minerals Policy Tracker
(https://www.iea.org/data-and-statistics/data-tools/critical-minerals-policy-tracker).

IEA provides this as a CSV download.  Pass the file path via --file-path
or the IEA_POLICY_TRACKER_PATH env var.  Both .csv and .xlsx are supported —
format is auto-detected from the file extension.

IEA updates the tracker periodically; re-running this command is safe —
duplicates are suppressed by content_hash.

CSV column layout (as of 2026-04 export):
  title, description, status, year, jurisdiction, source,
  datePromulgated, yearEnded, learnMore, learnMoreLanguage,
  dateModified, countries, states, technologies, tags, policyType

  - countries   JSON array: [{"iso3": "EUR", "name": "European Union"}, ...]
  - technologies JSON array of strings:  ["Battery technologies", ...]
  - tags         JSON array of objects:  [{"topic": "Critical Minerals",
                                           "family": "Economic",
                                           "name": "Recycling support",
                                           "category": null}, ...]
  - policyType   Same structure as tags
  - description  HTML content — stripped to plain text on ingest

Positive-policy context
-----------------------
The Policy Tracker covers the CONSTRUCTIVE side of minerals policy:
investment pledges, domestic-content milestones, CRMA/IRA implementation
progress.  GTA covers harmful trade interventions; this source covers
enabling policies.

Events are ingested at LOW severity (0.10–0.20) because these are positive
developments.  The scoring engine applies them as weak regulatory or
geopolitical signals; their main value is:
  (a) rationale-layer context explaining why a geography scores lower than
      raw supply-concentration data alone would suggest,
  (b) future-proofing for a negative-signal adjustment to the scoring engine.

DB output
---------
  RiskEvent             — event_type = POLICY_MILESTONE | INVESTMENT_PLEDGE
  RiskEventGeography    — implementing country (ISO2), one row per country
  RiskEventMaterial     — materials covered by the policy (best-effort)
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.documents import SourceDocument
from app.models.regulatory import (
    RiskEvent,
    RiskEventGeography,
    RiskEventMaterial,
)
from app.models.supply import Material

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SOURCE_NAME = "IEA Critical Minerals Policy Tracker"
_SOURCE_TYPE = "iea_policy_tracker"

# Default local path — override via IEA_POLICY_TRACKER_PATH env var or CLI.
DEFAULT_FILE_PATH = os.environ.get(
    "IEA_POLICY_TRACKER_PATH",
    "data/iea_policy_tracker.csv",
)

# Severity calibration — positive-policy events are low severity by design.
_SEVERITY_BY_STATUS: dict[str, float] = {
    "implemented":  0.20,
    "in force":     0.20,
    "enacted":      0.20,
    "announced":    0.15,
    "proposed":     0.10,
    "under review": 0.10,
}
_DEFAULT_SEVERITY = 0.15

# Policy family/name keywords → risk category
_CATEGORY_MAP: dict[str, str] = {
    "investment":            "geopolitical_trade",
    "financing":             "geopolitical_trade",
    "innovation funds":      "geopolitical_trade",
    "subsid":                "geopolitical_trade",
    "tax credit":            "geopolitical_trade",
    "trade":                 "geopolitical_trade",
    "procurement":           "geopolitical_trade",
    "strategic":             "geopolitical_trade",
    "recycling":             "regulatory_compliance",
    "standard":              "regulatory_compliance",
    "reporting":             "regulatory_compliance",
    "regulation":            "regulatory_compliance",
    "legal framework":       "regulatory_compliance",
    "milestone":             "regulatory_compliance",
}
_DEFAULT_CATEGORY = "regulatory_compliance"

# ---------------------------------------------------------------------------
# ISO-3 → ISO-2 country code map
# IEA PAMS uses ISO3 codes in the countries JSON array.
# Full list derived from the actual CSV export (96 codes + name fallbacks).
# IEA uses "EUR" for the European Union bloc (not a standard ISO3 code).
# ---------------------------------------------------------------------------

_ISO3_TO_ISO2: dict[str, str] = {
    # Africa
    "AGO": "AO", "BFA": "BF", "CAF": "CF", "CIV": "CI", "CMR": "CM",
    "COD": "CD", "COG": "CG", "ETH": "ET", "GAB": "GA", "GHA": "GH",
    "GIN": "GN", "KEN": "KE", "LBR": "LR", "MDG": "MG", "MLI": "ML",
    "MOZ": "MZ", "MRT": "MR", "MWI": "MW", "NAM": "NA", "NER": "NE",
    "NGA": "NG", "RWA": "RW", "SEN": "SN", "SLE": "SL", "STP": "ST",
    "SUR": "SR", "SYC": "SC", "TCD": "TD", "TGO": "TG", "TZA": "TZ",
    "UGA": "UG", "ZAF": "ZA", "ZMB": "ZM", "ZWE": "ZW", "MAR": "MA",
    "EGY": "EG", "AGO": "AO",
    # Americas
    "ARG": "AR", "BOL": "BO", "BRA": "BR", "CAN": "CA", "CHL": "CL",
    "COL": "CO", "DOM": "DO", "ECU": "EC", "GTM": "GT", "GUY": "GY",
    "HND": "HN", "MEX": "MX", "PAN": "PA", "PER": "PE", "SUR": "SR",
    "TTO": "TT", "USA": "US",
    # Asia-Pacific
    "AFG": "AF", "ARM": "AM", "AUS": "AU", "GRL": "GL", "IDN": "ID",
    "IND": "IN", "IRQ": "IQ", "JPN": "JP", "KAZ": "KZ", "KGZ": "KG",
    "KHM": "KH", "KOR": "KR", "MNG": "MN", "MMR": "MM", "MYS": "MY",
    "NCL": "NC", "NZL": "NZ", "PAK": "PK", "PHL": "PH", "PNG": "PG",
    "QAT": "QA", "SAU": "SA", "THA": "TH", "TJK": "TJ", "TLS": "TL",
    "TUR": "TR", "UKR": "UA", "VNM": "VN",
    # Europe
    "ALB": "AL", "AUT": "AT", "BEL": "BE", "CHE": "CH", "DEU": "DE",
    "DNK": "DK", "ESP": "ES", "EST": "EE", "EUR": "EU",  # IEA bloc code
    "FIN": "FI", "FRA": "FR", "GBR": "GB", "IRL": "IE", "ITA": "IT",
    "NLD": "NL", "NOR": "NO", "POL": "PL", "PRT": "PT", "ROU": "RO",
    "RUS": "RU", "SWE": "SE",
    # Fallback name-based entries for XLSX compat (lowercased)
    "european union": "EU", "eu": "EU",
    "australia": "AU", "brazil": "BR", "canada": "CA", "chile": "CL",
    "china": "CN", "people's republic of china": "CN",
    "democratic republic of the congo": "CD", "drc": "CD",
    "finland": "FI", "france": "FR", "germany": "DE", "india": "IN",
    "indonesia": "ID", "italy": "IT", "japan": "JP", "korea": "KR",
    "republic of korea": "KR", "south korea": "KR", "mexico": "MX",
    "namibia": "NA", "norway": "NO", "peru": "PE", "philippines": "PH",
    "poland": "PL", "russia": "RU", "russian federation": "RU",
    "south africa": "ZA", "spain": "ES", "sweden": "SE",
    "united kingdom": "GB", "uk": "GB", "united states": "US",
    "usa": "US", "united states of america": "US", "zambia": "ZM",
    "zimbabwe": "ZW",
    # CHN is not in the CSV but include for robustness
    "CHN": "CN",
}

# ---------------------------------------------------------------------------
# Mineral keyword map
# Keys are lowercased substrings to search in technology/tag text.
# ---------------------------------------------------------------------------

_MINERAL_CANONICAL: dict[str, str] = {
    "lithium":          "Lithium",
    "cobalt":           "Cobalt",
    "nickel":           "Nickel",
    "graphite":         "Natural Graphite",
    "natural graphite": "Natural Graphite",
    "manganese":        "Manganese",
    "copper":           "Copper",
    "rare earth":       "Rare Earth Elements",
    "ree":              "Rare Earth Elements",
    "aluminium":        "Aluminum",
    "aluminum":         "Aluminum",
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
    "zinc":             "Zinc",
    "tin":              "Tin",
    "tungsten":         "Tungsten",
    "platinum":         "Platinum-Group Metals",
    "pgm":              "Platinum-Group Metals",
    "magnesium":        "Magnesium",
}

# Flush every N inserts to keep the SQLAlchemy unit-of-work compact.
_FLUSH_EVERY = 50

# HTML tag stripper
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")


# ---------------------------------------------------------------------------
# JSON column helpers
# ---------------------------------------------------------------------------

def _parse_json_col(raw: str | None) -> Any:
    """Parse a JSON column value; return [] on failure."""
    if not raw or not raw.strip():
        return []
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return []


def _extract_iso2_codes(countries_raw: str | None) -> list[str]:
    """
    Parse the IEA 'countries' JSON array and return ISO2 codes.

    Input:  '[{"iso3": "EUR", "name": "European Union"}, {"iso3": "USA", ...}]'
    Output: ["EU", "US"]

    Falls back to name-based lookup if iso3 is absent or unrecognised.
    """
    items = _parse_json_col(countries_raw)
    if not isinstance(items, list):
        return []

    codes: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        # Prefer iso3 lookup
        iso3 = (item.get("iso3") or "").strip().upper()
        iso2 = _ISO3_TO_ISO2.get(iso3)
        if iso2 is None:
            # Fallback to name
            name = (item.get("name") or "").strip().lower()
            iso2 = _ISO3_TO_ISO2.get(name)
        if iso2 and iso2 not in codes:
            codes.append(iso2)
        elif not iso2:
            log.debug(
                "iea_policy_tracker.unknown_country",
                iso3=iso3,
                name=item.get("name"),
            )
    return codes


# ---------------------------------------------------------------------------
# Technology category → mineral basket
# IEA uses abstract technology categories; most don't name specific minerals.
# This mapping translates battery/energy tech categories to the minerals they
# cover by implication.  Broad categories (e.g. "Mining technologies") are
# intentionally excluded to avoid false positives.
# ---------------------------------------------------------------------------

_TECH_MINERAL_BASKETS: dict[str, list[str]] = {
    "battery technologies":           ["Lithium", "Cobalt", "Nickel", "Natural Graphite", "Manganese"],
    "battery recycling":              ["Lithium", "Cobalt", "Nickel", "Natural Graphite"],
    "lithium-ion batteries":          ["Lithium", "Cobalt", "Nickel", "Natural Graphite"],
    "other batteries":                ["Lithium", "Vanadium"],
    "battery electric":               ["Lithium", "Cobalt", "Nickel"],
    "energy storage technologies":    ["Lithium", "Vanadium"],
    "solar pv":                       ["Silicon (Anode Grade)", "Gallium"],
    "solar":                          ["Silicon (Anode Grade)"],
    "wind":                           ["Rare Earth Elements", "Copper"],
    "wind offshore":                  ["Rare Earth Elements", "Copper"],
    "fuel cells":                     ["Platinum-Group Metals"],
    "hydrogen electrolysis":          ["Platinum-Group Metals", "Nickel"],
}


def _extract_minerals_from_tech_and_text(
    technologies_raw: str | None,
    title: str,
    description: str,
) -> list[str]:
    """
    Derive canonical mineral names using two complementary strategies:

    1. Technology basket lookup — IEA's abstract tech categories (e.g.
       "Battery technologies") are mapped to the minerals they imply.
    2. Keyword scanning — title and plain-text description are searched for
       explicit mineral names (catches policies that name minerals directly).

    The `tags` column is not used: in the current CSV export it is always
    empty (all entries return no tag names).

    Returns a deduplicated list of canonical mineral names.
    """
    found: list[str] = []

    # Strategy 1: technology basket
    tech_list = _parse_json_col(technologies_raw)
    if isinstance(tech_list, list):
        for tech in tech_list:
            tech_lower = str(tech).lower().strip()
            # Exact match first
            basket = _TECH_MINERAL_BASKETS.get(tech_lower)
            if basket is None:
                # Substring match (handles minor naming variations)
                basket = next(
                    (v for k, v in _TECH_MINERAL_BASKETS.items() if k in tech_lower),
                    None,
                )
            if basket:
                for mineral in basket:
                    if mineral not in found:
                        found.append(mineral)

    # Strategy 2: word-boundary keyword scan of title + description.
    # Word boundaries prevent "tin" matching "containing", "ree" matching
    # "green", etc.  We compile per-call since the function isn't hot.
    search_text = f"{title} {description}".lower()
    for keyword, canonical in _MINERAL_CANONICAL.items():
        if canonical not in found:
            pattern = rf"\b{re.escape(keyword)}\b"
            if re.search(pattern, search_text):
                found.append(canonical)

    return found


def _extract_policy_type_names(policy_type_raw: str | None) -> list[str]:
    """
    Parse policyType JSON array and return a flat list of name strings.

    Input:  '[{"topic":"Critical Minerals","family":"Economic","name":"Financing",...}]'
    Output: ["Financing"]
    """
    items = _parse_json_col(policy_type_raw)
    if not isinstance(items, list):
        return []
    return [
        item["name"]
        for item in items
        if isinstance(item, dict) and item.get("name")
    ]


def _strip_html(html: str | None) -> str:
    """Strip HTML tags and normalise whitespace."""
    if not html:
        return ""
    text = _HTML_TAG_RE.sub(" ", html)
    return _WHITESPACE_RE.sub(" ", text).strip()


# ---------------------------------------------------------------------------
# CSV parsing
# ---------------------------------------------------------------------------

def _parse_csv_rows(rows_iter: Any) -> list[dict[str, Any]]:
    """
    Parse rows from a csv.DictReader using the known IEA PAMS column names.

    Required columns: title, countries
    Optional but used: description, status, year, technologies, tags, policyType
    """
    records: list[dict[str, Any]] = []

    for row in rows_iter:
        title = (row.get("title") or "").strip()
        countries_raw = row.get("countries") or ""

        if not title or not countries_raw:
            continue

        iso2_codes = _extract_iso2_codes(countries_raw)
        # Skip rows with no resolvable country (international org-only entries
        # are still included with empty iso2_codes — logged at ingest time)

        description_clean = _strip_html(row.get("description"))
        minerals = _extract_minerals_from_tech_and_text(
            row.get("technologies"),
            title,
            description_clean,
        )

        policy_type_names = _extract_policy_type_names(row.get("policyType"))

        records.append({
            "title":             title,
            "description":       description_clean,
            "status":            (row.get("status") or "").strip(),
            "year":              (row.get("year") or "").strip(),
            "jurisdiction":      (row.get("jurisdiction") or "").strip(),
            "iso2_codes":        iso2_codes,          # list[str]
            "minerals":          minerals,             # list[str] canonical names
            "policy_type_names": policy_type_names,   # list[str] e.g. ["Financing"]
            "countries_raw":     countries_raw,        # kept for content hash / metadata
        })

    return records


def parse_policy_tracker_file(path: str | Path) -> list[dict[str, Any]]:
    """
    Parse the IEA Policy Tracker export (CSV or XLSX) and return normalised
    row dicts.

    Format is auto-detected from the file extension (.csv → csv module,
    .xlsx / .xls → openpyxl).  IEA currently exports as CSV.

    Raises FileNotFoundError if the path does not exist.
    """
    import csv as csv_module

    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(
            f"IEA Policy Tracker file not found at {path}.  "
            f"Download it from https://www.iea.org/data-and-statistics/data-tools/"
            f"critical-minerals-policy-tracker and set IEA_POLICY_TRACKER_PATH."
        )

    suffix = path.suffix.lower()

    # ── CSV path (primary — current IEA export format) ───────────────────────
    if suffix == ".csv":
        with path.open(newline="", encoding="utf-8-sig") as f:
            reader = csv_module.DictReader(f)
            if reader.fieldnames is None:
                raise ValueError(f"Empty or unreadable CSV: {path}")
            records = _parse_csv_rows(reader)
        log.info("iea_policy_tracker.parsed_csv", path=str(path), row_count=len(records))
        return records

    # ── XLSX path (retained for older exports) ───────────────────────────────
    try:
        import openpyxl  # type: ignore[import]
    except ImportError as exc:
        raise ImportError(
            "openpyxl is required for XLSX parsing.  "
            "Install it with: pip install openpyxl"
        ) from exc

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    _SHEET_PREFERENCES = ["Policies", "Policy Tracker", "Data", "Sheet1"]
    ws = next(
        (wb[name] for name in _SHEET_PREFERENCES if name in wb.sheetnames),
        wb.active,
    )

    rows_iter = ws.iter_rows(values_only=True)

    # Find header row
    headers: list[str] = []
    remaining_rows: list[tuple] = []
    for row in rows_iter:
        non_empty = [c for c in row if c is not None and str(c).strip()]
        if len(non_empty) >= 3 and not headers:
            headers = [str(c).strip() if c is not None else "" for c in row]
        elif headers:
            remaining_rows.append(row)

    if not headers:
        raise ValueError(f"Could not find a header row in {path}")

    # Convert sequence rows to dicts mirroring csv.DictReader output
    def _seq_rows_as_dicts():
        for row in remaining_rows:
            cells = [str(c).strip() if c is not None else "" for c in row]
            while len(cells) < len(headers):
                cells.append("")
            yield dict(zip(headers, cells[:len(headers)]))

    records = _parse_csv_rows(_seq_rows_as_dicts())
    log.info("iea_policy_tracker.parsed_xlsx", path=str(path), row_count=len(records))
    return records


# Keep old name as an alias for backward compatibility
parse_policy_tracker_xlsx = parse_policy_tracker_file


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _build_material_name_map(session: Session) -> dict[str, int]:
    """Return {canonical_name.lower(): material_id} for all materials."""
    rows = session.execute(select(Material.id, Material.canonical_name)).all()
    return {name.lower(): mid for mid, name in rows}


def _resolve_material_ids(
    canonical_names: list[str],
    name_map: dict[str, int],
) -> list[int]:
    """Map canonical mineral names → material IDs, skipping unknowns."""
    ids: list[int] = []
    for name in canonical_names:
        mid = name_map.get(name.lower())
        if mid and mid not in ids:
            ids.append(mid)
    return ids


def _derive_event_type(policy_type_names: list[str]) -> str:
    combined = " ".join(policy_type_names).lower()
    if any(kw in combined for kw in ("invest", "financ", "fund", "grant", "subsid")):
        return "INVESTMENT_PLEDGE"
    return "POLICY_MILESTONE"


def _derive_category(policy_type_names: list[str]) -> str:
    combined = " ".join(policy_type_names).lower()
    for kw, cat in _CATEGORY_MAP.items():
        if kw in combined:
            return cat
    return _DEFAULT_CATEGORY


def _derive_severity(status: str) -> float:
    return _SEVERITY_BY_STATUS.get(status.lower().strip(), _DEFAULT_SEVERITY)


def _content_hash(title: str, countries_raw: str, year: str) -> str:
    raw = f"{title}|{countries_raw}|{year}".encode()
    return hashlib.sha256(raw).hexdigest()[:64]


# ---------------------------------------------------------------------------
# Main ingest function
# ---------------------------------------------------------------------------

def ingest_policy_tracker(
    session: Session,
    xls_path: str | Path | None = None,
    *,
    run_id: str | None = None,
) -> dict[str, Any]:
    """
    Parse the IEA Policy Tracker CSV/XLSX and persist new entries as RiskEvents.

    Args:
        session:   SQLAlchemy session.  Caller owns commit.
        xls_path:  Path to the downloaded file.  Defaults to DEFAULT_FILE_PATH.
        run_id:    Optional identifier for this ingestion run.

    Returns:
        Dict with inserted, skipped (duplicate), and failed counts.
    """
    if xls_path is None:
        xls_path = DEFAULT_FILE_PATH

    records = parse_policy_tracker_file(xls_path)
    material_map = _build_material_name_map(session)

    # Ensure source document exists
    source_doc = session.scalar(
        select(SourceDocument).where(
            SourceDocument.source_type == _SOURCE_TYPE
        ).limit(1)
    )
    if source_doc is None:
        source_doc = SourceDocument(
            title=_SOURCE_NAME,
            source_type=_SOURCE_TYPE,
            source_url="https://www.iea.org/data-and-statistics/data-tools/critical-minerals-policy-tracker",
        )
        session.add(source_doc)
        session.flush()

    inserted = 0
    skipped = 0
    failed = 0

    for rec in records:
        try:
            chash = _content_hash(
                rec["title"], rec["countries_raw"], rec["year"]
            )

            # Deduplication
            existing = session.scalar(
                select(RiskEvent).where(RiskEvent.content_hash == chash).limit(1)
            )
            if existing:
                skipped += 1
                continue

            policy_type_names = rec["policy_type_names"]
            event_type = _derive_event_type(policy_type_names)
            category   = _derive_category(policy_type_names)
            severity   = _derive_severity(rec["status"])

            # Parse year → approximate event date (Jan 1 of that year)
            event_date: Optional[datetime] = None
            try:
                yr_str = str(rec["year"])[:4]
                yr = int(yr_str) if yr_str.isdigit() else None
                if yr:
                    event_date = datetime(yr, 1, 1, tzinfo=timezone.utc)
            except (ValueError, TypeError):
                pass

            # Title format: first country name + policy title, truncated
            country_label = rec["iso2_codes"][0] if rec["iso2_codes"] else rec["jurisdiction"] or "INTL"
            title = f"{country_label} — {rec['title']}"[:1024]
            summary = rec["description"][:500] if rec["description"] else None

            event = RiskEvent(
                source_document_id=source_doc.id,
                event_type=event_type,
                event_date=event_date,
                title=title,
                summary=summary,
                severity_score=severity,
                confidence_score=0.65,
                risk_categories_json=[category],
                geography_json={
                    "primary": rec["iso2_codes"][0] if rec["iso2_codes"] else None,
                    "all_countries": rec["iso2_codes"],
                },
                content_hash=chash,
                verified=False,
                metadata_json={
                    "source": _SOURCE_NAME,
                    "policy_type": policy_type_names,
                    "status": rec["status"],
                    "jurisdiction": rec["jurisdiction"],
                    "minerals_resolved": rec["minerals"],
                    "countries_raw": rec["countries_raw"],
                    "run_id": run_id,
                    "positive_policy": True,
                    "note": (
                        "Low-severity positive-policy event. Contributes minimally to risk "
                        "scores; primary value is rationale context."
                    ),
                },
            )
            session.add(event)
            session.flush()

            # Geography junction — one row per country
            for iso2 in rec["iso2_codes"]:
                session.add(RiskEventGeography(
                    risk_event_id=event.id,
                    country_code=iso2,
                    geography_context="primary",
                    relevance_score=1.0,
                ))

            if not rec["iso2_codes"]:
                log.debug(
                    "iea_policy_tracker.no_iso2_resolved",
                    title=rec["title"],
                    countries_raw=rec["countries_raw"],
                )

            # Material junctions (best-effort, derived from technologies + tags)
            material_ids = _resolve_material_ids(rec["minerals"], material_map)
            for mid in material_ids:
                session.add(RiskEventMaterial(
                    risk_event_id=event.id,
                    material_id=mid,
                    relevance_score=0.85,
                    match_reason="technology_tag_keyword",
                ))

            inserted += 1

            if inserted % _FLUSH_EVERY == 0:
                session.flush()
                log.info(
                    "iea_policy_tracker.progress",
                    inserted=inserted, skipped=skipped, total=len(records),
                )

        except Exception:
            failed += 1
            log.exception(
                "iea_policy_tracker.row_error",
                title=rec.get("title"),
                countries=rec.get("countries_raw"),
            )

    session.flush()
    result = {
        "source": _SOURCE_NAME,
        "total_rows": len(records),
        "inserted": inserted,
        "skipped_duplicate": skipped,
        "failed": failed,
    }
    log.info("iea_policy_tracker.done", **result)
    return result


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    import sys

    from app.db.session import get_session_factory

    parser = argparse.ArgumentParser(
        description="Ingest IEA Critical Minerals Policy Tracker CSV into risk_events."
    )
    parser.add_argument(
        "--file-path",
        dest="xls_path",
        default=DEFAULT_FILE_PATH,
        help=(
            "Path to the downloaded IEA Policy Tracker CSV (or XLSX).  "
            "Download from https://www.iea.org/data-and-statistics/data-tools/"
            "critical-minerals-policy-tracker"
        ),
    )
    parser.add_argument("--run-id", default=None, help="Optional run identifier.")
    args = parser.parse_args()

    SessionFactory = get_session_factory()
    db = SessionFactory()
    try:
        result = ingest_policy_tracker(db, xls_path=args.xls_path, run_id=args.run_id)
        db.commit()
        print(result)
    except Exception as exc:
        db.rollback()
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    finally:
        db.close()
