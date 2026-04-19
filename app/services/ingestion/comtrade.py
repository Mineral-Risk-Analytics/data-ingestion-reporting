"""UN Comtrade annual export trade flow ingestion.

Fetches bilateral export data (flowCode=X, partnerCode=0 = world aggregate)
from the UN Comtrade API v1 and upserts normalised rows into ``trade_flows``.

Data links to the Material Concentration pillar (30% weight) of the supply
chain risk scoring model — which countries control what share of exports for
battery-critical materials.

Important notes
---------------
- **Data lag**: Comtrade annual data for year Y is typically available by
  March/April of Y+1. Do not query the current calendar year — it will return
  partial or no data.

- **Export vs import**: This script queries exports (flowCode=X) from producing
  countries. Import data (flowCode=M) from consuming countries gives a different
  view. Export data from producers is more useful for concentration scoring
  because it directly reflects supply availability. Import data can be added in
  a later phase.

- **World partner (partnerCode=0)**: Using partner=0 gives total exports to all
  destinations, which is what we need for market share calculations. Bilateral
  flows (e.g. CN→US specifically) require more API calls and are better suited
  to a later phase.

- **HS prefix granularity**: Querying by 4-digit prefix returns all 6-digit
  sub-codes. A single prefix may return many commodity lines (e.g. 8507 covers
  all battery types). The ``material_id`` mapping via ``hs_code_material_mappings``
  narrows these to specific materials.

- **Idempotency**: Duplicate detection is done via ``SourceDocument.external_id``.
  If a document already exists for a (reporter, hs_prefix, year) combination,
  the entire batch is skipped. No unique constraint on ``trade_flows`` — the
  source document check is the guard.

- **Commit strategy**: Each (reporter × hs_prefix × year) combination is
  committed independently. This returns the DB connection to the pool after
  every batch, so Neon's serverless connection timeout cannot kill a run that
  spans many slow API calls. A failed batch is logged and skipped; successfully
  committed batches are never re-processed.

- **API key**: Must be set via ``COMTRADE_API_KEY`` in the environment.
  The script raises immediately if the key is empty.
"""

from __future__ import annotations

import time
from typing import Optional

import httpx
import structlog
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.documents import SourceDocument
from app.models.source import Source
from app.models.supply import HsCodeMaterialMapping, TradeFlow
from app.models.supply_chain_context import SupplyChainContext

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Comtrade numeric reporter codes for key battery supply chain actors.
REPORTER_COUNTRIES: dict[str, int] = {
    "CN": 156,  # China — graphite, lithium processing, cells
    "CL": 152,  # Chile — lithium
    "AU": 36,   # Australia — lithium, nickel
    "CD": 180,  # DRC — cobalt
    "ID": 360,  # Indonesia — nickel
    "RU": 643,  # Russia — nickel
    "US": 842,  # United States
    "JP": 392,  # Japan
    "KR": 410,  # South Korea
    "DE": 276,  # Germany
    "CA": 124,  # Canada
    "ZA": 710,  # South Africa — manganese, platinum group
    "PH": 608,  # Philippines — nickel
    "MZ": 508,  # Mozambique — graphite
}

# Reverse map: Comtrade numeric code → ISO2. Used to translate partner codes.
_CODE_TO_ISO2: dict[int, str] = {v: k for k, v in REPORTER_COUNTRIES.items()}

# Comtrade uses 0 for "all partners" (world aggregate).
WORLD_PARTNER_CODE = 0

_SOURCE_NAME = "UN Comtrade API"
_SOURCE_TYPE = "comtrade"
_BATCH_SIZE = 500

# Hard wall-clock timeout for a single Comtrade API call.
# httpx's timeout= is a per-socket-read limit, not a total-response limit, so
# a slow-streaming 200 response can stall for many minutes. Setting a low read
# timeout (60 s) ensures the session is never blocked long enough for Neon's
# ~5-minute idle-connection killer to fire.
_API_TIMEOUT = httpx.Timeout(connect=15.0, read=60.0, write=15.0, pool=15.0)

# 429 retry settings
_MAX_RETRIES = 3
_RETRY_BASE_DELAY = 5.0  # seconds; doubles on each retry


# ---------------------------------------------------------------------------
# API client
# ---------------------------------------------------------------------------

def _comtrade_get(
    path: str,
    params: dict,
    api_key: str,
) -> dict:
    """Make one authenticated GET request to the Comtrade API with retry.

    Authentication uses the ``Ocp-Apim-Subscription-Key`` header (Azure API
    Management gateway in front of the Comtrade service).

    Retries up to ``_MAX_RETRIES`` times on HTTP 429, with exponential backoff.
    Uses ``_API_TIMEOUT`` to enforce a 60-second read limit so slow-streaming
    responses never stall the database connection.

    Raises:
        httpx.HTTPStatusError: on non-2xx HTTP response after all retries.
        ValueError: if the response body contains a top-level ``"error"`` key
            (Comtrade returns HTTP 200 with an error dict on bad parameters).
    """
    headers = {"Ocp-Apim-Subscription-Key": api_key}
    last_exc: Exception | None = None

    for attempt in range(_MAX_RETRIES):
        try:
            response = httpx.get(path, params=params, headers=headers, timeout=_API_TIMEOUT)
            if response.status_code == 429:
                delay = _RETRY_BASE_DELAY * (2 ** attempt)
                log.warning(
                    "comtrade.rate_limited",
                    attempt=attempt + 1,
                    retry_in_seconds=delay,
                )
                time.sleep(delay)
                last_exc = httpx.HTTPStatusError(
                    f"429 Too Many Requests",
                    request=response.request,
                    response=response,
                )
                continue
            response.raise_for_status()
            data = response.json()
            if isinstance(data, dict) and data.get("error"):
                raise ValueError(f"Comtrade API error: {data['error']}")
            return data
        except (httpx.ReadTimeout, httpx.ConnectTimeout) as exc:
            delay = _RETRY_BASE_DELAY * (2 ** attempt)
            log.warning(
                "comtrade.timeout",
                attempt=attempt + 1,
                retry_in_seconds=delay,
                error=str(exc),
            )
            time.sleep(delay)
            last_exc = exc

    raise last_exc  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Query builder / fetcher
# ---------------------------------------------------------------------------

def fetch_annual_exports(
    reporter_iso2: str,
    reporter_code: int,
    hs_prefix: str,
    year: int,
    api_key: str,
    base_url: str,
) -> list[dict]:
    """Fetch annual export records for one reporter × HS prefix × year.

    Queries flowCode=X (exports), partnerCode=0 (world aggregate).

    Returns the list of data rows from the Comtrade response, or [] if no data.
    Each row is a raw dict from the API ``data`` array.
    """
    endpoint = f"{base_url}/C/A/HS"
    params = {
        "reporterCode": reporter_code,
        "partnerCode": WORLD_PARTNER_CODE,
        "period": year,
        "cmdCode": hs_prefix,
        "flowCode": "X",
        "maxRecords": 100000,
        "includeDesc": "true",
    }

    log.info(
        "comtrade.fetch",
        reporter=reporter_iso2,
        hs_prefix=hs_prefix,
        year=year,
    )

    raw = _comtrade_get(endpoint, params, api_key)
    rows = raw.get("data") or []
    return rows  # noqa: RET504


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------

def parse_comtrade_rows(
    rows: list[dict],
    reporter_iso2: str,
    hs_prefix: str,
    year: int,
) -> list[dict]:
    """Normalise raw Comtrade API rows into TradeFlow insert dicts.

    Returns dicts with keys matching TradeFlow columns. Skips rows where both
    ``trade_value_usd`` and ``quantity`` are None or zero.

    Converts numeric ``partnerCode`` to ISO2 using the reverse of
    ``REPORTER_COUNTRIES``; falls back to ``str(partnerCode)`` for unknown codes.
    ``partnerCode=0`` is mapped to the string ``"WLD"``.
    """
    results: list[dict] = []

    for row in rows:
        trade_value = row.get("primaryValue")
        net_wgt = row.get("netWgt")

        try:
            trade_value_usd = float(trade_value) if trade_value is not None else None
        except (TypeError, ValueError):
            trade_value_usd = None

        try:
            quantity = float(net_wgt) if net_wgt is not None else None
        except (TypeError, ValueError):
            quantity = None

        # Skip rows with no meaningful data
        if (trade_value_usd is None or trade_value_usd == 0) and (
            quantity is None or quantity == 0
        ):
            continue

        # Resolve partner code → ISO2
        partner_code = row.get("partnerCode")
        if partner_code == 0 or partner_code == "0":
            partner_country = "WLD"
        else:
            try:
                partner_country = _CODE_TO_ISO2.get(int(partner_code), str(partner_code))
            except (TypeError, ValueError):
                partner_country = str(partner_code) if partner_code is not None else "UNK"

        results.append(
            {
                "period": str(year),
                "reporter_country": reporter_iso2,
                "partner_country": partner_country,
                "hs_code": str(row.get("cmdCode") or ""),
                "hs_description": row.get("cmdDesc") or None,
                "import_export_flag": "export",
                "quantity": quantity,
                "quantity_unit": "kg" if quantity is not None else None,
                "trade_value_usd": trade_value_usd,
                "metadata_json": {
                    "comtrade_flow_code": "X",
                    "comtrade_period": year,
                    "hs_prefix_queried": hs_prefix,
                },
            }
        )

    return results


# ---------------------------------------------------------------------------
# Source / SourceDocument helpers
# ---------------------------------------------------------------------------

def _get_or_create_comtrade_source(session: Session) -> int:
    """Get or create the Source row for UN Comtrade. Returns source.id."""
    settings = get_settings()
    existing = session.scalar(select(Source).where(Source.name == _SOURCE_NAME))
    if existing is not None:
        return existing.id

    source = Source(
        name=_SOURCE_NAME,
        source_type=_SOURCE_TYPE,
        phase="1",
        is_active=True,
        config_json={
            "base_url": settings.comtrade_base_url,
            "freq": "A",
            "classification": "HS",
        },
    )
    session.add(source)
    session.flush()
    log.info("comtrade.source_created", source_id=source.id)
    return source.id


def _external_id(reporter_iso2: str, hs_prefix: str, year: int) -> str:
    return f"comtrade_C_A_HS_{hs_prefix}_{reporter_iso2}_{year}"


def _create_source_document(
    session: Session,
    source_id: int,
    reporter_iso2: str,
    hs_prefix: str,
    year: int,
    row_count: int,
) -> int:
    """Create a SourceDocument for one API call batch. Returns source_document.id.

    If a document with this external_id already exists (source_id + external_id
    unique constraint), returns the existing row's id without inserting a duplicate.
    """
    ext_id = _external_id(reporter_iso2, hs_prefix, year)
    existing = session.scalar(
        select(SourceDocument).where(
            SourceDocument.source_id == source_id,
            SourceDocument.external_id == ext_id,
        )
    )
    if existing is not None:
        return existing.id

    doc = SourceDocument(
        source_id=source_id,
        external_id=ext_id,
        title=f"UN Comtrade: {reporter_iso2} HS {hs_prefix} exports {year}",
        document_type="trade_data",
        metadata_json={
            "reporter": reporter_iso2,
            "hs_prefix": hs_prefix,
            "year": year,
            "row_count": row_count,
        },
    )
    session.add(doc)
    session.flush()
    return doc.id


# ---------------------------------------------------------------------------
# HS → material mapping
# ---------------------------------------------------------------------------

def _build_hs_material_map(session: Session) -> dict[str, Optional[int]]:
    """Return a dict of hs_code_prefix → material_id from hs_code_material_mappings.

    Covers 4-digit prefixes. For a given 6-digit hs_code from Comtrade, callers
    should check whether any key in the returned dict is a prefix of the code.
    Returns None for unmatched codes — callers should still insert the TradeFlow
    row with material_id=None.
    """
    rows = session.scalars(select(HsCodeMaterialMapping)).all()
    return {r.hs_code_prefix: r.material_id for r in rows}


def _resolve_material_id(
    hs_code: str,
    hs_material_map: dict[str, Optional[int]],
) -> Optional[int]:
    """Return material_id for a 6-digit hs_code, or None if no mapping exists."""
    for prefix, material_id in hs_material_map.items():
        if hs_code.startswith(prefix):
            return material_id
    return None


# ---------------------------------------------------------------------------
# Main ingest function
# ---------------------------------------------------------------------------

def ingest_comtrade(
    session: Session,
    years: list[int],
    reporters: Optional[dict[str, int]] = None,
    hs_prefixes: Optional[list[str]] = None,
    api_key: Optional[str] = None,
) -> dict[str, int]:
    """Fetch and ingest UN Comtrade annual export data into trade_flows.

    Args:
        session:     SQLAlchemy session. Commits internally at the end.
        years:       List of years to fetch (e.g. [2021, 2022, 2023]).
        reporters:   Override dict of ISO2→comtrade_code. Defaults to
                     REPORTER_COUNTRIES.
        hs_prefixes: Override HS code prefixes. Defaults to reading from
                     supply_chain_contexts WHERE slug='ev_battery'.
        api_key:     Override API key. Defaults to settings.comtrade_api_key.

    Returns:
        {
            "inserted": int,
            "skipped_existing_doc": int,
            "skipped_empty_response": int,
            "api_calls_made": int,
            "errors": int,
        }

    Raises:
        ValueError: If no API key is configured.
    """
    settings = get_settings()

    resolved_key = api_key or settings.comtrade_api_key
    if not resolved_key:
        raise ValueError(
            "COMTRADE_API_KEY is not set. Add it to your .env file or pass "
            "api_key= directly. The UN Comtrade API requires a subscription key."
        )

    resolved_reporters = reporters if reporters is not None else REPORTER_COUNTRIES

    # --- Resolve HS prefixes from supply_chain_contexts if not provided ------
    if hs_prefixes is None:
        ctx = session.scalar(
            select(SupplyChainContext).where(SupplyChainContext.slug == "ev_battery")
        )
        if ctx is None or not ctx.relevant_hs_code_prefixes:
            raise ValueError(
                "No HS code prefixes found. Ensure supply_chain_contexts has a row "
                "with slug='ev_battery' and relevant_hs_code_prefixes set, "
                "or pass hs_prefixes= explicitly."
            )
        resolved_prefixes: list[str] = list(ctx.relevant_hs_code_prefixes)
    else:
        resolved_prefixes = list(hs_prefixes)

    log.info(
        "comtrade.ingest.start",
        years=years,
        reporters=list(resolved_reporters.keys()),
        hs_prefixes=resolved_prefixes,
    )

    # --- One-time setup ------------------------------------------------------
    source_id = _get_or_create_comtrade_source(session)
    hs_material_map = _build_hs_material_map(session)

    inserted = 0
    skipped_existing_doc = 0
    skipped_empty_response = 0
    api_calls_made = 0
    errors = 0

    # --- Outer loop: year × reporter × hs_prefix ----------------------------
    for year in years:
        for iso2, reporter_code in resolved_reporters.items():
            for hs_prefix in resolved_prefixes:
                ext_id = _external_id(iso2, hs_prefix, year)

                # Idempotency check — skip if already ingested.
                # Each check gets a fresh connection checkout so pool_pre_ping
                # can detect and replace any stale connection before we use it.
                try:
                    existing_doc = session.scalar(
                        select(SourceDocument).where(
                            SourceDocument.source_id == source_id,
                            SourceDocument.external_id == ext_id,
                        )
                    )
                except OperationalError as db_exc:
                    log.error(
                        "comtrade.db_error_idempotency_check",
                        reporter=iso2,
                        hs_prefix=hs_prefix,
                        year=year,
                        error=str(db_exc),
                    )
                    session.rollback()
                    errors += 1
                    continue

                if existing_doc is not None:
                    log.debug(
                        "comtrade.skip_existing_doc",
                        reporter=iso2,
                        hs_prefix=hs_prefix,
                        year=year,
                    )
                    skipped_existing_doc += 1
                    continue

                # Rate-limit sleep before every API call.
                # The session is NOT holding an open transaction during this
                # sleep — the previous iteration committed, returning the
                # connection to the pool. Pool pre-ping will validate it when
                # the next DB call checks it out.
                time.sleep(settings.comtrade_rate_limit_delay)

                # --- API call ------------------------------------------------
                try:
                    raw_rows = fetch_annual_exports(
                        reporter_iso2=iso2,
                        reporter_code=reporter_code,
                        hs_prefix=hs_prefix,
                        year=year,
                        api_key=resolved_key,
                        base_url=settings.comtrade_base_url,
                    )
                    api_calls_made += 1
                except (httpx.HTTPStatusError, httpx.TimeoutException, ValueError) as exc:
                    log.warning(
                        "comtrade.api_error",
                        reporter=iso2,
                        hs_prefix=hs_prefix,
                        year=year,
                        error=str(exc),
                    )
                    errors += 1
                    continue

                if not raw_rows:
                    log.debug(
                        "comtrade.empty_response",
                        reporter=iso2,
                        hs_prefix=hs_prefix,
                        year=year,
                    )
                    skipped_empty_response += 1
                    continue

                # --- Parse and insert, then commit immediately ---------------
                # Committing here (rather than once at the end) returns the
                # connection to the pool after each batch. This prevents Neon's
                # 5-minute idle-connection timeout from killing a run that spans
                # many slow API calls.
                normalised = parse_comtrade_rows(raw_rows, iso2, hs_prefix, year)

                try:
                    source_doc_id = _create_source_document(
                        session,
                        source_id=source_id,
                        reporter_iso2=iso2,
                        hs_prefix=hs_prefix,
                        year=year,
                        row_count=len(normalised),
                    )

                    batch_added = 0
                    for row_dict in normalised:
                        material_id = _resolve_material_id(
                            row_dict.get("hs_code") or "", hs_material_map
                        )
                        if material_id is None:
                            log.debug(
                                "comtrade.no_material_mapping",
                                hs_code=row_dict.get("hs_code"),
                            )

                        session.add(
                            TradeFlow(
                                source_document_id=source_doc_id,
                                material_id=material_id,
                                **row_dict,
                            )
                        )
                        batch_added += 1

                        if batch_added % _BATCH_SIZE == 0:
                            session.flush()

                    # Commit every (reporter × hs_prefix × year) batch.
                    session.commit()
                    inserted += batch_added

                    log.info(
                        "comtrade.batch_done",
                        reporter=iso2,
                        hs_prefix=hs_prefix,
                        year=year,
                        rows=batch_added,
                    )

                except OperationalError as db_exc:
                    session.rollback()
                    log.error(
                        "comtrade.db_error_insert",
                        reporter=iso2,
                        hs_prefix=hs_prefix,
                        year=year,
                        error=str(db_exc),
                    )
                    errors += 1

    log.info(
        "comtrade.ingest.done",
        inserted=inserted,
        skipped_existing_doc=skipped_existing_doc,
        skipped_empty_response=skipped_empty_response,
        api_calls_made=api_calls_made,
        errors=errors,
    )
    return {
        "inserted": inserted,
        "skipped_existing_doc": skipped_existing_doc,
        "skipped_empty_response": skipped_empty_response,
        "api_calls_made": api_calls_made,
        "errors": errors,
    }
