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
from app.models.supply import Material
from app.services.ingestion.material_classifier import MaterialClassifier
from app.services.ingestion.normalizers.material_baskets import (
    ANODE_BASKET,
    BATTERY_BASKET,
    BATTERY_BASKET_NO_MN,
    ENERGY_STORAGE_BASKET,
    FUEL_CELL_BASKET,
    HYDROGEN_BASKET,
    SOLAR_BASKET,
    WIND_BASKET,
    WIND_OFFSHORE_BASKET,
)
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

# Relevance applied to tech-basket-derived material attribution.
#
# 2026-05-12 (Step 2 audit follow-up): dropped from 0.85 to 0.40 because the
# basket path is a CATEGORY-LEVEL INFERENCE — when IEA tags a policy
# "Battery technologies" the basket auto-attributes 5 minerals
# (Li/Co/Ni/Graphite/Mn) with no evidence those specific materials appear
# in the policy's title or description.  Treating that as 0.85 confidence
# (same level as a direct canonical-name keyword match) inflated the
# basket's downstream contribution to per-material scoring.  0.40 still
# preserves the signal — a battery-policy event will appear in graphite's
# event feed — but at roughly half the weight a direct mention would
# carry.  See the IEA Policy Tracker audit (2026-05-12) for the data.
#
# Followup work tracked in #71 (Haiku classifier integration) will refine
# this further by using the LLM to filter the basket candidates against
# the actual policy text.
_TECH_BASKET_RELEVANCE = 0.40

# Battery-industry relevance gate for events with NO material attribution.
#
# 2026-05-12: when an IEA event produces no candidate materials (neither
# the tech basket nor the keyword scan resolve a single mineral), we
# previously dropped it.  Per partner direction we now preserve such
# events for analyst review, BUT only when there's a plausible signal
# that the policy is in scope for the EV-battery / critical-minerals
# industry.  Without a gate we'd preserve agriculture circular-economy
# policies, generic forced-labor due-diligence laws, and historical
# decrees that happen to live in the IEA tracker.
#
# Gate-1 (cheap, deterministic): substring search for any of these
# keywords in title + description.  Calibrated from the 2026-05-12 dropped-
# event probe — 48.7% of empty-candidate events pass.
_BATTERY_RELEVANCE_KEYWORDS: tuple[str, ...] = (
    "battery", "batteries",
    "ev ", " ev,", " ev.",  # bare "ev" with word-boundary surrogates
    "electric vehicle", "electric vehicles",
    "critical mineral", "critical minerals",
    "critical raw material", "critical raw materials",
    "strategic mineral", "strategic raw material",
    "lithium-ion", "li-ion",
    "anode", "cathode", "electrolyte",
    "energy storage",
    # Bare mineral names — picks up "lithium-bearing rocks" / "cobalt mining"
    # mentions in events that didn't trigger MaterialCache.detect (often
    # because Tier 1.3's list_threshold suppressed them as "generic CRM
    # lists").  Acceptable cross-fire: an agriculture policy mentioning
    # "potassium phosphate fertilizers" would now pass the gate; analyst
    # review catches it.
    "cobalt", "lithium", "nickel", "manganese", "graphite",
    "phosphate", "copper", "aluminum", "rare earth",
)

# Gate-2 (deterministic): policyType field carries CRM-categorical tags
# that guarantee battery-industry relevance even when title/description
# doesn't contain a keyword.
_BATTERY_RELEVANCE_POLICY_TYPES: frozenset[str] = frozenset({
    "Strategic mineral lists",
    "Stockpiling mechanisms",
    "Export controls and restrictions",
})


def _has_battery_relevance(rec: dict[str, Any]) -> bool:
    """Cheap deterministic battery-industry relevance check.

    Used to gate empty-candidate IEA events before they go to the
    review queue.  Passes when EITHER the title+description text
    contains a battery/CRM keyword, OR the policyType list includes a
    CRM-categorical tag.  Returns True when either gate triggers.
    """
    blob = (
        (rec.get("title") or "") + " " + (rec.get("description") or "")
    ).lower()
    if any(kw in blob for kw in _BATTERY_RELEVANCE_KEYWORDS):
        return True
    for pt in rec.get("policy_type_names") or []:
        if pt in _BATTERY_RELEVANCE_POLICY_TYPES:
            return True
    return False

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

# 2026-05-12: basket VALUES centralised in normalizers/material_baskets.py.
# This dict now maps IEA's tech-category STRINGS to those shared baskets so
# any divergence stays visible here while the basket contents themselves
# live in one canonical place.  Adding a new IEA tech category to a known
# basket is a single-line edit; adding a NEW basket requires a corresponding
# constant in material_baskets.py so other ingesters can share it.
_TECH_MINERAL_BASKETS: dict[str, list[str]] = {
    "battery technologies":           BATTERY_BASKET,             # Li, Co, Ni, Mn, Graphite (full LIB)
    "battery recycling":              BATTERY_BASKET_NO_MN,       # Li, Co, Ni, Graphite (pre-Mn LIB chemistry)
    "lithium-ion batteries":          BATTERY_BASKET_NO_MN,       # Li, Co, Ni, Graphite (NCA + older NCM)
    "other batteries":                ENERGY_STORAGE_BASKET,      # Li, Vanadium
    "battery electric":               BATTERY_BASKET[:3],         # Li, Co, Ni (drive-train cells — slicing safe: first 3 of BATTERY_BASKET = Li, Co, Ni)
    "energy storage technologies":    ENERGY_STORAGE_BASKET,
    "solar pv":                       SOLAR_BASKET,
    "solar":                          [SOLAR_BASKET[0]],          # silicon only
    "wind":                           WIND_BASKET,
    "wind offshore":                  WIND_OFFSHORE_BASKET,
    "fuel cells":                     FUEL_CELL_BASKET,
    "hydrogen electrolysis":          HYDROGEN_BASKET,
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

# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def _derive_event_type(policy_type_names: list[str]) -> str:
    combined = " ".join(policy_type_names).lower()
    if any(kw in combined for kw in ("invest", "financ", "fund", "grant", "subsid")):
        return "INVESTMENT_PLEDGE"
    return "POLICY_MILESTONE"


def _derive_event_subtype(event_type: str) -> str | None:
    """Map an IEA Policy Tracker event_type to a canonical event_subtype.

    2026-06-07 (Methodology correction):  IEA ``INVESTMENT_PLEDGE`` events
    are now routed to a ``POSITIVE_POLICY`` informational subtype rather
    than ``EXPORT_SUBSIDY``.  The change closes a directional bug — IEA
    captures supply-support policies (Canadian critical-minerals tax
    credits, Indonesian state-backed nickel investments, IRA §45X
    production guidance, etc.) which RAISED the producer country's
    geopolitical risk score under the old ``EXPORT_SUBSIDY`` →
    ``production_subsidy_distortion`` wiring.  From a buyer's
    perspective these policies REDUCE supply chain risk; treating them
    as distortion-creating misread the policy direction.

    ``POSITIVE_POLICY`` routes nowhere in the market_aggregator or
    hs_node_scorer sub-input matchers, so the events are recorded for
    rationale visibility but do not move scores.  Same pattern as
    ``EXPORT_DECLINE`` (informational Comtrade-derived) and
    ``PROCUREMENT_POLICY`` (informational GTA-derived).

    GTA's ``EXPORT_SUBSIDY`` routing is unaffected — GTA captures
    state-aid / trade-finance interventions in producer countries
    where the distortion direction is correctly risk-raising.  The
    subtype-to-pillar mapping in market_aggregator remains correct for
    GTA-sourced subsidy events.

    ``POLICY_MILESTONE`` events stay event_subtype=None as before.
    """
    if event_type == "INVESTMENT_PLEDGE":
        return "POSITIVE_POLICY"
    return None


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

    # Haiku classifier (2026-05-12, Tier-3-pattern integration for IEA).
    # When ANTHROPIC_API_KEY is set, the classifier refines candidate
    # material attributions against the actual policy text — rejecting
    # incidental keyword hits and broad tech-basket fan-out when the
    # text doesn't substantively discuss those materials.  When the key
    # is absent (CI, isolated tests, partner not yet provisioned), the
    # classifier is a no-op pass-through and we fall back to the prior
    # Tier 1.1 logic (basket primary, keyword scan as fallback only).
    # No is_active filter — the Material model has no soft-delete column and
    # the table is curated reference data (every row is a real material).
    materials_by_id: dict[int, str] = {
        m.id: m.canonical_name
        for m in session.scalars(select(Material)).all()
    }
    classifier = MaterialClassifier(materials_by_id=materials_by_id)

    inserted = 0
    skipped = 0
    # Replaced 2026-05-12: previously skipped_no_material / skipped_haiku_rejected
    # silently dropped events with no resolvable material.  Per partner
    # direction, those events are now preserved with needs_material_review
    # =True so an analyst can triage later.  Two distinct counters
    # distinguish the two paths.  Off-scope events (failing the
    # battery-relevance gate) are still dropped — counted separately.
    inserted_no_candidates_for_review = 0
    inserted_haiku_rejected_for_review = 0
    skipped_off_scope = 0           # failed deterministic keyword gate
    skipped_off_scope_haiku = 0     # passed keyword gate but Haiku said off-scope
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

            # ── Pre-resolve material attribution ─────────────────────────────
            # Three-stage attribution (2026-05-12, Haiku integration on top
            # of Tier 1.1 structured-field-first logic).
            #
            #   Stage 1: tech basket — partner-curated category→material map.
            #            Broad signal (a "Battery technologies" policy auto-
            #            attributes 5 minerals).
            #   Stage 2: keyword scan — MaterialCache.detect() over the
            #            title + description text.  Direct text evidence.
            #   Stage 3: Haiku confirmation — when ANTHROPIC_API_KEY is set,
            #            send candidates + text to Claude Haiku and filter
            #            attributions that the LLM rejects as incidental.
            #
            # Without Haiku, we fall back to Tier 1.1 semantics (basket
            # primary, keyword scan only when basket empty) — running both
            # paths concurrently without LLM mediation over-attributes
            # incidental mentions, which is the case Tier 1.1 was designed
            # to prevent.  With Haiku, we CAN run both paths because Haiku
            # filters the merged candidate set against the actual text.

            search_text = f"{rec['title']}\n\n{rec['description']}"

            # Stage 1: tech basket → canonical-name resolution.
            tech_basket_resolved: list[int] = []
            for canonical_name in rec["tech_basket_minerals"]:
                mat = mat_resolver.resolve_by_canonical_name(canonical_name)
                if mat is not None and mat.id not in tech_basket_resolved:
                    tech_basket_resolved.append(mat.id)

            # Stage 2: keyword scan.  Always runs when Haiku is enabled
            # (Haiku will reconcile); falls back to Tier 1.1 (basket-empty-
            # gated) when Haiku is disabled to preserve the pre-LLM accuracy
            # profile.
            #
            # 2026-05-12: we override Tier 1.3 caps for IEA specifically.
            # Industry-wide policies (Critical Raw Materials Act, etc.)
            # legitimately span 10+ minerals; the default list_threshold=5
            # was suppressing 39+ events that mention bare "lithium" / "nickel"
            # / "cobalt" etc. in the same paragraph.  Pass list_threshold=
            # None and max_materials=20 so detect() returns the full set;
            # Haiku (when enabled) filters down, and the review-queue gate
            # catches the rest.
            _IEA_DETECT_LIST_THRESHOLD = 999  # effectively no cap
            _IEA_DETECT_MAX_MATERIALS = 20
            keyword_hits: list[tuple[int, float, str, int | None]]
            if classifier.enabled:
                keyword_hits = material_cache.detect(
                    search_text,
                    list_threshold=_IEA_DETECT_LIST_THRESHOLD,
                    max_materials=_IEA_DETECT_MAX_MATERIALS,
                )
            else:
                # Without Haiku, keep the Tier-1.1 fallback shape but still
                # relax the caps so events with many real mineral mentions
                # land in the review queue rather than getting Tier-1.3-
                # suppressed.  The off-scope keyword gate below filters
                # the false positives.
                keyword_hits = (
                    material_cache.detect(
                        search_text,
                        list_threshold=_IEA_DETECT_LIST_THRESHOLD,
                        max_materials=_IEA_DETECT_MAX_MATERIALS,
                    )
                    if not tech_basket_resolved
                    else []
                )

            # Track whether ANY candidate ever existed.  Distinguishes
            # "scan returned nothing" from "scan found candidates but
            # Haiku rejected them all" downstream.
            initial_candidates_existed = bool(tech_basket_resolved or keyword_hits)

            # ── Merge into a unified candidate list for Haiku ──────────────
            # Tuple shape matches what MaterialCache.detect returns:
            #   (material_id, relevance, matched_keyword, hs_mapping_id|None)
            # Tech-basket entries get hs_mapping_id=None (basket carries no
            # HS-stage attribution).  When a material appears in BOTH paths,
            # we prefer the keyword-hit version since it preserves a real
            # hs_mapping_id (more precise stage attribution) and the higher
            # of the two relevances.
            unified_candidates: list[tuple[int, float, str, int | None]] = []
            cand_index: dict[int, int] = {}
            for mat_id in tech_basket_resolved:
                if mat_id in cand_index:
                    continue
                cand_index[mat_id] = len(unified_candidates)
                unified_candidates.append(
                    (mat_id, _TECH_BASKET_RELEVANCE, "technology_basket", None)
                )
            for kh_mat_id, kh_rel, kh_kw, kh_hs in keyword_hits:
                if kh_mat_id in cand_index:
                    # Material already in basket — upgrade entry, preferring
                    # keyword-scan's hs_mapping_id when present.
                    i = cand_index[kh_mat_id]
                    existing = unified_candidates[i]
                    upgraded_rel = max(existing[1], kh_rel)
                    upgraded_hs = kh_hs if kh_hs is not None else existing[3]
                    unified_candidates[i] = (
                        kh_mat_id,
                        upgraded_rel,
                        f"keyword_scan:{kh_kw[:48]}",
                        upgraded_hs,
                    )
                else:
                    cand_index[kh_mat_id] = len(unified_candidates)
                    unified_candidates.append(
                        (kh_mat_id, kh_rel, f"keyword_scan:{kh_kw[:48]}", kh_hs)
                    )

            # Stage 3: Haiku refinement.  No-op when key not set or text too
            # thin (_MIN_TEXT_CHARS = 200 in the classifier).  Returns the
            # unified candidate list with relevance scaled by Haiku's
            # confidence and rejected materials dropped entirely.
            refined_candidates = classifier.classify(search_text, unified_candidates)

            # ── Empty-candidate review path ─────────────────────────────────
            # Two distinct cases produce empty refined_candidates:
            #   (a) initial_candidates_existed=False — nothing matched at
            #       all (basket + keyword both empty).
            #   (b) initial_candidates_existed=True  — Haiku rejected every
            #       candidate as incidental.
            # In both cases the event has no confident material attribution,
            # but it may still be an industry-wide policy worth preserving
            # for analyst review (Critical Raw Materials Act, National
            # Resource Strategy, etc.).  We apply two layered gates:
            #   Gate-1 (cheap, deterministic): keyword/policyType check.
            #   Gate-2 (Haiku, when enabled):  ask the LLM "is this in
            #     scope for EV-battery / critical minerals?"
            # Events that fail either gate are dropped as off_scope.
            # Events that pass both are written WITHOUT RiskEventMaterial
            # rows but with metadata_json["needs_material_review"]=true.
            needs_material_review = False
            review_reason: Optional[str] = None
            if not refined_candidates:
                if not _has_battery_relevance(rec):
                    skipped_off_scope += 1
                    log.debug(
                        "iea_policy_tracker.skipped_off_scope_keyword",
                        title=rec["title"],
                    )
                    continue
                if classifier.enabled:
                    is_relevant, conf, hk_reason = (
                        classifier.assess_battery_industry_relevance(
                            search_text,
                            policy_type_names=rec.get("policy_type_names") or [],
                        )
                    )
                    if not is_relevant and conf >= 0.5:
                        skipped_off_scope_haiku += 1
                        log.debug(
                            "iea_policy_tracker.skipped_off_scope_haiku",
                            title=rec["title"],
                            confidence=conf,
                            reason=hk_reason,
                        )
                        continue
                needs_material_review = True
                review_reason = (
                    "haiku_rejected_all"
                    if initial_candidates_existed
                    else "no_candidates"
                )
                if review_reason == "no_candidates":
                    inserted_no_candidates_for_review += 1
                else:
                    inserted_haiku_rejected_for_review += 1

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
                event_subtype=_derive_event_subtype(event_type),
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
                    # 2026-05-12: events with no confident material
                    # attribution are preserved with these flags so an
                    # analyst can triage them later in a dedicated review
                    # queue.  False / None for fully-attributed events.
                    "needs_material_review": needs_material_review,
                    "review_reason": review_reason,
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

            # ── Material + HS junction rows ───────────────────────────────────
            # Iterate the refined candidate list — already deduped via
            # cand_index above, already passed through Haiku (or no-op
            # passed-through when Haiku is disabled).  Each entry's
            # match_reason carries either "technology_basket" or
            # "keyword_scan:<kw>"; if Haiku ran, the classifier prepends
            # "llm_confirmed:" so we can tell LLM-verified attributions
            # apart from raw ones in downstream queries.
            seen_hs_mapping_ids: set[int] = set()
            for mat_id, relevance, matched_kw, hs_mapping_id in refined_candidates:
                session.add(RiskEventMaterial(
                    risk_event_id=event.id,
                    material_id=mat_id,
                    relevance_score=relevance,
                    match_reason=matched_kw[:64],
                ))
                material_links += 1

                if (
                    hs_mapping_id is not None
                    and hs_mapping_id not in seen_hs_mapping_ids
                ):
                    session.add(RiskEventHsMapping(
                        risk_event_id=event.id,
                        hs_mapping_id=hs_mapping_id,
                        relevance_score=relevance,
                        match_reason=matched_kw[:64],
                    ))
                    hs_mapping_links += 1
                    seen_hs_mapping_ids.add(hs_mapping_id)

            # ``inserted`` counts events that made it to RiskEvent WITH at
            # least one RiskEventMaterial.  Review-path events were already
            # counted under inserted_no_candidates_for_review /
            # inserted_haiku_rejected_for_review above; they DON'T contribute
            # to ``inserted`` so the result-dict sanity sum
            # (inserted + review-counters + skip-counters + duplicate +
            # failed = total_rows) balances.
            if not needs_material_review:
                inserted += 1
            total_written = (
                inserted
                + inserted_no_candidates_for_review
                + inserted_haiku_rejected_for_review
            )

            if total_written % _FLUSH_EVERY == 0:
                session.flush()
                log.info(
                    "iea_policy_tracker.progress",
                    inserted=inserted,
                    review_queue=(
                        inserted_no_candidates_for_review
                        + inserted_haiku_rejected_for_review
                    ),
                    skipped=skipped,
                    total=len(records),
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
        # Review-queue counters (replace prior skipped_no_material /
        # skipped_haiku_rejected — same events, now preserved instead of
        # dropped).  Sum of these two equals the size of the review queue
        # from this run.
        "inserted_no_candidates_for_review": inserted_no_candidates_for_review,
        "inserted_haiku_rejected_for_review": inserted_haiku_rejected_for_review,
        # Genuinely off-scope events (failed the relevance gates).
        "skipped_off_scope": skipped_off_scope,
        "skipped_off_scope_haiku": skipped_off_scope_haiku,
        "failed": failed,
        "material_links": material_links,
        "hs_mapping_links": hs_mapping_links,
        "haiku_enabled": classifier.enabled,
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
