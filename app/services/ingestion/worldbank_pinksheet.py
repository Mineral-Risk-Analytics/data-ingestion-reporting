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

Re-running this ingestion is safe. The unique constraint
``(material_id, price_date, source)`` on ``commodity_prices`` is the idempotency
guard — existing rows are never overwritten.
"""

from __future__ import annotations

import io
from datetime import date, datetime
from typing import Optional

import httpx
import structlog
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.supply import CommodityPrice, Material

log = structlog.get_logger(__name__)

PINK_SHEET_URL = (
    "https://thedocs.worldbank.org/en/doc/74e8be41ceb20fa0da750cda2f6b9e4e-0050012026"
    "/related/CMO-Historical-Data-Monthly.xlsx"
)

_SOURCE = "worldbank_pink_sheet"

# Maps Pink Sheet column headers → materials.canonical_name values.
# Only commodities present in this dict are ingested; all others are silently skipped.
#
# Coverage notes:
#   - Tin, Zinc: LME-traded industrial metals used in battery/EV components and solder.
#   - Platinum: Used as the benchmark price for "platinum-group metals". Palladium is
#     excluded because both share the same material_id and the unique constraint on
#     (material_id, price_date, source) would cause silent data loss.
#   - Many battery-critical materials (REEs, Silicon, Gallium, Germanium, Vanadium,
#     Tungsten, Niobium, Tantalum, Fluorspar, Antimony) have no public benchmark price
#     and cannot be sourced from the Pink Sheet. These score flat zero on the financial
#     pillar, which is a data-gap ambiguity rather than a true low-risk signal.
_COMMODITY_TO_MATERIAL: dict[str, str] = {
    "Cobalt": "Cobalt",
    "Copper": "Copper",
    "Nickel": "Nickel",
    "Aluminum": "Aluminum",
    "Aluminium": "Aluminum",  # Pink Sheet uses British spelling in some editions
    "Lithium carbonate, battery grade": "Lithium",
    "Lithium": "Lithium",
    "Manganese ore": "Manganese",
    "Manganese": "Manganese",
    "Graphite": "Natural Graphite",
    "Natural graphite": "Natural Graphite",
    # --- Added: industrial metals with direct Pink Sheet columns ----------------
    "Tin": "Tin",
    "Tin, LME": "Tin",          # alternate header seen in some Pink Sheet editions
    "Zinc": "Zinc",
    # Platinum used as the representative benchmark for the PGM group.
    # Palladium is intentionally excluded: both share the same material_id
    # ("Platinum-Group Metals") and the unique constraint on (material_id,
    # price_date, source) would cause one to silently overwrite the other.
    "Platinum": "Platinum-Group Metals",
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


def parse_pink_sheet(raw_bytes: bytes) -> list[dict]:
    """Parse the Pink Sheet Excel bytes into a list of price observation dicts.

    Each returned dict has:
        price_date  (datetime.date)
        commodity   (str — the Pink Sheet column header, e.g. "Cobalt")
        price_usd   (float)
        price_unit  (str — normalised: "per_mt", "per_kg", "per_troy_oz", or raw)
        raw_unit    (str — original unit string from row 6)

    Rows where ``price_usd`` is ``None``, empty, or not a valid positive float
    are skipped. Rows where the date cannot be parsed are skipped with a warning.
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
        if commodity_str not in _COMMODITY_TO_MATERIAL:
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
    observations = parse_pink_sheet(raw_bytes)

    if since_year is not None:
        before = len(observations)
        observations = [o for o in observations if o["price_date"].year >= since_year]
        log.info(
            "pinksheet.ingest.year_filter",
            since_year=since_year,
            before=before,
            after=len(observations),
        )

    # --- Build material name → id cache (avoids N+1 queries) -----------------
    needed_canonical = {
        _COMMODITY_TO_MATERIAL[o["commodity"]]
        for o in observations
        if o["commodity"] in _COMMODITY_TO_MATERIAL
    }
    material_id_cache: dict[str, int] = {}
    for name in needed_canonical:
        row = session.scalar(select(Material).where(Material.canonical_name == name))
        if row is not None:
            material_id_cache[name] = row.id

    log.info(
        "pinksheet.ingest.material_cache",
        resolved=len(material_id_cache),
        requested=len(needed_canonical),
    )

    # --- Upsert loop ----------------------------------------------------------
    inserted = skipped_unknown = skipped_existing = 0
    batch_pending = 0

    for obs in observations:
        canonical = _COMMODITY_TO_MATERIAL.get(obs["commodity"])
        if canonical is None:
            # Should not happen after parse_pink_sheet filtering, but be safe.
            skipped_unknown += 1
            continue

        material_id = material_id_cache.get(canonical)
        if material_id is None:
            log.warning(
                "pinksheet.ingest.unknown_material",
                commodity=obs["commodity"],
                canonical=canonical,
            )
            skipped_unknown += 1
            continue

        # Check for existing row to keep the skip counter accurate.
        exists = session.scalar(
            select(CommodityPrice).where(
                CommodityPrice.material_id == material_id,
                CommodityPrice.price_date == obs["price_date"],
                CommodityPrice.source == _SOURCE,
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
                metadata_json={
                    "raw_unit": obs["raw_unit"],
                    "pink_sheet_column": obs["commodity"],
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
