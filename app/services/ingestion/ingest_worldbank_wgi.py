"""World Bank Worldwide Governance Indicators (WGI) ingester — Step 3.

Populates ``country_governance_signals`` with the percentile rank of each
of the six WGI dimensions per (country × reference_year):

  voice_accountability
  political_stability
  government_effectiveness
  regulatory_quality
  rule_of_law
  control_of_corruption

A ``composite_pct`` is computed as the NULL-safe mean of the six and
stored so the scoring path doesn't recompute it on every read.

Source
------
World Bank Worldwide Governance Indicators, annual.
  https://www.worldbank.org/en/publication/worldwide-governance-indicators

The World Bank changes URLs every January and publishes the data in
multiple shapes (per-dimension CSVs, a single long-format CSV, a wide
Excel workbook with one sheet per dimension).  This ingester accepts
EITHER:

  1. ``url``: an HTTP URL pointing at the bulk Excel file (configurable
     via ``WORLDBANK_WGI_URL`` env var; falls back to the documented
     default below).  Fetched with httpx; parsed with openpyxl.
  2. ``local_path``: a local file path (Excel ``.xlsx`` OR CSV ``.csv``).
     Use this when the URL is broken or you want to vet the data before
     loading.  Detection is by extension.

Input format expected
---------------------
**Long-format CSV** (preferred — simplest, easiest to vet):
  Columns:  country_iso3, country_iso2 (optional), reference_year,
            dimension, percentile_rank
  Where ``dimension`` is one of the six canonical names listed above
  (case-insensitive, underscore-separated).  Missing rows are treated
  as "no data" rather than "0 percentile" — same composite math applies.

**Wide Excel workbook**: six sheets, one per dimension.  Sheet name
  matching is lenient (case-insensitive substring search for each
  dimension).  Each sheet has a "Country Code" / "Country" / per-year
  columns layout.  We look for the latest year column whose header
  parses as a 4-digit int and read the percentile-rank rows.  Defensive
  parsing — variations in year-column placement are tolerated; rows
  without a numeric percentile are skipped.

The Excel path is more fragile (WGI redesigns layouts year over year);
the long-format CSV is the recommended steady-state input.

Re-runs
-------
Idempotent.  Upserts on (country_code, reference_year, source) — second
run with the same data is a no-op.  ``--force`` UPDATEs existing rows
in place.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import Country, CountryGovernanceSignal

log = structlog.get_logger(__name__)


# Default URL — World Bank rotates this annually.  Operator can override
# via ``WORLDBANK_WGI_URL`` env var when the URL changes.  If both this
# URL 404s and no local_path is provided, the ingester fails LOUDLY
# rather than silently picking another source.
WORLDBANK_WGI_DEFAULT_URL: str = (
    "https://www.worldbank.org/content/dam/sites/govindicators/doc/wgidataset.xlsx"
)


# Canonical dimension names — these are also the column suffixes on the
# DB model.  We match incoming column / sheet names against the keys.
_CANONICAL_DIMENSIONS: dict[str, str] = {
    "voice_accountability":       "voice_accountability_pct",
    "political_stability":        "political_stability_pct",
    "government_effectiveness":   "government_effectiveness_pct",
    "regulatory_quality":         "regulatory_quality_pct",
    "rule_of_law":                "rule_of_law_pct",
    "control_of_corruption":      "control_of_corruption_pct",
}


# World Bank DataBank export uses Series Codes like ``GOV_WGI_CC.SC`` for
# the 0-100 percentile score per dimension.  Suffix legend:
#   .EST    estimate in [-2.5, +2.5]
#   .SC     percentile score 0-100  ← what we want
#   .SC_LB  lower bound of 90% confidence interval
#   .SC_UB  upper bound
#   .SE     standard error
#   .SR     number of sources
# Prefix maps to canonical dimension:
_DATABANK_SERIES_PREFIX_TO_DIMENSION: dict[str, str] = {
    "GOV_WGI_VA": "voice_accountability",
    "GOV_WGI_PV": "political_stability",
    "GOV_WGI_GE": "government_effectiveness",
    "GOV_WGI_RQ": "regulatory_quality",
    "GOV_WGI_RL": "rule_of_law",
    "GOV_WGI_CC": "control_of_corruption",
}


# Common name variations the WGI publishes — mapped to canonical names.
# Add more here as the World Bank changes their labels.
_DIMENSION_ALIASES: dict[str, str] = {
    # voice
    "voiceandaccountability":              "voice_accountability",
    "voice_and_accountability":            "voice_accountability",
    "va":                                  "voice_accountability",
    "voice":                               "voice_accountability",
    # political stability
    "politicalstability":                  "political_stability",
    "political_stability":                 "political_stability",
    "politicalstabilitynoviolence":        "political_stability",
    "ps":                                  "political_stability",
    # government effectiveness
    "governmenteffectiveness":             "government_effectiveness",
    "government_effectiveness":            "government_effectiveness",
    "ge":                                  "government_effectiveness",
    # regulatory quality
    "regulatoryquality":                   "regulatory_quality",
    "regulatory_quality":                  "regulatory_quality",
    "rq":                                  "regulatory_quality",
    # rule of law
    "ruleoflaw":                           "rule_of_law",
    "rule_of_law":                         "rule_of_law",
    "rl":                                  "rule_of_law",
    # control of corruption
    "controlofcorruption":                 "control_of_corruption",
    "control_of_corruption":               "control_of_corruption",
    "cc":                                  "control_of_corruption",
}


def _normalise_dimension(raw: str) -> Optional[str]:
    """Map a free-text dimension label to one of the six canonical names.

    Returns ``None`` if no match — caller logs + skips.
    """
    if not raw:
        return None
    key = raw.strip().lower().replace(" ", "").replace("-", "").replace("_", "")
    if key in _CANONICAL_DIMENSIONS:
        return key
    # Try the aliases dict (which also has unspaced keys).
    return _DIMENSION_ALIASES.get(key)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def ingest_worldbank_wgi(
    session: Session,
    *,
    local_path: Optional[str] = None,
    url: Optional[str] = None,
    force: bool = False,
    http_client: Optional[httpx.Client] = None,
) -> dict:
    """Fetch + parse WGI source, upsert into ``country_governance_signals``.

    Args:
        session: SQLAlchemy session.  Caller commits.
        local_path: Optional path to a local ``.xlsx`` or ``.csv``.  Takes
            precedence over ``url``.  Recommended for production runs —
            lets operators vet the file before loading.
        url: Optional URL override.  Defaults to ``settings.worldbank_wgi_url``
            (env var ``WORLDBANK_WGI_URL``) or
            ``WORLDBANK_WGI_DEFAULT_URL``.
        force: If True, UPDATE existing (country, year, source) rows in
            place.  Default False: skip rows that already exist.
        http_client: Optional httpx.Client (for tests).

    Returns:
        Summary dict ``{rows_read, rows_inserted, rows_updated,
        rows_skipped_existing, countries_seen, unknown_dimensions,
        skipped_no_country_match}``.
    """
    summary = {
        "rows_read": 0,
        "rows_inserted": 0,
        "rows_updated": 0,
        "rows_skipped_existing": 0,
        "countries_seen": 0,
        "unknown_dimensions": [],  # for diagnostic logging
        "skipped_no_country_match": 0,
    }

    # ── 1. Acquire raw bytes (file or HTTP) ──────────────────────────────
    raw_bytes, source_kind = _acquire_raw_bytes(
        local_path=local_path, url=url, http_client=http_client,
    )

    # ── 2. Parse to a flat list of (country_iso3, year, dim, pct) ────────
    records = _parse_records(raw_bytes, source_kind=source_kind, summary=summary)
    summary["rows_read"] = len(records)

    if not records:
        log.warning(
            "wgi_ingest.no_records_parsed",
            note="Source file parsed cleanly but yielded zero rows.  "
                 "Check the format — see ingest_worldbank_wgi.py module "
                 "docstring for the expected shape.",
        )
        return summary

    # ── 3. Resolve ISO3 → ISO2 via Country reference table ───────────────
    iso3_to_iso2 = _build_iso3_to_iso2_map(session)

    # ── 4. Aggregate per (country, year) and upsert ──────────────────────
    # Group records so we can compute composite + dimension count per row.
    grouped: dict[tuple[str, int], dict[str, float]] = {}
    for iso3, year, dim_canonical, pct in records:
        iso2 = iso3_to_iso2.get(iso3.upper())
        if iso2 is None:
            summary["skipped_no_country_match"] += 1
            continue
        key = (iso2, year)
        grouped.setdefault(key, {})[dim_canonical] = pct

    summary["countries_seen"] = len({k[0] for k in grouped})

    for (country_code, year), dim_map in grouped.items():
        existing = session.scalar(
            select(CountryGovernanceSignal).where(
                CountryGovernanceSignal.country_code == country_code,
                CountryGovernanceSignal.reference_year == year,
                CountryGovernanceSignal.source == "worldbank_wgi",
            )
        )
        if existing is not None and not force:
            summary["rows_skipped_existing"] += 1
            continue

        # Build the column kwargs from the per-dimension percentile dict.
        col_kwargs: dict = {}
        for canonical, col_name in _CANONICAL_DIMENSIONS.items():
            col_kwargs[col_name] = dim_map.get(canonical)
        # Composite: mean of the non-NULL dimensions.
        present_values = [v for v in dim_map.values() if v is not None]
        composite = sum(present_values) / len(present_values) if present_values else None
        n_present = len(present_values)

        if existing is None:
            session.add(CountryGovernanceSignal(
                country_code=country_code,
                reference_year=year,
                source="worldbank_wgi",
                composite_pct=composite,
                n_dimensions_present=n_present,
                ingested_at=datetime.now(timezone.utc),
                **col_kwargs,
            ))
            summary["rows_inserted"] += 1
        else:
            for col, val in col_kwargs.items():
                setattr(existing, col, val)
            existing.composite_pct = composite
            existing.n_dimensions_present = n_present
            existing.ingested_at = datetime.now(timezone.utc)
            summary["rows_updated"] += 1

    return summary


# ---------------------------------------------------------------------------
# Helpers — file acquisition + parsing
# ---------------------------------------------------------------------------

def _acquire_raw_bytes(
    *,
    local_path: Optional[str],
    url: Optional[str],
    http_client: Optional[httpx.Client],
) -> tuple[bytes, str]:
    """Return (raw_bytes, source_kind) where source_kind ∈ {'xlsx','csv'}."""
    if local_path:
        path = Path(local_path)
        if not path.exists():
            raise FileNotFoundError(f"WGI input not found: {local_path}")
        raw = path.read_bytes()
        kind = "csv" if path.suffix.lower() == ".csv" else "xlsx"
        log.info("wgi_ingest.loaded_local", path=str(path), kind=kind, size=len(raw))
        return raw, kind

    settings = get_settings()
    fetch_url = url or getattr(
        settings, "worldbank_wgi_url", None,
    ) or WORLDBANK_WGI_DEFAULT_URL

    client = http_client or httpx.Client(
        timeout=120.0, follow_redirects=True,
        headers={"User-Agent": "battery-data-intelligence-engine WGI ingester"},
    )
    owns_client = http_client is None
    try:
        resp = client.get(fetch_url)
        resp.raise_for_status()
        raw = resp.content
        log.info(
            "wgi_ingest.fetched_url", url=fetch_url, size=len(raw),
            note="If this is an old URL the World Bank may have moved it; "
                 "set WORLDBANK_WGI_URL env var to override.",
        )
        return raw, "xlsx"
    finally:
        if owns_client:
            client.close()


def _parse_records(
    raw_bytes: bytes,
    *,
    source_kind: str,
    summary: dict,
) -> list[tuple[str, int, str, float]]:
    """Parse to a list of (iso3, year, canonical_dimension, percentile_rank)."""
    if source_kind == "csv":
        return _parse_csv_long(raw_bytes, summary=summary)
    return _parse_xlsx_wide(raw_bytes, summary=summary)


_DATABANK_YEAR_RE = __import__("re").compile(r"(\d{4})\s*\[YR\d{4}\]")


def _parse_csv_long(
    raw_bytes: bytes,
    *,
    summary: dict,
) -> list[tuple[str, int, str, float]]:
    """Route to the right CSV variant — DataBank semi-wide or long format.

    Detection:
      * DataBank export (Q4 2024+): header includes "Country Code" AND
        "Series Code" AND at least one ``YYYY [YRYYYY]`` year column.
      * Otherwise treated as the long format documented in the module
        docstring.
    """
    # World Bank DataBank exports CSV as Windows-1252 (cp1252) — curly
    # quotes, em-dashes and special spaces show up as bytes that UTF-8
    # rejects.  Try UTF-8 first to honour any clean re-export, then fall
    # back to cp1252.  utf-8-sig also strips a BOM if present.
    try:
        text = raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw_bytes.decode("cp1252")
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        return []

    header_lower = [h.strip().lower() for h in reader.fieldnames]
    is_databank = (
        "country code" in header_lower
        and "series code" in header_lower
        and any(_DATABANK_YEAR_RE.search(h) for h in reader.fieldnames)
    )
    if is_databank:
        return _parse_csv_databank(text, summary=summary)
    return _parse_csv_long_format(text, summary=summary)


def _parse_csv_databank(
    text: str,
    *,
    summary: dict,
) -> list[tuple[str, int, str, float]]:
    """Parse the World Bank DataBank export shape.

    Columns:
      Country Name, Country Code, Series Name, Series Code,
      ``YYYY [YRYYYY]`` (one per year — typically a single year)

    We use the ``Series Code`` column (prefix maps to canonical dimension,
    ``.SC`` suffix = the 0-100 percentile score we want) and ignore the
    other metric variants (``.EST``, ``.SE``, ``.SR``, ``.SC_LB``,
    ``.SC_UB``).  ``Series Name`` is not consulted — Code is the stable
    identifier across DataBank versions.

    Country Code is ISO-3 (e.g. 'USA').  The ingester's main loop
    converts to ISO-2 via the ``countries`` reference table.
    """
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        return []

    # Identify the year column(s).
    year_cols: list[tuple[str, int]] = []  # (header_str, year_int)
    for h in reader.fieldnames:
        m = _DATABANK_YEAR_RE.search(h)
        if m:
            year_cols.append((h, int(m.group(1))))

    # Pick the LATEST year column — Step 3 ingests latest only.
    if not year_cols:
        return []
    year_cols.sort(key=lambda t: t[1], reverse=True)
    latest_header, latest_year = year_cols[0]

    out: list[tuple[str, int, str, float]] = []
    unknown_series: set[str] = set()
    for row in reader:
        iso3 = (row.get("Country Code") or "").strip().upper()
        series_code = (row.get("Series Code") or "").strip()
        if not iso3 or not series_code:
            continue
        # Only ``.SC`` (percentile 0-100) is what we ingest.
        if not series_code.endswith(".SC"):
            continue
        prefix = series_code[:-3]  # strip ".SC"
        canonical = _DATABANK_SERIES_PREFIX_TO_DIMENSION.get(prefix)
        if canonical is None:
            if len(unknown_series) < 50:
                unknown_series.add(series_code)
            continue
        raw = (row.get(latest_header) or "").strip()
        if not raw or raw in {"..", "n/a", "N/A"}:
            continue
        try:
            pct = float(raw)
        except (ValueError, TypeError):
            continue
        out.append((iso3, latest_year, canonical, pct))

    summary["unknown_dimensions"] = sorted(unknown_series)
    return out


def _parse_csv_long_format(
    text: str,
    *,
    summary: dict,
) -> list[tuple[str, int, str, float]]:
    """Parse the long-format CSV.

    Required columns (case-insensitive header match):
      country_iso3, reference_year, dimension, percentile_rank

    Optional columns ignored.  Rows with non-numeric percentile_rank are
    silently skipped; rows with unknown dimensions are tallied in the
    summary's ``unknown_dimensions`` list (capped at 50 distinct values
    for log hygiene).
    """
    reader = csv.DictReader(io.StringIO(text))
    # Normalise headers to lowercase + underscore.
    if reader.fieldnames is None:
        return []
    field_map = {h: h.strip().lower().replace(" ", "_") for h in reader.fieldnames}

    out: list[tuple[str, int, str, float]] = []
    unknown_dims: set[str] = set()
    for row in reader:
        # Lookup helpers — tolerate any of the expected header variants.
        def get(name: str) -> Optional[str]:
            for orig, normalised in field_map.items():
                if normalised == name:
                    v = row.get(orig)
                    return v.strip() if isinstance(v, str) else None
            return None

        iso3 = get("country_iso3") or get("iso3") or ""
        year_str = get("reference_year") or get("year") or ""
        dim_raw = get("dimension") or get("indicator") or ""
        pct_str = get("percentile_rank") or get("rank") or get("pct") or ""
        if not iso3 or not year_str or not dim_raw or not pct_str:
            continue
        try:
            year = int(year_str)
            pct = float(pct_str)
        except (ValueError, TypeError):
            continue
        canonical = _normalise_dimension(dim_raw)
        if canonical is None:
            if len(unknown_dims) < 50:
                unknown_dims.add(dim_raw)
            continue
        out.append((iso3.upper(), year, canonical, pct))

    summary["unknown_dimensions"] = sorted(unknown_dims)
    return out


def _parse_xlsx_wide(
    raw_bytes: bytes,
    *,
    summary: dict,
) -> list[tuple[str, int, str, float]]:
    """Parse the World Bank's wide-format XLSX.

    Six sheets are expected, one per dimension.  Sheet name matching is
    lenient (case-insensitive substring) so World Bank tweaks to the
    label don't break ingestion.

    Each sheet's layout (rough — varies slightly by year):
      * 1-2 banner / metadata rows
      * a header row that contains "Country" / "Country Name" + "Code"
        / "Country Code" / "ISO" columns plus YEAR columns
      * sometimes a sub-header row that distinguishes
        Estimate / StdErr / NumSrc / Rank / Lower / Upper.

    We look for the LATEST 4-digit year column whose sub-header is
    "rank"-flavoured (matches percentile rank) and read every data row.
    """
    import openpyxl  # heavy — lazy import

    wb = openpyxl.load_workbook(io.BytesIO(raw_bytes), data_only=True, read_only=True)
    out: list[tuple[str, int, str, float]] = []
    unknown_sheets: set[str] = set()

    for sheet in wb.worksheets:
        canonical = _normalise_dimension(sheet.title)
        if canonical is None:
            unknown_sheets.add(sheet.title)
            continue
        rows = list(sheet.iter_rows(values_only=True))
        if not rows:
            continue
        # Find header row: first row containing a cell that says "country"
        # (case-insensitive) AND a cell that looks like a 4-digit year.
        header_row_idx: Optional[int] = None
        for i, row in enumerate(rows[:20]):
            has_country = any(
                isinstance(c, str) and "country" in c.lower() for c in row
            )
            has_year = any(
                isinstance(c, (int, str)) and _looks_like_year(c) for c in row
            )
            if has_country and has_year:
                header_row_idx = i
                break
        if header_row_idx is None:
            continue
        header = rows[header_row_idx]
        # Sub-header may be the next row carrying "Rank" / "Estimate" labels.
        sub_header = rows[header_row_idx + 1] if header_row_idx + 1 < len(rows) else None

        # Locate column indices.
        iso3_col: Optional[int] = None
        country_name_col: Optional[int] = None
        for j, c in enumerate(header):
            if not isinstance(c, str):
                continue
            cl = c.strip().lower()
            if cl in {"code", "country code", "iso", "iso code", "country iso3", "wbcode"}:
                iso3_col = j
            elif "country" in cl and iso3_col is None and country_name_col is None:
                country_name_col = j

        # Year columns: collect (col_idx, year) for every Rank-flavoured
        # year in the header.  If the sheet uses a sub-header row to
        # label metrics, prefer the column whose sub-header is "Rank";
        # otherwise treat every year column as Rank (older WGI layouts).
        year_cols: list[tuple[int, int]] = []
        for j, c in enumerate(header):
            y = _to_year(c)
            if y is None:
                continue
            if sub_header is not None and j < len(sub_header):
                sub = sub_header[j]
                if isinstance(sub, str) and "rank" not in sub.lower():
                    continue
            year_cols.append((j, y))

        if iso3_col is None or not year_cols:
            continue

        # Take the LATEST year only — Step 3 scope per locked design.
        latest_year_col_idx, latest_year = max(year_cols, key=lambda t: t[1])

        # Read data rows starting after the header (+ sub-header if present).
        data_start = header_row_idx + (2 if sub_header else 1)
        for row in rows[data_start:]:
            if iso3_col >= len(row):
                continue
            iso3_raw = row[iso3_col]
            if not isinstance(iso3_raw, str) or not iso3_raw.strip():
                continue
            iso3 = iso3_raw.strip().upper()
            pct_raw = row[latest_year_col_idx] if latest_year_col_idx < len(row) else None
            try:
                pct = float(pct_raw) if pct_raw is not None else None
            except (TypeError, ValueError):
                pct = None
            if pct is None:
                continue
            out.append((iso3, latest_year, canonical, pct))

    summary["unknown_dimensions"] = sorted(unknown_sheets)
    wb.close()
    return out


def _looks_like_year(cell) -> bool:
    """True if a cell parses as a 4-digit year in the WGI range."""
    return _to_year(cell) is not None


def _to_year(cell) -> Optional[int]:
    """Coerce a cell value to a 4-digit year in [1996, 2099], else None."""
    if cell is None:
        return None
    if isinstance(cell, int):
        if 1996 <= cell <= 2099:
            return cell
        return None
    if isinstance(cell, float) and cell.is_integer():
        return _to_year(int(cell))
    if isinstance(cell, str):
        s = cell.strip()
        if len(s) == 4 and s.isdigit():
            return _to_year(int(s))
    return None


def _build_iso3_to_iso2_map(session: Session) -> dict[str, str]:
    """Return ISO-3 → ISO-2 mapping from the ``countries`` reference table.

    Countries that lack an ISO-3 in the seed are skipped (the join just
    won't resolve them).  Logged for visibility on first run.
    """
    rows = session.execute(
        select(Country.iso2, Country.iso3).where(Country.iso3.is_not(None))
    ).all()
    mapping = {iso3.upper(): iso2 for (iso2, iso3) in rows if iso3 and iso2}
    log.info("wgi_ingest.iso3_map_built", n_countries=len(mapping))
    return mapping
