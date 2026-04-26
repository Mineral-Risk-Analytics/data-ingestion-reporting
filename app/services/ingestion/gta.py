"""Global Trade Alert (GTA) harmful-intervention ingestion.

GTA, maintained by the University of St. Gallen, is the authoritative free
database of government trade-policy interventions worldwide. Without these
records the Geopolitical/Trade pillar materially understates risk for
graphite (China), nickel (Indonesia), and cobalt (DRC) — every major export
restriction since 2018 is sourced from GTA.

We ingest **Red** (harmful) interventions only — Amber (announced) and Green
(liberalising) are out of scope for risk scoring. Each intervention is
filtered down to the rows whose affected HS codes match
``BATTERY_HS_PREFIXES`` and persisted as one ``RiskEvent`` plus
``RiskEventGeography`` / ``RiskEventMaterial`` junction rows.

GTA download URL stability
    GTA occasionally restructures their download endpoints. If the primary
    URL stops returning a CSV, check
    ``https://www.globaltradealert.org/data_extraction`` for the current
    bulk download link and update :data:`GTA_DOWNLOAD_URL` here. The URL
    actually used is recorded in each event's ``metadata_json["source_url"]``.

Column-name resilience
    GTA CSV column names have changed between releases (e.g.
    ``implementing_jurisdiction`` vs ``implementing_country``). The parser
    builds a lowercase column map from the actual header row and never
    assumes fixed column positions. If a *required* column is missing the
    parser raises ``ValueError`` with the available column names so the
    operator can update the alias map.

HS code format
    GTA stores HS codes as 6-digit strings, sometimes with leading zeros
    stripped during CSV roundtrips. We zero-pad to six digits before
    comparing against :data:`BATTERY_HS_PREFIXES`.

Severity calibration
    - Export bans                       → 0.9  (explicitly blocks supply)
    - All other active Red interventions → 0.7
    - Inactive / removed interventions  → 0.3
    The scoring engine applies recency decay on top of these base values.

Review workflow
    GTA events are inserted with ``verified=False``. The existing admin
    event-review UI promotes them; this ingester never auto-verifies.

Update cadence
    GTA refreshes weekly. Re-running this command is safe — duplicates are
    suppressed by ``RiskEvent.content_hash`` (and a secondary
    ``metadata_json->>'gta_id'`` check) so only new interventions hit the
    insert path.
"""

from __future__ import annotations

import csv
import hashlib
import io
from datetime import date, datetime, timezone
from typing import Any, Optional

import httpx
import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.documents import SourceDocument
from app.models.regulatory import (
    RiskEvent,
    RiskEventGeography,
    RiskEventMaterial,
)
from app.models.source import Source
from app.services.ingestion.comtrade import (
    _build_hs_material_map,
    _resolve_material_id,
)

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Constants & taxonomies
# ---------------------------------------------------------------------------

# NOTE: The old bulk-CSV endpoint (/data_extraction/download) no longer exists.
# GTA now requires registration and an API key to access data programmatically.
# See https://globaltradealert.org/api-access to register.
#
# Until the API is wired up, use the --local-file option in the CLI to pass
# a CSV you have manually exported from https://globaltradealert.org/data-center.
#
# When the API is ready, set this to:
#   "https://api.globaltradealert.org/api/v1/data/"
# and update download_gta_csv() to use POST + Authorization: APIKey <key>.
GTA_DOWNLOAD_URL = (
    "https://www.globaltradealert.org/data_extraction/download"
    "?dataset=state-acts&format=csv"
)

_SOURCE_NAME = "Global Trade Alert"
_SOURCE_TYPE = "gta"
_SOURCE_PHASE = "1"
_SOURCE_BASE_URL = "https://www.globaltradealert.org"

# Filter GTA rows where any affected HS code starts with one of these prefixes.
BATTERY_HS_PREFIXES: list[str] = [
    "2501",   # Salt; sulphur; earths/stone — lithium minerals
    "2504",   # Natural graphite
    "2602",   # Manganese ores and concentrates
    "2604",   # Nickel ores and concentrates
    "2825",   # Hydrazine; hydroxylamine; inorganic bases incl. lithium hydroxide/carbonate
    "2836",   # Carbonates — lithium carbonate
    "7501",   # Nickel mattes, oxide sinters
    "7502",   # Unwrought nickel
    "8104",   # Magnesium and articles
    "8107",   # Cobalt and articles
    "8108",   # Titanium (EV structural materials)
    "8507",   # Electric accumulators (batteries and cells)
    "8548",   # Waste and scrap of primary cells, batteries
]

# Map GTA implementing country names (or ISO2) → ISO2.
# GTA uses ISO2 codes in some columns and full country names in others.
GTA_COUNTRY_MAP: dict[str, str] = {
    "China": "CN",
    "Indonesia": "ID",
    "Democratic Republic of the Congo": "CD",
    "DRC": "CD",
    "Congo, Democratic Republic": "CD",
    "Russia": "RU",
    "Russian Federation": "RU",
    "United States of America": "US",
    "United States": "US",
    "USA": "US",
    "Chile": "CL",
    "Australia": "AU",
    "South Africa": "ZA",
    "Philippines": "PH",
    "Mozambique": "MZ",
    "European Union": "EU",
    "Germany": "DE",
    "Japan": "JP",
    "South Korea": "KR",
    "Korea, Republic of": "KR",
    "Canada": "CA",
    "Zimbabwe": "ZW",
    "Bolivia": "BO",
    "Argentina": "AR",
}

# Map GTA intervention_type → app.constants.RiskCategory string value.
GTA_INTERVENTION_CATEGORY_MAP: dict[str, str] = {
    "Export taxes": "geopolitical_trade",
    "Export quotas": "geopolitical_trade",
    "Export licensing requirements": "geopolitical_trade",
    "Export bans": "geopolitical_trade",
    "Export subsidies": "geopolitical_trade",
    "Import tariff": "geopolitical_trade",
    "Import quota": "geopolitical_trade",
    "Import ban": "geopolitical_trade",
    "Sanitary and phytosanitary measure": "regulatory_compliance",
    "Technical barrier to trade": "regulatory_compliance",
    "Local content requirement": "geopolitical_trade",
    "Trade finance": "financial_pressure",
    "Investment measure": "geopolitical_trade",
    "State aid": "financial_pressure",
    "Procurement": "geopolitical_trade",
}
DEFAULT_GTA_CATEGORY = "geopolitical_trade"

# RiskEvent.event_type is String(128); truncate to fit.
_EVENT_TYPE_MAX = 128
# Summary kept short so the row stays printable in admin tables.
_SUMMARY_MAX = 500
# Flush every N inserts to keep the SQLAlchemy unit-of-work compact.
_FLUSH_EVERY = 100


# ---------------------------------------------------------------------------
# CSV download
# ---------------------------------------------------------------------------

def download_gta_csv(url: str = GTA_DOWNLOAD_URL, timeout: int = 120) -> bytes:
    """Download the GTA State Acts CSV and return raw bytes.

    Streams the response so the full file is not held as a single allocation
    until every chunk has been received (the bulk export is ~50–100 MB
    uncompressed).

    Raises:
        httpx.HTTPStatusError: on non-2xx HTTP response.
        httpx.RequestError: on network / DNS / timeout failures.
    """
    log.info("gta.download.start", url=url)
    chunks: list[bytes] = []
    with httpx.stream("GET", url, timeout=timeout, follow_redirects=True) as response:
        response.raise_for_status()
        for chunk in response.iter_bytes():
            chunks.append(chunk)
    raw = b"".join(chunks)
    log.info("gta.download.done", bytes=len(raw))
    return raw


# ---------------------------------------------------------------------------
# CSV parsing & filtering
# ---------------------------------------------------------------------------

# Aliases per logical column. The first match (case-insensitive) in the CSV
# header row wins. Extending these keeps the ingester resilient to GTA
# header renames without touching the rest of the code.
_COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "id": ("id", "intervention_id", "state_act_id"),
    "title": ("title", "intervention_title", "state_act_title"),
    "date_announced": ("date_announced", "announced_date", "announcement_date"),
    "date_implemented": (
        "date_implemented",
        "implementation_date",
        "implemented_date",
    ),
    "date_removed": ("date_removed", "removal_date", "removed_date"),
    "implementing_jurisdiction": (
        "implementing_jurisdiction",
        # GTA curated exports use plural form
        "implementing_jurisdictions",
        "implementing_country",
        "jurisdiction",
        "implementer",
    ),
    "gta_evaluation": ("gta_evaluation", "evaluation", "traffic_light"),
    "intervention_type": ("intervention_type", "mast_chapter", "instrument"),
    "affected_hs_codes": (
        "affected_hs_codes",
        "hs_codes_affected",
        "hs_codes",
        "affected_products_hs",
        # GTA curated dataset exports name this column "Affected Products";
        # it may contain GTA internal product codes or HS codes depending on
        # the export type. Use --skip-hs-filter when ingesting curated exports
        # that are already pre-filtered (e.g. "Harmful Trade Policy
        # Interventions: Batteries") so the HS prefix check is bypassed.
        "affected_products",
    ),
    "description": ("description", "summary", "intervention_description"),
    "in_force": ("in_force", "currently_in_force", "is_in_force"),
}

# Required columns — the parser raises ValueError if any of these can't be
# resolved against the actual header row.
_REQUIRED_LOGICAL_COLUMNS: tuple[str, ...] = (
    "id",
    "title",
    "implementing_jurisdiction",
    "gta_evaluation",
    "intervention_type",
    "affected_hs_codes",
)

_RED_VALUES = {"red", "harmful"}
_TRUE_STRS = {"true", "1", "yes", "y", "t"}

_DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%Y-%m", "%Y/%m")


def _build_column_map(headers: list[str]) -> dict[str, str]:
    """Resolve logical column names → actual header names from the CSV.

    Normalises headers and alias candidates to lowercase with spaces converted
    to underscores, so GTA curated exports ("Intervention ID", "GTA Evaluation",
    "Implementing Jurisdictions") match the underscore-style aliases without
    needing explicit duplicate entries.
    """

    def _norm(s: str) -> str:
        return s.strip().lower().replace(" ", "_")

    normalised = {_norm(h): h for h in headers if h is not None}
    resolved: dict[str, str] = {}
    for logical, candidates in _COLUMN_ALIASES.items():
        for cand in candidates:
            actual = normalised.get(_norm(cand))
            if actual is not None:
                resolved[logical] = actual
                break
    missing = [c for c in _REQUIRED_LOGICAL_COLUMNS if c not in resolved]
    if missing:
        raise ValueError(
            "GTA CSV is missing required column(s) "
            f"{missing}. Headers seen: {sorted(normalised.values())}. "
            "Update _COLUMN_ALIASES in app/services/ingestion/gta.py with the "
            "current GTA column names."
        )
    return resolved


def _parse_date(raw: str) -> Optional[datetime]:
    """Parse a date string tolerantly.

    GTA mixes ``YYYY-MM-DD`` and ``YYYY-MM`` (and occasionally with ``/``
    separators). Returns a tz-aware UTC ``datetime`` so ``RiskEvent.event_date``
    (a ``DateTime(timezone=True)`` column) gets a consistent value. If only a
    year-month is present, day defaults to 1.
    """
    if not raw:
        return None
    raw = raw.strip()
    if not raw:
        return None
    for fmt in _DATE_FORMATS:
        try:
            naive = datetime.strptime(raw, fmt)
            return naive.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _split_hs_codes(raw: str) -> list[str]:
    """Split a GTA HS codes cell into normalised 6-digit strings.

    GTA separates codes with semicolons or commas. Codes may arrive as
    integers (``"260400"`` → ``260400``) or with chapter dots
    (``"26.04.00"``). We strip non-digits and zero-pad to six characters.
    """
    if not raw:
        return []
    out: list[str] = []
    # Allow either separator. csv parses already, so we work on the cell.
    parts = raw.replace(",", ";").split(";")
    for part in parts:
        digits = "".join(ch for ch in part if ch.isdigit())
        if not digits:
            continue
        out.append(digits.zfill(6)[:6] if len(digits) <= 6 else digits[:6])
    return out


def _matches_prefix(hs_code: str, prefixes: list[str]) -> bool:
    return any(hs_code.startswith(p) for p in prefixes)


def _resolve_country(raw: str) -> Optional[str]:
    """Map a GTA implementing-jurisdiction cell to ISO2, or ``None``.

    Accepts pre-mapped ISO2 codes (``"CN"``) and the names listed in
    :data:`GTA_COUNTRY_MAP`. Returns ``None`` for anything unrecognised so the
    caller can log + continue rather than abort.
    """
    if not raw:
        return None
    raw = raw.strip()
    if not raw:
        return None
    # Already ISO2.
    if len(raw) == 2 and raw.isalpha():
        return raw.upper()
    return GTA_COUNTRY_MAP.get(raw)


def _to_bool(raw: Any) -> bool:
    if isinstance(raw, bool):
        return raw
    if raw is None:
        return False
    return str(raw).strip().lower() in _TRUE_STRS


def parse_gta_csv(
    raw_bytes: bytes,
    hs_prefixes: list[str] = BATTERY_HS_PREFIXES,
    since_year: Optional[int] = 2018,
    skip_hs_filter: bool = False,
) -> list[dict]:
    """Parse GTA CSV bytes into a list of normalised intervention dicts.

    Filters to:
        - ``gta_evaluation`` is "Red" / "harmful" (case-insensitive)
        - At least one affected HS code matches one of ``hs_prefixes``
          (skipped when ``skip_hs_filter=True`` — use for pre-filtered curated
          exports such as "Harmful Trade Policy Interventions: Batteries" where
          GTA has already applied product-level filtering using its own internal
          classification codes)
        - ``date_announced`` or ``date_implemented`` is on or after
          ``since_year`` (when set)

    Returned dict keys::

        gta_id              int    (from "id" column; 0 if unparseable)
        title               str
        event_date          datetime  (tz-aware UTC; date_implemented preferred)
        implementing_iso2   str | None
        intervention_type   str
        risk_category       str   (mapped via GTA_INTERVENTION_CATEGORY_MAP)
        matched_hs_codes    list[str]   (only codes matching the prefix list)
        summary             str         (truncated to 500 chars)
        in_force            bool
        raw_row             dict[str, str]  (full original row for metadata_json)
    """
    text = raw_bytes.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        log.warning("gta.parse.empty_csv")
        return []

    col = _build_column_map(list(reader.fieldnames))
    log.info(
        "gta.parse.start",
        rows_estimate="streaming",
        hs_prefixes=hs_prefixes,
        since_year=since_year,
    )

    results: list[dict] = []
    seen = kept = 0

    for row in reader:
        seen += 1

        # 1. Red interventions only.
        evaluation = (row.get(col["gta_evaluation"]) or "").strip().lower()
        if evaluation not in _RED_VALUES:
            continue

        # 2. HS code filter — skipped for pre-filtered curated exports.
        all_hs = _split_hs_codes(row.get(col["affected_hs_codes"]) or "")
        if skip_hs_filter:
            # Trust GTA's curation; record whatever codes are present.
            matched = all_hs
        else:
            matched = [c for c in all_hs if _matches_prefix(c, hs_prefixes)]
            if not matched:
                continue

        # 3. Date filter (announced OR implemented within the year window).
        date_implemented_str = (
            row.get(col["date_implemented"]) if "date_implemented" in col else ""
        ) or ""
        date_announced_str = (
            row.get(col["date_announced"]) if "date_announced" in col else ""
        ) or ""

        event_date = _parse_date(date_implemented_str) or _parse_date(date_announced_str)
        if event_date is None:
            log.warning(
                "gta.parse.bad_date",
                gta_id=row.get(col["id"]),
                date_implemented=date_implemented_str,
                date_announced=date_announced_str,
            )
            continue

        if since_year is not None and event_date.year < since_year:
            continue

        # 4. Country & category resolution.
        implementing_iso2 = _resolve_country(
            row.get(col["implementing_jurisdiction"]) or ""
        )

        intervention_type = (row.get(col["intervention_type"]) or "").strip()
        risk_category = GTA_INTERVENTION_CATEGORY_MAP.get(
            intervention_type, DEFAULT_GTA_CATEGORY
        )

        # 5. id, title, summary, in_force.
        gta_id_raw = (row.get(col["id"]) or "").strip()
        try:
            gta_id = int(gta_id_raw)
        except ValueError:
            gta_id = 0
            log.warning("gta.parse.bad_id", value=gta_id_raw)

        title = (row.get(col["title"]) or "").strip()
        if not title:
            continue
        description = (
            row.get(col["description"], "") if "description" in col else ""
        ) or ""
        summary = description.strip()[:_SUMMARY_MAX]

        in_force = _to_bool(
            row.get(col["in_force"]) if "in_force" in col else False
        )

        results.append(
            {
                "gta_id": gta_id,
                "title": title,
                "event_date": event_date,
                "implementing_iso2": implementing_iso2,
                "intervention_type": intervention_type,
                "risk_category": risk_category,
                "matched_hs_codes": matched,
                "summary": summary,
                "in_force": in_force,
                "raw_row": dict(row),
            }
        )
        kept += 1

    log.info("gta.parse.done", rows_seen=seen, rows_kept=kept)
    return results


# ---------------------------------------------------------------------------
# Source / SourceDocument helpers
# ---------------------------------------------------------------------------

def _get_or_create_gta_source(session: Session) -> int:
    """Get or create the ``Source`` row for Global Trade Alert. Returns ``source.id``."""
    existing = session.scalar(select(Source).where(Source.name == _SOURCE_NAME))
    if existing is not None:
        return existing.id

    source = Source(
        name=_SOURCE_NAME,
        source_type=_SOURCE_TYPE,
        phase=_SOURCE_PHASE,
        is_active=True,
        config_json={"base_url": _SOURCE_BASE_URL, "method": "bulk_csv"},
    )
    session.add(source)
    session.flush()
    log.info("gta.source_created", source_id=source.id)
    return source.id


def _create_gta_source_document(
    session: Session,
    source_id: int,
    row_count: int,
    run_date: date,
) -> int:
    """Idempotent ``SourceDocument`` upsert keyed on ``(source_id, external_id)``.

    One document per ingestion run-date. Re-running on the same date reuses
    the existing document; row-level deduplication is handled by
    ``RiskEvent.content_hash``.
    """
    external_id = f"gta_bulk_csv_{run_date.isoformat()}"
    existing = session.scalar(
        select(SourceDocument).where(
            SourceDocument.source_id == source_id,
            SourceDocument.external_id == external_id,
        )
    )
    if existing is not None:
        # Refresh row_count metadata so it reflects the latest run.
        if existing.metadata_json is not None:
            existing.metadata_json = {
                **(existing.metadata_json or {}),
                "row_count": row_count,
            }
        return existing.id

    doc = SourceDocument(
        source_id=source_id,
        external_id=external_id,
        title=f"Global Trade Alert — bulk CSV download {run_date.isoformat()}",
        document_type="trade_policy_database",
        metadata_json={"run_date": run_date.isoformat(), "row_count": row_count},
    )
    session.add(doc)
    session.flush()
    return doc.id


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------

def _content_hash(title: str, summary: str, event_date: datetime) -> str:
    """SHA-256 of ``f'{title}{summary}{event_date.isoformat()}'``.

    Per the prompt spec — no separator characters. The hash is the primary
    deduplication key for ``risk_events``; GTA occasionally re-publishes the
    same intervention with a new ``id`` so the row id alone is not enough.
    """
    payload = f"{title}{summary}{event_date.isoformat()}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _existing_event_id(
    session: Session,
    *,
    content_hash: str,
    gta_id: int,
) -> Optional[int]:
    """Return the id of an existing ``RiskEvent`` for this intervention, if any.

    Primary check: ``content_hash`` equality.
    Secondary check: ``metadata_json->>'gta_id' = :gta_id`` — falls back to a
    Python-side check on SQLite (no JSONB ``->>`` operator there).
    """
    by_hash = session.scalar(
        select(RiskEvent.id).where(RiskEvent.content_hash == content_hash)
    )
    if by_hash is not None:
        return by_hash

    if gta_id:
        try:
            from sqlalchemy import text  # noqa: PLC0415  - lazy import for SQLite path
        except ImportError:
            text = None  # type: ignore[assignment]

        # Postgres path — JSONB key access. Cheap and correct.
        try:
            by_id = session.scalar(
                select(RiskEvent.id).where(
                    RiskEvent.metadata_json["gta_id"].as_string() == str(gta_id)
                )
            )
            if by_id is not None:
                return by_id
        except Exception:
            # SQLite (and any backend without JSONB ``->>``) — fall back to a
            # Python-side scan. This is O(N) but only invoked when the primary
            # hash check missed, which is rare in practice.
            for ev in session.scalars(
                select(RiskEvent).where(RiskEvent.metadata_json.is_not(None))
            ):
                meta = ev.metadata_json or {}
                if meta.get("gta_id") == gta_id:
                    return ev.id
    return None


# ---------------------------------------------------------------------------
# Severity calibration
# ---------------------------------------------------------------------------

def _severity_for(intervention_type: str, in_force: bool) -> float:
    """Conservative initial severity calibration.

    - ``0.9`` for explicit export bans (supply is blocked outright).
    - ``0.7`` for any other active Red intervention.
    - ``0.3`` for inactive / removed Red interventions (historical signal).

    The scoring engine applies recency decay on top of this base value, so
    recent interventions still dominate even when their severity score is
    moderate.
    """
    if "ban" in intervention_type.lower() and "export" in intervention_type.lower():
        return 0.9
    return 0.7 if in_force else 0.3


# ---------------------------------------------------------------------------
# Main ingest function
# ---------------------------------------------------------------------------

def ingest_gta(
    session: Session,
    url: str = GTA_DOWNLOAD_URL,
    since_year: Optional[int] = 2018,
    hs_prefixes: Optional[list[str]] = None,
    local_file: Optional[str] = None,
    skip_hs_filter: bool = False,
) -> dict[str, int]:
    """Download (or read locally), parse, and ingest GTA harmful interventions.

    Args:
        session: SQLAlchemy session. Committed once at the end of a successful run.
        url: Override download URL — used only when ``local_file`` is not set.
        since_year: Only ingest interventions on or after this calendar year.
            Default ``2018`` covers China graphite (2023), Indonesia nickel
            (2019–2023), and DRC cobalt restrictions.
        hs_prefixes: Override HS prefix filter. Defaults to
            :data:`BATTERY_HS_PREFIXES`.
        local_file: Path to a locally-downloaded GTA CSV file. When set, skips
            the HTTP download entirely. Use this when the GTA bulk download
            requires authentication — download manually from
            https://globaltradealert.org/data-center and pass the path here.
        skip_hs_filter: When ``True``, bypass the HS prefix filter and ingest
            all Red interventions regardless of product codes. Use this for
            pre-filtered curated exports from the GTA data center (e.g.
            "Harmful Trade Policy Interventions: Batteries") where GTA has
            already applied product-level filtering. The full bulk State Acts
            export should always use the default ``False``.

    Returns:
        dict with these keys::

            downloaded_rows         rows kept after Red + HS filters
            inserted                new RiskEvent rows created
            skipped_existing        rows skipped due to content_hash / gta_id match
            material_links          RiskEventMaterial rows created
            geography_links         RiskEventGeography rows created
            skipped_unknown_country events inserted but with no resolvable country
    """
    effective_prefixes = list(hs_prefixes) if hs_prefixes else BATTERY_HS_PREFIXES

    if local_file is not None:
        import pathlib
        path = pathlib.Path(local_file)
        if not path.exists():
            raise FileNotFoundError(f"GTA local file not found: {local_file}")
        log.info("gta.ingest.local_file", path=str(path), size_bytes=path.stat().st_size)
        raw_bytes = path.read_bytes()
    else:
        raw_bytes = download_gta_csv(url)
    interventions = parse_gta_csv(
        raw_bytes,
        hs_prefixes=effective_prefixes,
        since_year=since_year,
        skip_hs_filter=skip_hs_filter,
    )

    source_id = _get_or_create_gta_source(session)
    today = date.today()
    source_document_id = _create_gta_source_document(
        session, source_id, len(interventions), today
    )

    hs_material_map = _build_hs_material_map(session)

    inserted = 0
    skipped_existing = 0
    skipped_unknown_country = 0
    material_links_created = 0
    geography_links_created = 0
    pending_in_batch = 0

    for intervention in interventions:
        title = intervention["title"]
        summary = intervention["summary"]
        event_date: datetime = intervention["event_date"]
        gta_id = intervention["gta_id"]
        intervention_type = intervention["intervention_type"][:_EVENT_TYPE_MAX]
        implementing_iso2 = intervention["implementing_iso2"]
        matched_hs_codes = intervention["matched_hs_codes"]
        risk_category = intervention["risk_category"]
        in_force = intervention["in_force"]

        ch = _content_hash(title, summary, event_date)
        if _existing_event_id(session, content_hash=ch, gta_id=gta_id) is not None:
            skipped_existing += 1
            continue

        severity = _severity_for(intervention_type, in_force)

        geography_json: Optional[dict[str, Any]] = (
            {"primary": implementing_iso2} if implementing_iso2 else None
        )
        if implementing_iso2 is None:
            skipped_unknown_country += 1
            log.debug(
                "gta.unresolved_country",
                gta_id=gta_id,
                raw=intervention["raw_row"].get("implementing_jurisdiction"),
            )

        event = RiskEvent(
            source_document_id=source_document_id,
            event_type=intervention_type or "trade_intervention",
            event_date=event_date,
            title=title,
            summary=summary,
            severity_score=severity,
            confidence_score=0.9,
            risk_categories_json=[risk_category],
            geography_json=geography_json,
            content_hash=ch,
            metadata_json={
                "gta_id": gta_id,
                "gta_hs_codes": matched_hs_codes,
                "raw_intervention_type": intervention["intervention_type"],
                "in_force": in_force,
                "source_url": url,
            },
            verified=False,
        )
        session.add(event)
        session.flush()

        if implementing_iso2 is not None:
            session.add(
                RiskEventGeography(
                    risk_event_id=event.id,
                    country_code=implementing_iso2,
                    geography_context="primary",
                    relevance_score=1.0,
                )
            )
            geography_links_created += 1

        seen_material_ids: set[int] = set()
        for hs_code in matched_hs_codes:
            material_id = _resolve_material_id(hs_code, hs_material_map)
            if material_id is None or material_id in seen_material_ids:
                continue
            # Defensive: respect the (risk_event_id, material_id) UNIQUE
            # constraint on the junction table even though the in-memory
            # set above already prevents duplicates within this event.
            existing_link = session.scalar(
                select(RiskEventMaterial).where(
                    RiskEventMaterial.risk_event_id == event.id,
                    RiskEventMaterial.material_id == material_id,
                )
            )
            if existing_link is not None:
                seen_material_ids.add(material_id)
                continue

            session.add(
                RiskEventMaterial(
                    risk_event_id=event.id,
                    material_id=material_id,
                    relevance_score=0.9,
                    match_reason="hs_code",
                )
            )
            material_links_created += 1
            seen_material_ids.add(material_id)

        inserted += 1
        pending_in_batch += 1

        if pending_in_batch >= _FLUSH_EVERY:
            session.flush()
            log.info(
                "gta.ingest.batch_progress",
                inserted_so_far=inserted,
                skipped_so_far=skipped_existing,
            )
            pending_in_batch = 0

    session.commit()

    result = {
        "downloaded_rows": len(interventions),
        "inserted": inserted,
        "skipped_existing": skipped_existing,
        "material_links": material_links_created,
        "geography_links": geography_links_created,
        "skipped_unknown_country": skipped_unknown_country,
    }
    log.info("gta.ingest.done", **result)
    return result
