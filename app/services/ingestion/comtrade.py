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
from app.models.country import Country
from app.models.documents import SourceDocument
from app.models.source import Source
from app.models.supply import HsCodeMaterialMapping, TradeFlow
from app.models.supply_chain_context import SupplyChainContext

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Hardcoded fallback reporter / consumer country sets.
# These are used ONLY when the ``countries`` table has not been seeded
# (e.g. a fresh environment before ``bdi-ingest seed-countries`` has run).
# Once seeded, ``get_reporter_countries()`` and ``get_consumer_countries()``
# query the DB instead of reading these dicts.
#
# To add or remove a country from Comtrade ingestion, update the
# ``is_major_producer`` / ``is_major_consumer`` flags via ``seed-countries``
# rather than editing these dicts.
REPORTER_COUNTRIES: dict[str, int] = {
    "CN": 156,  "CL": 152,  "AU": 36,   "CD": 180,  "ID": 360,
    "RU": 643,  "US": 842,  "JP": 392,  "KR": 410,  "DE": 276,
    "CA": 124,  "ZA": 710,  "PH": 608,  "MZ": 508,
}
CONSUMER_COUNTRIES: dict[str, int] = {
    "US": 842,  "JP": 392,  "KR": 410,  "DE": 276,
    "FR": 251,  "GB": 826,  "BE": 56,   "IN": 699,
}

# Reverse map: Comtrade numeric code → ISO2. Used to translate partner codes.
# Re-built at runtime by get_reporter_countries() when the DB is available.
_CODE_TO_ISO2: dict[int, str] = {v: k for k, v in REPORTER_COUNTRIES.items()}

# Comtrade uses 0 for "all partners" (world aggregate).
WORLD_PARTNER_CODE = 0


def get_reporter_countries(session: Session) -> dict[str, int]:
    """Return ISO2 → Comtrade code map for major-producer countries.

    Queries the ``countries`` table for rows where ``is_major_producer = True``
    and ``comtrade_code IS NOT NULL``.  Falls back to the hardcoded
    ``REPORTER_COUNTRIES`` dict if the table is empty (not yet seeded).
    """
    rows = session.scalars(
        select(Country)
        .where(Country.is_major_producer.is_(True))
        .where(Country.comtrade_code.is_not(None))
    ).all()
    if not rows:
        log.warning(
            "comtrade.get_reporter_countries.fallback",
            hint="Run 'bdi-ingest seed-countries' to populate the countries table.",
        )
        return dict(REPORTER_COUNTRIES)
    return {r.iso2: r.comtrade_code for r in rows}


def get_consumer_countries(session: Session) -> dict[str, int]:
    """Return ISO2 → Comtrade code map for major-consumer countries.

    Queries the ``countries`` table for rows where ``is_major_consumer = True``
    and ``comtrade_code IS NOT NULL``.  Falls back to the hardcoded
    ``CONSUMER_COUNTRIES`` dict if the table is empty (not yet seeded).
    """
    rows = session.scalars(
        select(Country)
        .where(Country.is_major_consumer.is_(True))
        .where(Country.comtrade_code.is_not(None))
    ).all()
    if not rows:
        log.warning(
            "comtrade.get_consumer_countries.fallback",
            hint="Run 'bdi-ingest seed-countries' to populate the countries table.",
        )
        return dict(CONSUMER_COUNTRIES)
    return {r.iso2: r.comtrade_code for r in rows}

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
    flow_code: str = "X",
) -> list[dict]:
    """Fetch annual trade records for one reporter × HS prefix × year.

    Args:
        flow_code: ``"X"`` for exports (default), ``"M"`` for imports.
                   Both use ``partnerCode=0`` (world aggregate).

    Returns the list of data rows from the Comtrade response, or [] if no data.
    Each row is a raw dict from the API ``data`` array.
    """
    endpoint = f"{base_url}/C/A/HS"
    params = {
        "reporterCode": reporter_code,
        "partnerCode": WORLD_PARTNER_CODE,
        "period": year,
        "cmdCode": hs_prefix,
        "flowCode": flow_code,
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
    flow_code: str = "X",
) -> list[dict]:
    """Normalise raw Comtrade API rows into TradeFlow insert dicts.

    Returns dicts with keys matching TradeFlow columns. Skips rows where both
    ``trade_value_usd`` and ``quantity`` are None or zero.

    Converts numeric ``partnerCode`` to ISO2 using the reverse of
    ``REPORTER_COUNTRIES``; falls back to ``str(partnerCode)`` for unknown codes.
    ``partnerCode=0`` is mapped to the string ``"WLD"``.

    ``flow_code`` sets ``import_export_flag``: ``"X"`` → ``"export"``,
    ``"M"`` → ``"import"``.  Defaults to ``"X"`` for backward compatibility.
    """
    _FLOW_FLAG = {"X": "export", "M": "import"}
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
                "import_export_flag": _FLOW_FLAG.get(flow_code, flow_code),
                "quantity": quantity,
                "quantity_unit": "kg" if quantity is not None else None,
                "trade_value_usd": trade_value_usd,
                "metadata_json": {
                    "comtrade_flow_code": flow_code,
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


def _external_id(reporter_iso2: str, hs_prefix: str, year: int, flow_code: str = "X") -> str:
    # flow_code included so export and import runs for the same reporter/HS/year
    # produce distinct source_documents and don't skip each other's idempotency check.
    return f"comtrade_{flow_code}_A_HS_{hs_prefix}_{reporter_iso2}_{year}"


def _create_source_document(
    session: Session,
    source_id: int,
    reporter_iso2: str,
    hs_prefix: str,
    year: int,
    row_count: int,
    flow_code: str = "X",
) -> int:
    """Create a SourceDocument for one API call batch. Returns source_document.id.

    If a document with this external_id already exists (source_id + external_id
    unique constraint), returns the existing row's id without inserting a duplicate.
    """
    ext_id = _external_id(reporter_iso2, hs_prefix, year, flow_code=flow_code)
    existing = session.scalar(
        select(SourceDocument).where(
            SourceDocument.source_id == source_id,
            SourceDocument.external_id == ext_id,
        )
    )
    if existing is not None:
        return existing.id

    direction = "imports" if flow_code == "M" else "exports"
    doc = SourceDocument(
        source_id=source_id,
        external_id=ext_id,
        title=f"UN Comtrade: {reporter_iso2} HS {hs_prefix} {direction} {year}",
        document_type="trade_data",
        metadata_json={
            "reporter": reporter_iso2,
            "hs_prefix": hs_prefix,
            "year": year,
            "flow_code": flow_code,
            "row_count": row_count,
        },
    )
    session.add(doc)
    session.flush()
    return doc.id


# ---------------------------------------------------------------------------
# HS → material mapping
# ---------------------------------------------------------------------------

def _build_hs_material_map(
    session: Session,
) -> dict[str, list[tuple[int, float]]]:
    """Return all hs_code_material_mappings keyed by normalised prefix.

    Returns ``{prefix: [(material_id, confidence), ...]}``.  A single prefix
    may resolve to multiple materials (e.g. "2615" covers Vanadium, Niobium,
    Tantalum, Zirconium).  ``_resolve_material_id`` uses confidence scores to
    disambiguate or explicitly returns None for ambiguous cases rather than
    picking arbitrarily.

    Prefixes are stored without dots so comparison against raw Comtrade HS
    codes (which also have no dots) is straightforward.
    """
    rows = session.scalars(select(HsCodeMaterialMapping)).all()
    result: dict[str, list[tuple[int, float]]] = {}
    for r in rows:
        prefix = r.hs_code_prefix.replace(".", "")
        result.setdefault(prefix, []).append((r.material_id, r.confidence))
    return result


def _resolve_material_id(
    hs_code: str,
    hs_material_map: dict[str, list[tuple[int, float]]],
) -> Optional[int]:
    """Return material_id for a 6-digit hs_code using a two-pass strategy.

    Pass 1 — exact match on the full hs_code string (up to 6 digits).
        If exactly one material maps to this code, return it.
        If multiple map to it, return the highest-confidence one; if tied,
        return None (genuinely ambiguous at this granularity).

    Pass 2 — 4-digit prefix fallback.
        Collect all mapping rows whose 4-digit prefix is a prefix of hs_code.
        Apply the same single/highest-confidence/tie-means-None logic.

    Returning None for ambiguous shared-prefix codes is intentional — a NULL
    material_id is honest; a wrong material_id silently poisons scoring.
    """
    # Pass 1: exact 6-digit (or shorter if stored that way) match.
    exact = hs_material_map.get(hs_code)
    if exact:
        if len(exact) == 1:
            return exact[0][0]
        max_conf = max(c for _, c in exact)
        top = [(mid, c) for mid, c in exact if c == max_conf]
        return top[0][0] if len(top) == 1 else None

    # Pass 2: 4-digit prefix fallback.
    candidates: list[tuple[int, float]] = []
    for prefix, entries in hs_material_map.items():
        if len(prefix) == 4 and hs_code.startswith(prefix):
            candidates.extend(entries)

    if not candidates:
        return None

    max_conf = max(c for _, c in candidates)
    top = [(mid, c) for mid, c in candidates if c == max_conf]
    return top[0][0] if len(top) == 1 else None


# ---------------------------------------------------------------------------
# Main ingest function
# ---------------------------------------------------------------------------

def ingest_comtrade(
    session: Session,
    years: list[int],
    reporters: Optional[dict[str, int]] = None,
    hs_prefixes: Optional[list[str]] = None,
    api_key: Optional[str] = None,
    flow_code: str = "X",
) -> dict[str, int]:
    """Fetch and ingest UN Comtrade annual trade flow data into trade_flows.

    Args:
        session:     SQLAlchemy session. Commits internally at the end.
        years:       List of years to fetch (e.g. [2021, 2022, 2023]).
        reporters:   Override dict of ISO2→comtrade_code. Defaults to
                     REPORTER_COUNTRIES for exports or CONSUMER_COUNTRIES
                     for imports — pass explicitly to mix or restrict scope.
        hs_prefixes: Override HS code prefixes. Defaults to reading from
                     supply_chain_contexts WHERE slug='ev_battery'.
        api_key:     Override API key. Defaults to settings.comtrade_api_key.
        flow_code:   ``"X"`` (exports, default) or ``"M"`` (imports).
                     Exports from REPORTER_COUNTRIES show supply-side
                     concentration. Imports from CONSUMER_COUNTRIES show
                     demand-side dependency and enable import-drop signals.

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

    # Default reporter set depends on flow direction: producers for exports,
    # consuming nations for imports. Caller can override either way.
    # DB query is preferred; falls back to hardcoded dicts if table is empty.
    if reporters is not None:
        resolved_reporters = reporters
    elif flow_code == "M":
        resolved_reporters = get_consumer_countries(session)
    else:
        resolved_reporters = get_reporter_countries(session)

    # Rebuild the numeric-code → ISO2 reverse map from the resolved set so
    # partner_country resolution in parse_comtrade_rows is consistent.
    global _CODE_TO_ISO2  # noqa: PLW0603 — intentional module-level update
    _CODE_TO_ISO2 = {v: k for k, v in {**REPORTER_COUNTRIES, **resolved_reporters}.items()}

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
                ext_id = _external_id(iso2, hs_prefix, year, flow_code=flow_code)

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
                        flow_code=flow_code,
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
                normalised = parse_comtrade_rows(raw_rows, iso2, hs_prefix, year, flow_code=flow_code)

                try:
                    source_doc_id = _create_source_document(
                        session,
                        source_id=source_id,
                        reporter_iso2=iso2,
                        hs_prefix=hs_prefix,
                        year=year,
                        row_count=len(normalised),
                        flow_code=flow_code,
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
