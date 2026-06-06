"""World Bank Pink Sheet commodity price ingestion.

Downloads CMO-Historical-Data-Monthly.xlsx directly from the World Bank and
upserts monthly price observations into ``commodity_prices``.

Source
------
World Bank Commodity Price Data ("Pink Sheet")
https://www.worldbank.org/en/research/commodity-markets

The ``Monthly Prices`` worksheet has a non-standard multi-row header:
  - Rows 1–4: metadata (title, description, attribution) — skipped
  - Row 5:    commodity names (e.g. "Cobalt", "Copper", "Lithium carbonate, battery grade")
  - Row 6:    units per commodity (e.g. "$/mt", "$/kg", "$/troy oz")
  - Row 7+:   data rows — column A is a date string like "2024M01",
              subsequent columns are price floats (or empty when not reported)

Stage attribution (May 2026)
----------------------------
The Pink Sheet column header is canonicalised to a tracked material via
``material_source_aliases`` (source_system='worldbank_pinksheet').  For
columns whose trading basis maps cleanly to a specific HS prefix
(``"Lithium carbonate, battery grade"`` → 2836.91 battery_grade,
LME industrial metals → refined-stage HS, etc.), we additionally write
``commodity_prices.hs_mapping_id`` and ``commodity_prices.price_form``
(verbatim header) so downstream scoring / UI can distinguish stages.

Headers without a Pink-Sheet-disclosed form (bare ``"Graphite"``, bare
``"Manganese"``) get ``hs_mapping_id=NULL`` and ``price_form=NULL``.
That's the honest representation — Pink Sheet doesn't tell us the
basis, so we don't fabricate one.

Re-running this ingestion is safe.  The unique constraint
``(material_id, price_date, source, hs_mapping_id, price_form)`` on
``commodity_prices`` (migration 030) is the idempotency guard.
"""

from __future__ import annotations

import io
import re
from datetime import date, datetime
from typing import Optional

import httpx
import structlog
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.supply import CommodityPrice
from app.services.ingestion.material_resolver import MaterialAliasResolver
from app.services.ingestion.normalizers.material_resolver import (
    MaterialResolver,
)

log = structlog.get_logger(__name__)

# 10.1 (2026-06): module-level default kept for backwards compatibility with
# callers (and existing tests) that pass ``url=PINK_SHEET_URL`` explicitly.
# Production callers should rely on the ``download_pink_sheet`` default,
# which reads the URL from settings so an operator can override via the
# ``WORLDBANK_PINK_SHEET_URL`` env var when the World Bank rotates the
# URL each January.  When the configured URL 404s the download function
# logs a discovery hint pointing at the URL it found by scraping the
# landing page; the job still fails (we never silently switch sources).
PINK_SHEET_URL = (
    "https://thedocs.worldbank.org/en/doc/74e8be41ceb20fa0da750cda2f6b9e4e-0050012026"
    "/related/CMO-Historical-Data-Monthly.xlsx"
)

# 10.1: regex matches the "Monthly prices" XLSX link on the World Bank
# Commodity Markets landing page.  Used ONLY for failure-mode discovery
# logging - never used as a fallback source.  The pattern is permissive
# so it survives small landing-page redesigns: it just requires a doc
# URL ending in CMO-Historical-Data-Monthly.xlsx anywhere in the HTML.
_PINK_SHEET_URL_DISCOVERY_PATTERN = re.compile(
    r'https://thedocs\.worldbank\.org/[^"\s]+CMO-Historical-Data-Monthly\.xlsx'
)

_SOURCE = "worldbank_pink_sheet"
_ALIAS_SOURCE_SYSTEM = "worldbank_pinksheet"

# ── Stage attribution per Pink Sheet header ─────────────────────────────────
# Maps ``Pink Sheet column header -> 6-digit HS prefix`` for every column
# the World Bank actually publishes in the "Monthly Prices" worksheet of
# CMO-Historical-Data-Monthly.xlsx that maps to one of our tracked
# materials.  Resolution to ``hs_mapping_id`` happens at ingest time via
# ``MaterialResolver.resolve_by_hs_code`` against the curated rows in
# ``hs_code_material_mappings``.
#
# 10.4 audit (2026-06)
# --------------------
# Authoritative inspection of the live XLSX file (71 columns total) and
# its "Description" tab established the full scope of what Pink Sheet
# does and does not publish.  Highlights:
#
#   * Pink Sheet DOES publish: Aluminum, Copper, Iron ore, Lead, Nickel,
#     Tin, Zinc, Gold, Platinum, Silver, Phosphate rock (plus energy +
#     agricultural commodities not relevant here).  Each carries a
#     documented price basis in the Description tab (LME settlement for
#     the base metals, 62% Fe CFR China fines for iron ore, FOB North
#     Africa for phosphate rock, etc.).
#
#   * Pink Sheet does NOT publish: Cobalt, Lithium (any form), Manganese
#     (any form), Graphite (any form), or Rare Earth Elements.  These
#     are our highest-priority battery minerals; for them the engine
#     gets no Pink Sheet price-volatility signal.  USGS MCS Fig 10 is
#     the current substitute (annual cadence, lower resolution).  Phase
#     1.5 work is tracked to identify monthly price sources for these
#     battery minerals (LME cobalt contract, Fastmarkets, Benchmark
#     Mineral Intelligence, Argus Media are candidates).
#
# Pre-10.4 the dict contained four entries (Cobalt, Lithium carbonate,
# Manganese ore, "Tin, LME") referencing columns that do not exist in
# the live XLSX.  The parser silently skipped these unknown columns so
# nothing was breaking, but the dead entries documented false
# expectations.  10.4 removed them and adds the two entries that DO
# resolve to tracked materials but were previously missing (Iron ore,
# Phosphate rock).
#
# Downstream consequence to be aware of: ``market_aggregator``'s
# price-volatility math filters CommodityPrice by material_id only -
# rows from Pink Sheet for the same material are averaged into one
# volatility signal regardless of their hs_mapping_id.  Each material
# below currently has a SINGLE Pink Sheet column, so the no-mixing case
# holds.
_HEADER_TO_HS_PREFIX: dict[str, str] = {
    # Base metals - LME settlement basis (refined stage)
    "Aluminum":            "760110",  # aluminium unwrought, unalloyed (LME min 99.7% purity)
    "Copper":              "740311",  # copper cathodes (LME grade A, min 99.9935% purity)
    "Nickel":              "750210",  # nickel unwrought, unalloyed (LME cathodes, min 99.8%)
    "Tin":                 "800110",  # tin unwrought, unalloyed (LME refined 99.85%)
    "Zinc":                "790111",  # zinc unwrought, unalloyed (LME min 99.95% since 1990)
    # Precious metals
    "Platinum":            "711011",  # platinum unwrought (99.95% min purity, plate/ingot)
    # Bulk ores / industrial inputs (10.4 additions, basis confirmed from XLSX
    # Description tab)
    "Iron ore, cfr spot":  "260111",  # iron ore fines, CFR China, 62% Fe, non-agglomerated
    "Phosphate rock":      "251010",  # natural calcium phosphates, unground, FOB North Africa
    # ---- Intentionally absent ------------------------------------------
    # Columns that exist in the XLSX but are NOT mapped because they don't
    # correspond to a tracked material:
    #   "Lead" (LME refined 99.97%) - not in tracked battery materials
    #   "Gold", "Silver" - not battery-relevant
    #
    # Columns the engine USED to reference that do NOT exist in the
    # current XLSX (removed in 10.4):
    #   "Cobalt", "Lithium carbonate, battery grade", "Manganese ore",
    #   "Tin, LME", "Aluminium" (British spelling - XLSX uses American).
    # These were dead entries that the parser silently skipped.
    #
    # Battery-critical minerals NOT published by Pink Sheet (cobalt,
    # lithium, manganese, graphite, REEs) feed price-volatility scoring
    # only via USGS MCS Fig 10 (annual cadence) today.  See Phase 1.5
    # task "Identify monthly price sources for battery minerals not in
    # Pink Sheet" for the planned remediation.
}

_UNIT_MAP: dict[str, str] = {
    "$/mt": "per_mt",
    "$/kg": "per_kg",
    "$/troy oz": "per_troy_oz",
}

_BATCH_SIZE = 500


def _discover_pink_sheet_url(landing_page_url: str, timeout: int = 30) -> Optional[str]:
    """Scrape the World Bank Commodity Markets landing page for the current
    "Monthly prices" XLSX URL.

    10.1 helper (2026-06).  Used only when the configured download URL has
    already failed with 404.  Returns the first URL on the page that
    matches ``_PINK_SHEET_URL_DISCOVERY_PATTERN``, or ``None`` if no
    match is found (page redesign / network error / etc).  The caller
    logs the result as a hint - this function never causes the ingest
    to switch to the discovered URL automatically, because a future
    World Bank page redesign could cause the scraper to return a wrong
    or stale URL and silently corrupt the ingest.
    """
    try:
        resp = httpx.get(landing_page_url, timeout=timeout, follow_redirects=True)
        resp.raise_for_status()
    except (httpx.HTTPError, httpx.TimeoutException) as exc:
        log.warning(
            "pinksheet.url_discovery.failed",
            landing_page=landing_page_url,
            error=str(exc),
            hint=(
                "Couldn't scrape the World Bank landing page for an updated "
                "URL hint.  Operator must find the current Monthly prices "
                "XLSX URL manually and set WORLDBANK_PINK_SHEET_URL."
            ),
        )
        return None

    match = _PINK_SHEET_URL_DISCOVERY_PATTERN.search(resp.text)
    if match is None:
        log.warning(
            "pinksheet.url_discovery.no_match",
            landing_page=landing_page_url,
            hint=(
                "World Bank landing page no longer contains a URL matching "
                "the expected pattern.  The page may have been redesigned. "
                "Operator must find the Monthly prices XLSX URL manually."
            ),
        )
        return None

    return match.group(0)


def download_pink_sheet(url: Optional[str] = None, timeout: int = 60) -> bytes:
    """Download the Pink Sheet Excel file and return raw bytes.

    Streams the response so the full file is not held as a single allocation
    until all chunks have been received.

    Args:
        url: Override URL. When None (default), reads from
            ``settings.worldbank_pink_sheet_url`` so operator can fix a
            broken URL via env var without a code change.

    Raises:
        httpx.HTTPStatusError: on non-2xx HTTP response. 10.1 (2026-06):
            on 404 specifically, scrapes the World Bank landing page for
            a discovery hint and logs the URL it found before re-raising,
            so the operator sees exactly what to set
            ``WORLDBANK_PINK_SHEET_URL`` to.
        httpx.TimeoutException: if the download exceeds ``timeout`` seconds.
    """
    settings = get_settings()
    effective_url = url if url is not None else settings.worldbank_pink_sheet_url

    log.info("pinksheet.download.start", url=effective_url)
    chunks: list[bytes] = []

    try:
        with httpx.stream(
            "GET", effective_url, timeout=timeout, follow_redirects=True,
        ) as response:
            response.raise_for_status()
            for chunk in response.iter_bytes():
                chunks.append(chunk)
    except httpx.HTTPStatusError as exc:
        # 10.1 (2026-06): on 404, fetch the World Bank landing page and
        # extract the current Monthly-prices URL as a discovery hint for
        # the operator.  Reported as a structured log so the operator
        # gets the new URL handed to them - no silent retry against the
        # scraped URL, because a page redesign could produce a wrong
        # match.
        if exc.response is not None and exc.response.status_code == 404:
            discovered = _discover_pink_sheet_url(
                settings.worldbank_pink_sheet_landing_page,
            )
            if discovered is not None and discovered != effective_url:
                log.error(
                    "pinksheet.download.url_404_with_discovered_hint",
                    configured_url=effective_url,
                    discovered_url=discovered,
                    hint=(
                        "World Bank rotates the Pink Sheet URL annually "
                        "(typically in January).  Set "
                        "WORLDBANK_PINK_SHEET_URL=<discovered_url> to fix.  "
                        "Job still failing (not silently switching sources)."
                    ),
                )
            else:
                log.error(
                    "pinksheet.download.url_404_no_hint_available",
                    configured_url=effective_url,
                    hint=(
                        "World Bank URL returned 404 and discovery scraping "
                        "produced no match.  Operator must find the current "
                        "Monthly prices XLSX URL manually at "
                        f"{settings.worldbank_pink_sheet_landing_page} and "
                        "set WORLDBANK_PINK_SHEET_URL."
                    ),
                )
        raise

    raw = b"".join(chunks)
    log.info("pinksheet.download.done", bytes=len(raw))
    return raw


def _normalise_unit(raw_unit: str) -> str:
    """Return a normalised unit string, or the raw unit if unrecognised."""
    if not raw_unit:
        return "unknown"
    stripped = raw_unit.strip()
    return _UNIT_MAP.get(stripped, stripped)


def _parse_date(cell_value: object) -> Optional[date]:
    """Parse a Pink Sheet date cell (e.g. ``"2024M01"``) to a ``date``.

    Returns ``None`` if the value is missing or cannot be parsed, so the
    caller can skip the row rather than raise.
    """
    if cell_value is None:
        return None
    raw = str(cell_value).strip()
    try:
        return datetime.strptime(raw, "%YM%m").date()
    except ValueError:
        return None


def parse_pink_sheet(
    raw_bytes: bytes,
    *,
    accepted_headers: Optional[set[str]] = None,
) -> list[dict]:
    """Parse the Pink Sheet Excel bytes into a list of price observation dicts.

    Each returned dict has:
        price_date  (datetime.date)
        commodity   (str — the Pink Sheet column header, e.g. "Cobalt")
        price_usd   (float)
        price_unit  (str — normalised: "per_mt", "per_kg", "per_troy_oz", or raw)
        raw_unit    (str — original unit string from row 6)

    Rows where ``price_usd`` is ``None``, empty, or not a valid positive float
    are skipped.  Rows where the date cannot be parsed are skipped with a
    warning.

    Args:
      raw_bytes:         Pink Sheet Excel file content.
      accepted_headers:  When provided, only include columns whose header
                         is in this set.  Used by ``ingest_pink_sheet`` to
                         pre-filter to headers that resolve via the alias
                         table.  When ``None``, every column is parsed
                         (caller decides how to filter downstream).
    """
    try:
        import openpyxl  # noqa: PLC0415
    except ImportError as exc:
        raise ImportError(
            "openpyxl is required to parse the Pink Sheet. "
            "Add 'openpyxl>=3.1.0' to pyproject.toml and run 'uv sync'."
        ) from exc

    wb = openpyxl.load_workbook(io.BytesIO(raw_bytes), read_only=True, data_only=True)

    # The relevant data is in the "Monthly Prices" sheet.
    sheet_name = "Monthly Prices"
    if sheet_name not in wb.sheetnames:
        # Fall back to the first sheet if the expected name is missing (format change).
        sheet_name = wb.sheetnames[0]
        log.warning(
            "pinksheet.sheet_not_found",
            expected="Monthly Prices",
            using=sheet_name,
        )

    ws = wb[sheet_name]

    # --- Build column index from row 5 (commodity names) and row 6 (units) ----
    # openpyxl row/column indices are 1-based.
    rows = list(ws.iter_rows(min_row=5, max_row=6, values_only=True))
    if len(rows) < 2:
        log.warning("pinksheet.parse.no_header_rows")
        return []

    name_row, unit_row = rows[0], rows[1]

    # Map column index → (commodity_name, raw_unit, normalised_unit)
    # Skip column 0 (index 0 = column A = date column).
    col_meta: dict[int, tuple[str, str, str]] = {}
    for col_idx, commodity in enumerate(name_row):
        if col_idx == 0 or commodity is None:
            continue
        commodity_str = str(commodity).strip()
        if accepted_headers is not None and commodity_str not in accepted_headers:
            continue
        raw_unit = str(unit_row[col_idx]).strip() if unit_row[col_idx] is not None else ""
        col_meta[col_idx] = (
            commodity_str,
            raw_unit,
            _normalise_unit(raw_unit),
        )

    if not col_meta:
        log.warning("pinksheet.parse.no_matching_commodities")
        return []

    log.info(
        "pinksheet.parse.columns_found",
        commodities=list({v[0] for v in col_meta.values()}),
    )

    # --- Parse data rows (row 7 onward) ---------------------------------------
    results: list[dict] = []

    for row in ws.iter_rows(min_row=7, values_only=True):
        price_date = _parse_date(row[0])
        if price_date is None:
            if row[0] is not None:
                log.warning("pinksheet.parse.bad_date", cell=str(row[0]))
            continue

        for col_idx, (commodity, raw_unit, unit) in col_meta.items():
            if col_idx >= len(row):
                continue
            cell_val = row[col_idx]
            if cell_val is None:
                continue
            try:
                price_usd = float(cell_val)
            except (TypeError, ValueError):
                log.warning(
                    "pinksheet.parse.bad_price",
                    commodity=commodity,
                    date=str(price_date),
                    value=str(cell_val),
                )
                continue
            if price_usd <= 0:
                continue

            results.append(
                {
                    "price_date": price_date,
                    "commodity": commodity,
                    "price_usd": price_usd,
                    "price_unit": unit,
                    "raw_unit": raw_unit,
                }
            )

    log.info("pinksheet.parse.done", observations=len(results))
    return results


def _days_since_latest_observation(session: Session) -> Optional[int]:
    """Return days since the latest Pink Sheet observation in our DB.

    10.2 rename (2026-06): was ``_days_since_last_run``.  The previous
    name implied "days since the job last executed," but the function
    actually measures the gap between today and the most recent
    ``price_date`` row.  Those are different things: Pink Sheet
    publishes monthly with publication lag, so a successful run on
    March 3 2026 ingests data through Feb 2026 - the "last
    observation" is Feb 1 (publication date - lag), not March 3.

    Returns ``None`` if no rows exist yet (i.e. first run ever).  The
    caller uses this to throttle redundant downloads when our latest
    observation is fresh enough that no new monthly release can
    plausibly exist yet.
    """
    max_date = session.scalar(
        select(func.max(CommodityPrice.price_date)).where(
            CommodityPrice.source == _SOURCE
        )
    )
    if max_date is None:
        return None
    return (date.today() - max_date).days


def ingest_pink_sheet(
    session: Session,
    url: str = PINK_SHEET_URL,
    since_year: Optional[int] = None,
    min_interval_days: int = 28,
) -> dict[str, int]:
    """Download, parse, and upsert Pink Sheet prices into ``commodity_prices``.

    Args:
        session:           SQLAlchemy session. Commits internally in batches.
        url:               Override download URL (useful for tests).
        since_year:        If set, only ingest rows from this year onward.
                           The Pink Sheet goes back to ~1960; limiting to recent
                           years reduces the initial load significantly.
        min_interval_days: Skip the download when our latest observation in
                           ``commodity_prices`` is younger than this many days.
                           10.2 (2026-06): default raised 25 → 28 to cover the
                           full short-month window without false-positive runs.
                           The check is "days between today and the latest
                           ``price_date`` for this source", which is a freshness
                           gate on the DATA, not a recency throttle on the job.
                           See ``_days_since_latest_observation`` for the
                           rationale.  Pass 0 to force a run.

    Returns:
        {
            "inserted": int,
            "skipped_unknown_material": int,
            "skipped_existing": int,
            "skipped_too_recent": bool,   # True when the gate fires
        }
    """
    if min_interval_days > 0:
        # 10.2: renamed from _days_since_last_run to reflect what's
        # actually measured (latest observation in DB, not latest job
        # execution).  Variable kept locally as ``days_ago`` for
        # log-payload backwards compatibility.
        days_ago = _days_since_latest_observation(session)
        if days_ago is not None and days_ago < min_interval_days:
            log.info(
                "pinksheet.ingest.skipped_too_recent",
                days_since_latest_observation=days_ago,
                min_interval_days=min_interval_days,
            )
            return {
                "inserted": 0,
                "skipped_unknown_material": 0,
                "skipped_existing": 0,
                "skipped_too_recent": True,
            }

    raw_bytes = download_pink_sheet(url)

    # ── Build the accepted-headers set from the alias table ─────────────────
    # Every Pink Sheet header that resolves (status='ok' OR is_skipped, since
    # both are partner-curated decisions) gets through to parse_pink_sheet.
    # Headers we've never seen would resolve to 'unknown' and be excluded —
    # the warning fires after parsing, so partner sees an audit trail of
    # which Pink Sheet columns showed up unmapped.
    alias_resolver = MaterialAliasResolver(session)
    # Touch the cache once so subsequent header lookups are O(1).
    alias_resolver._ensure_loaded(_ALIAS_SOURCE_SYSTEM)
    cached = alias_resolver._cache.get(_ALIAS_SOURCE_SYSTEM, {})
    if not cached:
        log.warning(
            "pinksheet.no_aliases_seeded",
            hint=(
                "material_source_aliases is empty for source_system="
                "'worldbank_pinksheet'.  Run `bdi-ingest seed-material-aliases` "
                "first."
            ),
        )
        return {
            "inserted": 0,
            "skipped_unknown_material": 0,
            "skipped_existing": 0,
            "skipped_too_recent": False,
        }
    # Keys in the resolver cache are normalised (lower + strip).  Pink Sheet
    # headers come in mixed case verbatim, so we accept any header whose
    # normalised form is in the cache.
    accepted_headers_normalised: set[str] = set(cached.keys())

    # First pass: parse every column, then filter to accepted headers
    # (case-insensitive).  Done as a post-filter rather than inside
    # parse_pink_sheet because parse_pink_sheet treats accepted_headers as
    # exact-match for tests / general use.
    observations = parse_pink_sheet(raw_bytes)
    observations = [
        o for o in observations
        if o["commodity"].strip().lower() in accepted_headers_normalised
    ]

    if since_year is not None:
        before = len(observations)
        observations = [o for o in observations if o["price_date"].year >= since_year]
        log.info(
            "pinksheet.ingest.year_filter",
            since_year=since_year,
            before=before,
            after=len(observations),
        )

    # ── HS-prefix → hs_mapping_id resolver (per-header, cached) ─────────────
    hs_resolver = MaterialResolver(session)
    hs_mapping_cache: dict[str, Optional[int]] = {}

    def _hs_mapping_id_for(header: str) -> Optional[int]:
        """Cached HS-mapping lookup for one Pink Sheet header.

        Returns None when the header has no entry in
        ``_HEADER_TO_HS_PREFIX`` (Pink Sheet didn't disclose the form),
        or when the prefix doesn't resolve to a curated HS mapping.
        """
        if header in hs_mapping_cache:
            return hs_mapping_cache[header]
        prefix = _HEADER_TO_HS_PREFIX.get(header)
        if prefix is None:
            hs_mapping_cache[header] = None
            return None
        # Pink Sheet doesn't write RiskEventMaterial so mapping confidence
        # isn't threaded downstream — Tier 1.4 audit only affects ingesters
        # that write event-material junctions.  Unpack the 3rd element to
        # match the signature change in resolve_by_hs_code (2026-05-09).
        _material_id, hs_mapping_id, _confidence = hs_resolver.resolve_by_hs_code(prefix)
        if hs_mapping_id is None:
            log.warning(
                "pinksheet.hs_prefix_unmapped",
                header=header,
                prefix=prefix,
                hint=(
                    "HS prefix in _HEADER_TO_HS_PREFIX has no row in "
                    "hs_code_material_mappings — add it via "
                    "seed_hs_mappings._MAPPINGS."
                ),
            )
        hs_mapping_cache[header] = hs_mapping_id
        return hs_mapping_id

    # --- 10.3 (2026-06): pre-fetch existing keys to eliminate N+1 SELECT -----
    # Before 10.3 the loop did ``session.scalar(select(CommodityPrice).where(
    # material_id=..., price_date=..., ...))`` per observation.  For a first
    # full ingest (~30 commodities x 12 months x 30 years = ~10,800 rows)
    # that issued ~10,800 sequential SELECT queries before any inserts, each
    # one a DB round-trip.  Pre-fetching the entire existing key set for
    # this source in a single SELECT, then doing O(1) set-membership checks
    # in Python, converts that to 1 SELECT + ~22 batched INSERTs.
    #
    # Python set-of-tuples handles NULL semantics naturally: tuples with
    # ``None`` in the same position compare equal.  The PostgreSQL unique
    # constraint at migration 030 treats NULL as distinct (which is why
    # the old per-row check needed the ``.is_(None)`` clauses), so two
    # ``(mid, date, NULL, NULL)`` rows CAN exist in the DB; pre-fetching
    # captures whichever ones are already there.  Repeat observations
    # within the SAME run with the same key get added to the set on first
    # insert so dup-protection holds across the run.
    existing_keys: set[tuple] = set(
        session.execute(
            select(
                CommodityPrice.material_id,
                CommodityPrice.price_date,
                CommodityPrice.hs_mapping_id,
                CommodityPrice.price_form,
            ).where(CommodityPrice.source == _SOURCE)
        ).all()
    )
    log.info(
        "pinksheet.ingest.existing_keys_prefetched",
        count=len(existing_keys),
    )

    # --- Upsert loop ----------------------------------------------------------
    inserted = skipped_unknown = skipped_existing = 0
    batch_pending = 0

    for obs in observations:
        header = obs["commodity"]
        result = alias_resolver.resolve(_ALIAS_SOURCE_SYSTEM, header)
        if result.status == "skipped":
            # Partner-curated decision not to ingest — count under
            # skipped_unknown_material to keep the existing return shape
            # but log distinctly so it's visible.
            log.debug("pinksheet.ingest.alias_skipped", header=header)
            skipped_unknown += 1
            continue
        if result.status == "unknown":
            # Should not happen after the accepted_headers filter; defensive.
            log.warning("pinksheet.ingest.alias_unknown", header=header)
            skipped_unknown += 1
            continue
        material = result.material
        assert material is not None  # status == "ok"
        material_id = material.id

        # Stage attribution (NULL when Pink Sheet doesn't disclose form).
        hs_mapping_id = _hs_mapping_id_for(header)
        # Use the verbatim Pink Sheet header as the price_form descriptor
        # (UI-facing, free text).  NULL for headers without HS attribution
        # so display logic can hide stage-ambiguous rows from stage-scoped
        # views.
        price_form: Optional[str] = header if hs_mapping_id is not None else None

        # 10.3: O(1) set-membership replaces per-row SELECT.  Key matches
        # the post-migration-030 unique constraint:
        # (material_id, price_date, source, hs_mapping_id, price_form).
        # Source is constant (_SOURCE) for this whole run so it doesn't
        # need to be in the tuple key.
        key = (material_id, obs["price_date"], hs_mapping_id, price_form)
        if key in existing_keys:
            skipped_existing += 1
            continue

        session.add(
            CommodityPrice(
                material_id=material_id,
                price_date=obs["price_date"],
                price_usd=obs["price_usd"],
                price_unit=obs["price_unit"],
                source=_SOURCE,
                hs_mapping_id=hs_mapping_id,
                price_form=price_form,
                metadata_json={
                    "raw_unit": obs["raw_unit"],
                    "pink_sheet_column": header,
                },
            )
        )
        # 10.3: track the just-inserted key so a duplicate observation
        # within the SAME run (e.g. parser hiccup that yields the same
        # (commodity, date) twice) gets skipped on the second occurrence
        # rather than producing an extra row that would later collide on
        # next-run replay.
        existing_keys.add(key)
        inserted += 1
        batch_pending += 1

        if batch_pending >= _BATCH_SIZE:
            session.flush()
            batch_pending = 0
            log.debug("pinksheet.ingest.batch_flush", inserted_so_far=inserted)

    session.commit()

    log.info(
        "pinksheet.ingest.done",
        inserted=inserted,
        skipped_unknown_material=skipped_unknown,
        skipped_existing=skipped_existing,
    )
    return {
        "inserted": inserted,
        "skipped_unknown_material": skipped_unknown,
        "skipped_existing": skipped_existing,
        "skipped_too_recent": False,
    }
