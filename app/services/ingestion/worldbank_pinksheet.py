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
from datetime import date, datetime
from typing import Optional

import httpx
import structlog
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.supply import CommodityPrice
from app.services.ingestion.material_resolver import MaterialAliasResolver
from app.services.ingestion.normalizers.material_resolver import (
    MaterialResolver,
)

log = structlog.get_logger(__name__)

PINK_SHEET_URL = (
    "https://thedocs.worldbank.org/en/doc/74e8be41ceb20fa0da750cda2f6b9e4e-0050012026"
    "/related/CMO-Historical-Data-Monthly.xlsx"
)

_SOURCE = "worldbank_pink_sheet"
_ALIAS_SOURCE_SYSTEM = "worldbank_pinksheet"

# ── Stage attribution per Pink Sheet header ─────────────────────────────────
# Maps ``Pink Sheet column header → 6-digit HS prefix`` for headers whose
# trading basis is unambiguous.  Resolution to ``hs_mapping_id`` happens
# at ingest time via ``MaterialResolver.resolve_by_hs_code`` against the
# curated rows in ``hs_code_material_mappings``.
#
# Headers absent from this dict get ``hs_mapping_id=NULL`` and
# ``price_form=NULL`` — Pink Sheet didn't disclose the form, so we don't
# guess.  Examples: bare "Graphite", bare "Manganese".
#
# LME-convention basis (refined stage) is documented because the Pink
# Sheet column header itself is just the metal name; the form is
# implicit in the exchange spec.  This dict is the only place we encode
# that convention.
_HEADER_TO_HS_PREFIX: dict[str, str] = {
    # Stage-explicit headers
    "Lithium carbonate, battery grade": "283691",  # battery_grade — Li2CO3
    "Manganese ore":                    "2602",    # ore — manganese ores and concentrates
    # LME / exchange convention → refined stage
    "Cobalt":     "810520",  # cobalt unwrought (LME cathode 99.8%)
    "Copper":     "740311",  # copper cathodes (LME Grade A)
    "Nickel":     "750210",  # nickel unwrought, not alloyed (LME Class 1)
    "Aluminum":   "760110",  # aluminium unwrought, not alloyed (LME primary ingot)
    "Aluminium":  "760110",  # British-spelling header in some editions
    "Tin":        "800110",  # tin unwrought, not alloyed (LME 99.85%)
    "Tin, LME":   "800110",
    "Zinc":       "790111",  # zinc unwrought, not alloyed (LME SHG 99.95%)
    "Platinum":   "711011",  # platinum unwrought (LBMA AM)
    # Intentionally absent (form not disclosed, no honest stage attribution):
    #   "Graphite", "Natural graphite", "Manganese" (without "ore"), "Lithium" (bare)
}

_UNIT_MAP: dict[str, str] = {
    "$/mt": "per_mt",
    "$/kg": "per_kg",
    "$/troy oz": "per_troy_oz",
}

_BATCH_SIZE = 500


def download_pink_sheet(url: str = PINK_SHEET_URL, timeout: int = 60) -> bytes:
    """Download the Pink Sheet Excel file and return raw bytes.

    Streams the response so the full file is not held as a single allocation
    until all chunks have been received.

    Raises:
        httpx.HTTPStatusError: on non-2xx HTTP response.
        httpx.TimeoutException: if the download exceeds ``timeout`` seconds.
    """
    log.info("pinksheet.download.start", url=url)
    chunks: list[bytes] = []

    with httpx.stream("GET", url, timeout=timeout, follow_redirects=True) as response:
        response.raise_for_status()
        for chunk in response.iter_bytes():
            chunks.append(chunk)

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


def _days_since_last_run(session: Session) -> Optional[int]:
    """Return days since the most recent Pink Sheet price row was inserted.

    Uses ``MAX(price_date)`` from ``commodity_prices`` for this source as a
    proxy for the last successful run. Returns ``None`` if no rows exist yet
    (i.e. first run).
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
    min_interval_days: int = 25,
) -> dict[str, int]:
    """Download, parse, and upsert Pink Sheet prices into ``commodity_prices``.

    Args:
        session:           SQLAlchemy session. Commits internally in batches.
        url:               Override download URL (useful for tests).
        since_year:        If set, only ingest rows from this year onward.
                           The Pink Sheet goes back to ~1960; limiting to recent
                           years reduces the initial load significantly.
        min_interval_days: Skip the download if the most recent price row is
                           younger than this many days. Default 25 — slightly
                           less than a month so the scheduled job always catches
                           the new monthly release. Pass 0 to force a run.

    Returns:
        {
            "inserted": int,
            "skipped_unknown_material": int,
            "skipped_existing": int,
            "skipped_too_recent": bool,   # True when the gate fires
        }
    """
    if min_interval_days > 0:
        days_ago = _days_since_last_run(session)
        if days_ago is not None and days_ago < min_interval_days:
            log.info(
                "pinksheet.ingest.skipped_too_recent",
                days_since_last_run=days_ago,
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
        _material_id, hs_mapping_id = hs_resolver.resolve_by_hs_code(prefix)
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

        # Existing-row check matches the post-migration-030 unique
        # constraint: (material_id, price_date, source, hs_mapping_id,
        # price_form).  PG treats NULL as distinct in unique constraints,
        # so historical rows with NULL hs_mapping_id won't conflict.
        exists = session.scalar(
            select(CommodityPrice).where(
                CommodityPrice.material_id == material_id,
                CommodityPrice.price_date == obs["price_date"],
                CommodityPrice.source == _SOURCE,
                CommodityPrice.hs_mapping_id.is_(hs_mapping_id) if hs_mapping_id is None
                    else CommodityPrice.hs_mapping_id == hs_mapping_id,
                CommodityPrice.price_form.is_(price_form) if price_form is None
                    else CommodityPrice.price_form == price_form,
            )
        )
        if exists is not None:
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
