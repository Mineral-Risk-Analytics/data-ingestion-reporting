Create a UN Comtrade API ingestion script for the battery-data-intelligence-engine FastAPI project.

## Context

The UN Comtrade API provides bilateral trade flow data (imports and exports) by HS code, country, and year. This feeds the `trade_flows` table, which is the primary input for the **Material Concentration pillar** (30% weight) of the supply chain risk scoring model — specifically, which countries control the largest share of exports for battery-critical materials.

The project already has HS code prefixes configured in the `supply_chain_contexts` table (slug `ev_battery`): `["8507","2825","2836","2604","2602","2501"]`. These prefixes are the filter for which commodity codes to query.

The `trade_flows` table requires a `source_document_id` FK — every batch of Comtrade results must be linked to a `SourceDocument` row for provenance tracking.

The user has a paid Comtrade API subscription key. The free tier allows 500 calls/day; the paid tier allows significantly more. The script must be rate-limit-aware regardless.

## What to build

### 1. `app/core/config.py` — add Comtrade settings

Add these fields to the existing `Settings` class:

```python
comtrade_api_key: str = ""
comtrade_base_url: str = "https://comtradeapi.un.org/data/v1/get"
comtrade_rate_limit_delay: float = 1.0  # seconds between API calls
```

Add to `.env.example`:
```
COMTRADE_API_KEY=your_key_here
COMTRADE_BASE_URL=https://comtradeapi.un.org/data/v1/get
COMTRADE_RATE_LIMIT_DELAY=1.0
```

### 2. `app/services/ingestion/comtrade.py` — ingestion module

Create this file.

#### 2a. Constants

```python
# Comtrade numeric country codes for key battery supply chain actors.
# These are the reporters we query — exporting countries and major importers.
REPORTER_COUNTRIES: dict[str, int] = {
    "CN": 156,   # China — graphite, lithium processing, cells
    "CL": 152,   # Chile — lithium
    "AU": 36,    # Australia — lithium, nickel
    "CD": 180,   # DRC — cobalt
    "ID": 360,   # Indonesia — nickel
    "RU": 643,   # Russia — nickel
    "US": 842,   # United States
    "JP": 392,   # Japan
    "KR": 410,   # South Korea
    "DE": 276,   # Germany
    "CA": 124,   # Canada
    "ZA": 710,   # South Africa — manganese, platinum group
    "PH": 608,   # Philippines — nickel
    "MZ": 508,   # Mozambique — graphite
}

# World aggregate partner — Comtrade uses 0 for "all partners"
WORLD_PARTNER_CODE = 0
```

#### 2b. API client

```python
def _comtrade_get(
    path: str,
    params: dict,
    api_key: str,
    timeout: int = 30,
) -> dict:
    """Make one authenticated GET request to the Comtrade API.

    Auth: Ocp-Apim-Subscription-Key header (Azure API Management).
    Raises httpx.HTTPStatusError on non-2xx.
    Raises ValueError if the response contains a Comtrade-level error message
    (the API returns 200 with {"error": "..."} on invalid params).
    Returns the parsed JSON dict.
    """
```

Use `httpx.get()` (synchronous). Set header `{"Ocp-Apim-Subscription-Key": api_key}`. Do not retry automatically — let the caller handle retries if needed.

#### 2c. Query builder

The Comtrade API endpoint structure:

```
GET {base_url}/{typeCode}/{freqCode}/{clCode}
    ?reporterCode={int}
    &partnerCode={int}
    &period={year}
    &cmdCode={hs_code}
    &flowCode={M|X|MX}
    &maxRecords=100000
    &includeDesc=true
```

- `typeCode` = `C` (commodities, not services)
- `freqCode` = `A` (annual)
- `clCode` = `HS` (Harmonized System classification)
- `flowCode` = `X` (exports) — export data tells you what each country ships to the world, which is the concentration signal we need

Implement:

```python
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
    Each row is a raw dict from the API response's data array.
    """
```

The HS code passed to Comtrade should be the 4-digit prefix as-is (e.g. `"8507"`). Comtrade will return all matching 6-digit subheadings.

#### 2d. Response parsing

```python
def parse_comtrade_rows(
    rows: list[dict],
    reporter_iso2: str,
    hs_prefix: str,
    year: int,
) -> list[dict]:
    """Normalise raw Comtrade API rows into TradeFlow insert dicts.

    Returns dicts with keys matching TradeFlow columns:
        period          (str — e.g. "2023")
        reporter_country (str — ISO2)
        partner_country  (str — ISO2 or "WLD" for world aggregate)
        hs_code         (str — the 6-digit cmdCode from the response)
        hs_description  (str | None — cmdDesc)
        import_export_flag (str — "export")
        quantity        (float | None — netWgt in kg, if available)
        quantity_unit   (str | None — "kg" if netWgt used)
        trade_value_usd (float | None — primaryValue)
        metadata_json   (dict — {"comtrade_flow_code": "X", "comtrade_period": year,
                                  "hs_prefix_queried": hs_prefix})

    Skips rows where both trade_value_usd and quantity are None or zero.
    Converts Comtrade numeric partner codes to ISO2 using the reverse of REPORTER_COUNTRIES
    where possible; falls back to str(partnerCode) for unmapped codes.
    """
```

Note: `partner_country` for world-aggregate queries (partnerCode=0) will be `"WLD"` — this is expected and valid.

#### 2e. Source and SourceDocument management

`TradeFlow.source_document_id` is NOT NULL — every insert must be linked to a provenance record.

```python
def _get_or_create_comtrade_source(session: Session) -> int:
    """Get or create the Source row for UN Comtrade. Returns source.id."""
    # Use name="UN Comtrade API", source_type="comtrade", phase="1"
    # config_json={"base_url": settings.comtrade_base_url, "freq": "A", "classification": "HS"}
```

```python
def _create_source_document(
    session: Session,
    source_id: int,
    reporter_iso2: str,
    hs_prefix: str,
    year: int,
    row_count: int,
) -> int:
    """Create a SourceDocument for one API call batch. Returns source_document.id.

    external_id = f"comtrade_C_A_HS_{hs_prefix}_{reporter_iso2}_{year}"
    title = f"UN Comtrade: {reporter_iso2} HS {hs_prefix} exports {year}"
    document_type = "trade_data"
    metadata_json = {"reporter": reporter_iso2, "hs_prefix": hs_prefix,
                     "year": year, "row_count": row_count}

    If a SourceDocument with this external_id already exists (unique constraint on
    source_id + external_id), return the existing row's id — do not insert a duplicate.
    """
```

#### 2f. Material lookup

```python
def _build_hs_material_map(session: Session) -> dict[str, Optional[int]]:
    """Return a dict of hs_code_prefix → material_id from hs_code_material_mappings.

    Covers both 4-digit prefixes (from supply_chain_contexts) and 6-digit codes
    (matched by startswith). Used to set material_id on TradeFlow rows.
    """
```

Query all `HsCodeMaterialMapping` rows once. For a given 6-digit `hs_code` from Comtrade, find the material by checking if any 4-digit prefix in the map is a prefix of the code. Return `None` if no match (still insert the row — `material_id` is nullable on `TradeFlow`).

#### 2g. Main ingest function

```python
def ingest_comtrade(
    session: Session,
    years: list[int],
    reporters: Optional[dict[str, int]] = None,
    hs_prefixes: Optional[list[str]] = None,
    api_key: Optional[str] = None,
) -> dict[str, int]:
    """Fetch and ingest UN Comtrade annual export data into trade_flows.

    Args:
        session: SQLAlchemy session.
        years: List of years to fetch (e.g. [2021, 2022, 2023]).
        reporters: Override dict of ISO2→comtrade_code. Defaults to REPORTER_COUNTRIES.
        hs_prefixes: Override HS code prefixes. Defaults to reading from
                     supply_chain_contexts WHERE slug='ev_battery'.
        api_key: Override API key. Defaults to settings.comtrade_api_key.

    Returns:
        {
            "inserted": int,
            "skipped_existing_doc": int,
            "skipped_empty_response": int,
            "api_calls_made": int,
            "errors": int,
        }
    """
```

Implementation:

1. Resolve `hs_prefixes`: if not provided, query `supply_chain_contexts` WHERE `slug = 'ev_battery'` and read `relevant_hs_code_prefixes` from the JSONB column.

2. Call `_get_or_create_comtrade_source()` once.

3. Build `hs_material_map` once via `_build_hs_material_map()`.

4. Outer loop: for each `year` × `(iso2, code)` in reporters × `hs_prefix`:
   - Check if a `SourceDocument` with `external_id = f"comtrade_C_A_HS_{hs_prefix}_{iso2}_{year}"` already exists. If it does, increment `skipped_existing_doc` and continue. (This is the idempotency check — if the document exists, that batch was already ingested.)
   - Sleep `settings.comtrade_rate_limit_delay` seconds before each API call (respect rate limits even with a paid key).
   - Call `fetch_annual_exports()`. On `httpx.HTTPStatusError` or `ValueError`, log the error, increment `errors`, and continue to the next iteration — do not abort the whole run.
   - If response is empty, increment `skipped_empty_response` and continue.
   - Call `parse_comtrade_rows()` to normalise the rows.
   - Call `_create_source_document()` to get a `source_document_id`.
   - For each normalised row, resolve `material_id` from `hs_material_map`. Insert a `TradeFlow` row. Use `session.flush()` every 500 rows.
   - Increment `inserted` by the number of rows inserted.
   - Increment `api_calls_made`.

5. `session.commit()` once at the end.

Log each API call at INFO level with reporter, hs_prefix, year, and row count. Log errors at WARNING.

### 3. `app/cli.py` — add `ingest-comtrade` command

```python
@app.command("ingest-comtrade")
def ingest_comtrade_cmd(
    years: str = typer.Option(
        "2021,2022,2023",
        "--years",
        help="Comma-separated list of years to fetch (e.g. '2021,2022,2023').",
    ),
    reporters: Optional[str] = typer.Option(
        None,
        "--reporters",
        help="Comma-separated ISO2 reporter codes to limit scope (e.g. 'CN,CL,AU'). Default: all 14 configured reporters.",
    ),
    hs_prefixes: Optional[str] = typer.Option(
        None,
        "--hs-prefixes",
        help="Comma-separated HS prefixes to query (e.g. '2604,2602'). Default: reads from supply_chain_contexts.",
    ),
) -> None:
    """Ingest UN Comtrade annual export trade flows for battery-critical HS codes.

    Fetches export data (flowCode=X) for configured reporter countries × HS prefixes × years.
    Idempotent: skips reporter/HS/year combinations already present in source_documents.
    Re-run annually when a new data year becomes available (typically ~3-month lag).

    API call count = len(reporters) × len(hs_prefixes) × len(years).
    With defaults (14 reporters × 6 HS codes × 3 years = 252 calls).
    Paid tier handles this comfortably; free tier (500/day) handles it in one run.
    """
    from app.services.ingestion.comtrade import REPORTER_COUNTRIES, ingest_comtrade

    year_list = [int(y.strip()) for y in years.split(",")]

    reporter_filter = None
    if reporters:
        codes = [r.strip().upper() for r in reporters.split(",")]
        reporter_filter = {k: v for k, v in REPORTER_COUNTRIES.items() if k in codes}
        unknown = set(codes) - set(REPORTER_COUNTRIES.keys())
        if unknown:
            typer.echo(f"Warning: unknown reporter codes ignored: {unknown}", err=True)

    hs_list = [h.strip() for h in hs_prefixes.split(",")] if hs_prefixes else None

    s = _session()
    try:
        result = ingest_comtrade(
            session=s,
            years=year_list,
            reporters=reporter_filter,
            hs_prefixes=hs_list,
        )
        typer.echo(json.dumps({"ok": True, **result}, indent=2))
    except Exception as exc:
        typer.echo(json.dumps({"ok": False, "error": str(exc)}), err=True)
        raise typer.Exit(code=1)
    finally:
        s.close()
```

### 4. `pyproject.toml` — add hatch scripts

```toml
ingest-comtrade = "bdi-ingest ingest-comtrade"
ingest-comtrade-recent = "bdi-ingest ingest-comtrade --years 2021,2022,2023"
ingest-comtrade-cn-only = "bdi-ingest ingest-comtrade --reporters CN --years 2021,2022,2023"
```

No new dependencies — only `httpx` (already present) and stdlib (`time`, `json`).

### 5. Tests — `tests/test_comtrade.py`

Create this file. Do not make real API calls.

**Test `parse_comtrade_rows`:**
- Provide a minimal list of raw Comtrade row dicts (matching the API's response schema)
- Verify period, reporter_country, import_export_flag are set correctly
- Verify rows with zero primaryValue and no netWgt are skipped
- Verify hs_code is taken from cmdCode (6-digit), not the queried prefix

**Test `_build_hs_material_map`:**
- Mock a session that returns two HsCodeMaterialMapping rows
- Verify prefix lookup: a 6-digit code starting with a known 4-digit prefix resolves correctly
- Verify unrecognised code returns None

**Test `ingest_comtrade` (mocked):**
- Mock `fetch_annual_exports` to return 3 rows for one call, 0 rows for another
- Mock `_comtrade_get` to avoid HTTP calls
- Verify `inserted` = 3, `skipped_empty_response` = 1
- Verify running again (with mocked existing SourceDocument) produces `skipped_existing_doc` > 0

## Constraints

- Never hardcode the API key — always read from `settings.comtrade_api_key`
- Raise a clear error (not a silent skip) if `settings.comtrade_api_key` is empty when `ingest_comtrade` is called
- The rate limit sleep must happen before every API call, not after — this ensures the delay applies even on the first call of a new run immediately following a previous one
- `trade_flows` has no unique constraint — duplicate detection is done via the `SourceDocument` check (external_id uniqueness). Do not insert `TradeFlow` rows if the corresponding `SourceDocument` already exists.
- `material_id` is nullable on `TradeFlow` — do not skip rows just because no HS mapping exists. Insert with `material_id=None` and log a DEBUG message.
- Use `structlog` throughout, following the module-level `log = structlog.get_logger()` pattern
- The Comtrade API returns HTTP 200 even for invalid parameter errors — always check the response body for an `"error"` key and raise `ValueError` if present

## Reference: ORM models used

```python
# app/models/supply.py
class TradeFlow(Base):
    __tablename__ = "trade_flows"
    id: Mapped[int]
    source_document_id: Mapped[int]        # NOT NULL — must always be set
    period: Mapped[str]                    # "2023"
    reporter_country: Mapped[str]          # ISO2 e.g. "CN"
    partner_country: Mapped[str]           # ISO2 or "WLD"
    hs_code: Mapped[Optional[str]]         # 6-digit e.g. "280450"
    hs_description: Mapped[Optional[str]]
    material_id: Mapped[Optional[int]]     # FK → materials.id (nullable)
    import_export_flag: Mapped[str]        # "export"
    quantity: Mapped[Optional[float]]      # netWgt in kg
    quantity_unit: Mapped[Optional[str]]   # "kg"
    trade_value_usd: Mapped[Optional[float]]
    metadata_json: Mapped[Optional[Any]]

class HsCodeMaterialMapping(Base):
    __tablename__ = "hs_code_material_mappings"
    hs_code_prefix: Mapped[str]            # 4-digit prefix
    material_id: Mapped[int]

# app/models/source.py
class Source(Base):
    __tablename__ = "sources"
    id: Mapped[int]
    name: Mapped[str]                      # unique
    source_type: Mapped[str]
    phase: Mapped[str]
    config_json: Mapped[Optional[Any]]

# app/models/documents.py
class SourceDocument(Base):
    __tablename__ = "source_documents"
    __table_args__ = UniqueConstraint("source_id", "external_id")
    id: Mapped[int]
    source_id: Mapped[int]
    external_id: Mapped[str]               # idempotency key
    title: Mapped[Optional[str]]
    document_type: Mapped[str]
    metadata_json: Mapped[Optional[Any]]

# app/models/supply_chain_context.py
class SupplyChainContext(Base):
    __tablename__ = "supply_chain_contexts"
    slug: Mapped[str]
    relevant_hs_code_prefixes: Mapped[Optional[Any]]  # JSONB array of strings
```

## Reference: existing CLI pattern

Follow `ingest_usgs_cmd` in `app/cli.py` for session management, error handling, and JSON output format.

## Important implementation notes (add as module docstring)

- **Data lag**: Comtrade annual data for year Y is typically available by March/April of Y+1. Do not query the current year — it will return partial or no data.
- **Export vs import**: This script queries exports (flowCode=X) from producing countries. Import data (flowCode=M) from consuming countries gives a different view. Exports from producers are more useful for concentration scoring. Import data can be added later.
- **World partner (partnerCode=0)**: Using partner=0 gives total exports to all destinations, which is what we want for market share calculations. Bilateral flows (e.g. CN→US specifically) are available but require more API calls and are better suited to a later phase.
- **HS prefix granularity**: Querying by 4-digit prefix returns all 6-digit sub-codes. A single HS prefix may return many commodity lines (e.g. 8507 covers all battery types). The `material_id` mapping via `hs_code_material_mappings` narrows these to specific materials.
