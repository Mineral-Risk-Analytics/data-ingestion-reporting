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

from app.models.country import Country
from app.models.documents import SourceDocument
from app.models.regulatory import (
    RiskEvent,
    RiskEventGeography,
    RiskEventHsMapping,
    RiskEventMaterial,
)
from app.models.source import Source
from app.services.ingestion.normalizers.material_resolver import MaterialCache, MaterialResolver

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SOURCE_NAME = "IEA Critical Minerals Policy Tracker"
_SOURCE_TYPE = "iea_policy_tracker"
_SOURCE_PHASE = "1"
_SOURCE_BASE_URL = (
    "https://www.iea.org/data-and-statistics/data-tools/critical-minerals-policy-tracker"
)

# Default local path — override via IEA_POLICY_TRACKER_PATH env var or CLI.
DEFAULT_FILE_PATH = os.environ.get(
    "IEA_POLICY_TRACKER_PATH",
    "data/iea/policy_tracker.csv",
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
# ISO-3 → ISO-2 country code map (DB-backed)
# ---------------------------------------------------------------------------

def _get_or_create_source(session: Session) -> int:
    """Get or create the ``Source`` row for IEA Policy Tracker. Returns id.

    Mirrors the pattern in ``gta.py::_get_or_create_gta_source`` — the
    SourceDocument table requires a non-null ``source_id``, so the Source
    row must exist first.  Idempotent: re-runs return the existing row.
    """
    existing = session.scalar(select(Source).where(Source.name == _SOURCE_NAME))
    if existing is not None:
        return existing.id
    source = Source(
        name=_SOURCE_NAME,
        source_type=_SOURCE_TYPE,
        phase=_SOURCE_PHASE,
        is_active=True,
        config_json={"base_url": _SOURCE_BASE_URL, "method": "csv_download"},
    )
    session.add(source)
    session.flush()
    log.info("iea_policy_tracker.source_created", source_id=source.id)
    return source.id


def _get_or_create_source_document(
    session: Session,
    source_id: int,
    file_path: str | Path,
) -> int:
    """Get or create the SourceDocument row for the current Policy Tracker file.

    Idempotent on (source_id, external_id) where ``external_id`` is the
    string form of the file path.  Each Policy Tracker download gets its
    own SourceDocument row keyed by path so re-runs of the same file
    re-use the row, while a new download (different filename) creates a
    new one.
    """
    external_id = str(file_path)
    existing = session.scalar(
        select(SourceDocument).where(
            SourceDocument.source_id == source_id,
            SourceDocument.external_id == external_id,
        )
    )
    if existing is not None:
        return existing.id
    doc = SourceDocument(
        source_id=source_id,
        external_id=external_id,
        title=_SOURCE_NAME,
        url=_SOURCE_BASE_URL,
        document_type="policy_tracker_csv",
        metadata_json={"local_path": external_id},
    )
    session.add(doc)
    session.flush()
    log.info(
        "iea_policy_tracker.source_document_created",
        source_document_id=doc.id,
        external_id=external_id,
    )
    return doc.id


def _build_iso3_map(session: Session) -> dict[str, str]:
    """
    Build iso3→iso2 and common_name→iso2 lookup from the ``countries`` table.

    Replaces the former module-level ``_ISO3_TO_ISO2`` static dict.

    Keys:
      - iso3.upper()         — standard ISO 3166-1 alpha-3 codes (e.g. "COD"→"CD")
      - name.lower()         — each entry in Country.common_names (e.g. "drc"→"CD")

    Special case: IEA uses "EUR" as a non-standard bloc code for the European
    Union; we add it explicitly if an EU entry exists in the countries table.
    """
    rows = session.execute(
        select(Country.iso2, Country.iso3, Country.common_names)
    ).all()

    result: dict[str, str] = {}
    has_eu = False

    for iso2, iso3, common_names in rows:
        if iso2 == "EU":
            has_eu = True
        if iso3:
            result[iso3.upper()] = iso2
        if common_names and isinstance(common_names, list):
            for name in common_names:
                if isinstance(name, str) and name.strip():
                    result[name.lower()] = iso2

    # IEA uses "EUR" for the European Union bloc (not a real ISO3 code).
    if has_eu:
        result.setdefault("EUR", "EU")

    return result

# ---------------------------------------------------------------------------
# Mineral keyword map — REMOVED (Phase 2)
# ---------------------------------------------------------------------------
# _MINERAL_CANONICAL was a hardcoded keyword→canonical_name dict used in
# _extract_minerals_from_tech_and_text() (strategy 2).
# It has been replaced by MaterialCache.detect() in ingest_policy_tracker(),
# which loads keywords from hs_code_material_mappings.keywords (DB-backed).
# To add or update mineral keywords, update that table via seed_hs_mappings.py.

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


def _extract_iso2_codes(
    countries_raw: str | None,
    iso3_map: dict[str, str],
) -> list[str]:
    """
    Parse the IEA 'countries' JSON array and return ISO2 codes.

    Input:  '[{"iso3": "EUR", "name": "European Union"}, {"iso3": "USA", ...}]'
    Output: ["EU", "US"]

    ``iso3_map`` is the DB-backed lookup built by ``_build_iso3_map()``.
    Falls back to name-based lookup (lowercased) if iso3 is absent or
    unrecognised.
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
        iso2 = iso3_map.get(iso3)
        if iso2 is None:
            # Fallback to name (common_names entries are stored lowercased in map)
            name = (item.get("name") or "").strip().lower()
            iso2 = iso3_map.get(name)
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


def _extract_tech_basket_minerals(technologies_raw: str | None) -> list[str]:
    """
    Derive canonical mineral names from IEA technology category strings using
    ``_TECH_MINERAL_BASKETS``.  This is strategy 1 of the original two-strategy
    approach; strategy 2 (keyword scanning of title/description) has been moved
    to ``ingest_policy_tracker()`` and is now handled by ``MaterialCache.detect()``.

    Returns a deduplicated list of canonical mineral names implied by the
    policy's technology categories.  Returns [] when ``technologies_raw`` is
    absent or does not match any basket key.

    Note: the ``tags`` column is not used — in the current CSV export it is
    always empty.
    """
    found: list[str] = []
    tech_list = _parse_json_col(technologies_raw)
    if not isinstance(tech_list, list):
        return found
    for tech in tech_list:
        tech_lower = str(tech).lower().strip()
        # Exact match first, then substring fallback for minor naming variations
        basket = _TECH_MINERAL_BASKETS.get(tech_lower)
        if basket is None:
            basket = next(
                (v for k, v in _TECH_MINERAL_BASKETS.items() if k in tech_lower),
                None,
            )
        if basket:
            for mineral in basket:
                if mineral not in found:
                    found.append(mineral)
    return found


# Keep old name as an alias so any external callers don't immediately break.
# Remove in Phase 3.
def _extract_minerals_from_tech_and_text(
    technologies_raw: str | None,
    title: str = "",
    description: str = "",
) -> list[str]:  # pragma: no cover
    return _extract_tech_basket_minerals(technologies_raw)


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

def _parse_csv_rows(
    rows_iter: Any,
    iso3_map: dict[str, str],
) -> list[dict[str, Any]]:
    """
    Parse rows from a csv.DictReader using the known IEA PAMS column names.

    Required columns: title, countries
    Optional but used: description, status, year, technologies, tags, policyType

    ``iso3_map`` is passed to ``_extract_iso2_codes()`` for country resolution.
    Mineral detection now covers only the technology-basket path; keyword
    scanning of title/description is handled later by ``MaterialCache.detect()``
    in ``ingest_policy_tracker()``.
    """
    records: list[dict[str, Any]] = []

    for row in rows_iter:
        title = (row.get("title") or "").strip()
        countries_raw = row.get("countries") or ""

        if not title or not countries_raw:
            continue

        iso2_codes = _extract_iso2_codes(countries_raw, iso3_map)
        # Rows with no resolvable country are still included (logged at ingest time)

        description_clean = _strip_html(row.get("description"))
        # Tech-basket minerals only; keyword scan happens at ingest time.
        tech_basket_minerals = _extract_tech_basket_minerals(row.get("technologies"))

        policy_type_names = _extract_policy_type_names(row.get("policyType"))

        records.append({
            "title":              title,
            "description":        description_clean,
            "status":             (row.get("status") or "").strip(),
            "year":               (row.get("year") or "").strip(),
            "jurisdiction":       (row.get("jurisdiction") or "").strip(),
            "iso2_codes":         iso2_codes,           # list[str]
            "tech_basket_minerals": tech_basket_minerals,  # list[str] canonical names (basket only)
            "policy_type_names":  policy_type_names,    # list[str] e.g. ["Financing"]
            "countries_raw":      countries_raw,         # kept for content hash / metadata
        })

    return records


def parse_policy_tracker_file(
    path: str | Path,
    iso3_map: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """
    Parse the IEA Policy Tracker export (CSV or XLSX) and return normalised
    row dicts.

    Format is auto-detected from the file extension (.csv → csv module,
    .xlsx / .xls → openpyxl).  IEA currently exports as CSV.

    ``iso3_map`` is the DB-backed iso3→iso2 lookup from ``_build_iso3_map()``.
    When called standalone (outside of ``ingest_policy_tracker``), pass a
    manually-built dict or an empty dict — country resolution will fall back
    to logging unknowns.

    Raises FileNotFoundError if the path does not exist.
    """
    import csv as csv_module

    if iso3_map is None:
        iso3_map = {}

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
            records = _parse_csv_rows(reader, iso3_map)
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

    records = _parse_csv_rows(_seq_rows_as_dicts(), iso3_map)
    log.info("iea_policy_tracker.parsed_xlsx", path=str(path), row_count=len(records))
    return records


# Keep old name as an alias for backward compatibility
parse_policy_tracker_xlsx = parse_policy_tracker_file


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


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
        Dict with inserted, skipped (duplicate), and failed counts plus
        material_links and hs_mapping_links counters.

    Material attribution (two complementary paths):
      1. Tech basket  — ``_extract_tech_basket_minerals()`` maps IEA technology
                        categories to canonical mineral names.  Writes
                        ``RiskEventMaterial`` only (no HS stage attribution).
      2. Keyword scan — ``MaterialCache.detect()`` scans title + description
                        against DB-backed keywords from hs_code_material_mappings.
                        Writes ``RiskEventMaterial`` AND ``RiskEventHsMapping``
                        when hs_mapping_id is known (stage attribution).
    """
    if xls_path is None:
        xls_path = DEFAULT_FILE_PATH

    # Build DB-backed lookups once per run.
    iso3_map = _build_iso3_map(session)
    material_cache = MaterialCache.build(session)

    records = parse_policy_tracker_file(xls_path, iso3_map=iso3_map)

    # Source + SourceDocument scaffolding (fixed 2026-05-06).  Pre-fix this
    # used SourceDocument(source_type=, source_url=) — neither column exists
    # on the model; the actual schema requires source_id (FK) + external_id
    # + url + document_type, so the original code crashed on flush with a
    # NOT NULL constraint violation.  Now mirrors the gta.py pattern.
    source_id = _get_or_create_source(session)
    source_document_id = _get_or_create_source_document(session, source_id, xls_path)

    mat_resolver = MaterialResolver(session)

    inserted = 0
    skipped = 0
    skipped_no_material = 0    # added 2026-05-06 (per partner direction)
    failed = 0
    material_links = 0
    hs_mapping_links = 0

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

            # ── Pre-resolve material attribution from both paths (2026-05-06) ──
            # Resolve BEFORE inserting the RiskEvent so we can gate on empty
            # results and skip the row entirely.  Partner direction: don't
            # write events that have no resolvable material — they bloat the
            # events table without contributing actionable scoring signal.

            # Path 1: tech basket → resolve canonical names to material IDs.
            tech_basket_resolved: list[int] = []
            for canonical_name in rec["tech_basket_minerals"]:
                mat = mat_resolver.resolve_by_canonical_name(canonical_name)
                if mat is not None and mat.id not in tech_basket_resolved:
                    tech_basket_resolved.append(mat.id)

            # Path 2: keyword scan over title + description.
            search_text = f"{rec['title']} {rec['description']}"
            keyword_hits = material_cache.detect(search_text)

            # Gate: skip event entirely if neither path yields a resolvable
            # material.  We still need the event for evidence-layer use cases
            # later (positive-policy rationale) but with zero material
            # attribution, scoring queries that filter on RiskEventMaterial
            # never see it — it's just dead rows in risk_events.
            if not tech_basket_resolved and not keyword_hits:
                skipped_no_material += 1
                log.debug(
                    "iea_policy_tracker.skipped_no_material",
                    title=rec["title"],
                    countries_raw=rec["countries_raw"],
                )
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

            # Title: first country ISO2 + policy title, truncated
            country_label = rec["iso2_codes"][0] if rec["iso2_codes"] else rec["jurisdiction"] or "INTL"
            title = f"{country_label} — {rec['title']}"[:1024]
            summary = rec["description"][:500] if rec["description"] else None

            event = RiskEvent(
                source_document_id=source_document_id,
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
                    "tech_basket_minerals": rec["tech_basket_minerals"],
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

            # ── Geography junction — one row per country ──────────────────────
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

            # ── Material + HS junction rows (using pre-resolved data) ─────────
            # No defensive existence-check queries: we just inserted the
            # RiskEvent above, so there can't be any junction rows for it
            # yet.  Within-event dedup is handled by the seen_* sets below.
            seen_material_ids: set[int] = set()
            seen_hs_mapping_ids: set[int] = set()

            # Path 1: tech basket — RiskEventMaterial only (no HS attribution).
            for mat_id in tech_basket_resolved:
                if mat_id in seen_material_ids:
                    continue
                session.add(RiskEventMaterial(
                    risk_event_id=event.id,
                    material_id=mat_id,
                    relevance_score=0.85,
                    match_reason="technology_basket",
                ))
                material_links += 1
                seen_material_ids.add(mat_id)

            # Path 2: keyword scan — RiskEventMaterial + RiskEventHsMapping.
            for mat_id, relevance, matched_kw, hs_mapping_id in keyword_hits:
                if mat_id not in seen_material_ids:
                    session.add(RiskEventMaterial(
                        risk_event_id=event.id,
                        material_id=mat_id,
                        relevance_score=relevance,
                        match_reason=f"keyword_scan:{matched_kw[:48]}",
                    ))
                    material_links += 1
                    seen_material_ids.add(mat_id)

                if hs_mapping_id is not None and hs_mapping_id not in seen_hs_mapping_ids:
                    session.add(RiskEventHsMapping(
                        risk_event_id=event.id,
                        hs_mapping_id=hs_mapping_id,
                        relevance_score=relevance,
                        match_reason=f"keyword_scan:{matched_kw[:48]}",
                    ))
                    hs_mapping_links += 1
                    seen_hs_mapping_ids.add(hs_mapping_id)

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
        "skipped_no_material": skipped_no_material,
        "failed": failed,
        "material_links": material_links,
        "hs_mapping_links": hs_mapping_links,
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
