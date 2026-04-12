Create a World Bank Pink Sheet commodity price ingestion script for the battery-data-intelligence-engine FastAPI project.

## Context

The project has a `commodity_prices` table (ORM model `CommodityPrice` in `app/models/supply.py`) that stores historical price time series for battery materials. The table has a unique constraint on `(material_id, price_date, source)` so the ingestion must be idempotent. Materials are already seeded from USGS data and live in the `materials` table — prices are linked by FK.

The World Bank publishes the "Pink Sheet" (CMO-Historical-Data-Monthly.xlsx), a free monthly commodity price dataset going back decades. This is the primary free source for historical price data on Cobalt, Copper, Nickel, Aluminum, and Lithium.

## What to build

### 1. Add `openpyxl` dependency

In `pyproject.toml`, add to the `dependencies` list:

```toml
"openpyxl>=3.1.0",
```

`openpyxl` is needed to parse the Pink Sheet Excel file. Do not use `polars.read_excel()` — the Pink Sheet has a non-standard multi-row header structure that requires manual cell-level parsing.

### 2. `app/services/ingestion/worldbank_pinksheet.py` — ingestion module

Create this file. It must:

#### 2a. Download the file

```python
PINK_SHEET_URL = (
    "https://thedocs.worldbank.org/en/doc/18675f1d1639c7a34d463f59263ba0a2-0050012025"
    "/original/CMO-Historical-Data-Monthly.xlsx"
)
```

Use `httpx` (already in project deps) to download. Implement:

```python
def download_pink_sheet(url: str = PINK_SHEET_URL, timeout: int = 60) -> bytes:
    """Download the Pink Sheet Excel file and return raw bytes.

    Raises httpx.HTTPStatusError on non-2xx responses.
    Streams the response to avoid loading a large file into memory at once.
    """
```

Stream the download with `httpx.stream("GET", url)`, accumulate chunks, and return the full bytes. Do not write to disk — return bytes so the caller can pass them to `io.BytesIO`.

#### 2b. Parse the Excel structure

The Pink Sheet `Monthly Prices` worksheet has this non-standard layout:

- Rows 1–4: metadata (ignore)
- Row 5: commodity names (e.g. "Cobalt", "Copper", "Nickel", "Aluminum", "Lithium carbonate, battery grade")
- Row 6: units per commodity (e.g. "$/mt", "$/mmbtu", "$/troy oz")
- Row 7 onward: data rows — column A is a date string like `"2024M01"` (year + `M` + zero-padded month), subsequent columns are prices (floats or empty)

Implement:

```python
def parse_pink_sheet(raw_bytes: bytes) -> list[dict]:
    """Parse the Pink Sheet Excel bytes into a list of price observation dicts.

    Returns dicts with keys:
        price_date  (datetime.date)
        commodity   (str — the Pink Sheet column header, e.g. "Cobalt")
        price_usd   (float)
        price_unit  (str — normalised: "per_mt", "per_kg", "per_troy_oz")
        raw_unit    (str — original unit string from row 6)

    Skips rows where price_usd is None or not a valid positive float.
    Skips rows where the date cannot be parsed.
    """
```

Parse dates from the `"YYYYMmm"` format (e.g. `"2024M01"` → `date(2024, 1, 1)`). Use `datetime.strptime(cell, "%YM%m").date()` — set day to 1.

Normalise units:
- `"$/mt"` → `"per_mt"`
- `"$/kg"` → `"per_kg"`
- `"$/troy oz"` → `"per_troy_oz"`
- Any other unit → keep raw as-is (do not fail)

#### 2c. Material name mapping

Define a module-level dict mapping Pink Sheet column headers to `materials.canonical_name` values:

```python
_COMMODITY_TO_MATERIAL: dict[str, str] = {
    "Cobalt": "Cobalt",
    "Copper": "Copper",
    "Nickel": "Nickel",
    "Aluminum": "Aluminum",
    "Aluminium": "Aluminum",   # Pink Sheet uses British spelling
    "Lithium carbonate, battery grade": "Lithium",
    "Lithium": "Lithium",
    "Manganese ore": "Manganese",
    "Manganese": "Manganese",
    "Graphite": "Natural Graphite",
    "Natural graphite": "Natural Graphite",
}
```

Only commodities present in this mapping are ingested. All others are silently skipped.

#### 2d. Upsert function

```python
def ingest_pink_sheet(
    session: Session,
    url: str = PINK_SHEET_URL,
    since_year: Optional[int] = None,
) -> dict[str, int]:
    """Download, parse, and upsert Pink Sheet prices into commodity_prices.

    Args:
        session: SQLAlchemy session.
        url: Override download URL (useful for tests).
        since_year: If set, only ingest rows from this year onward (e.g. 2015).
                    Helps limit initial load size.

    Returns:
        {"inserted": int, "skipped_unknown_material": int, "skipped_existing": int}
    """
```

For each parsed row:
1. Look up `material_id` by querying `SELECT id FROM materials WHERE canonical_name = :name`. Cache results in a local dict to avoid N+1 queries.
2. If `canonical_name` is not found in the DB, increment `skipped_unknown_material` and continue.
3. Check for an existing `CommodityPrice` row with `(material_id, price_date, source="worldbank_pink_sheet")`. If it exists, increment `skipped_existing` and continue. (Do not update existing rows — historical prices should not change.)
4. Insert a new `CommodityPrice` row:
   - `source = "worldbank_pink_sheet"`
   - `metadata_json = {"raw_unit": raw_unit, "pink_sheet_column": commodity_column_header}`

Use `session.flush()` in batches of 500 rows, then `session.commit()` at the end.

### 3. `app/cli.py` — add `ingest-worldbank` command

Add this command to the existing Typer app:

```python
@app.command("ingest-worldbank")
def ingest_worldbank_cmd(
    since_year: Optional[int] = typer.Option(
        2015,
        "--since-year",
        help="Only ingest data from this year onward. Use 1960 for full history.",
    ),
    url: Optional[str] = typer.Option(
        None,
        "--url",
        help="Override the Pink Sheet download URL (default: current World Bank URL).",
    ),
) -> None:
    """Download the World Bank Pink Sheet and ingest commodity prices.

    Fetches CMO-Historical-Data-Monthly.xlsx directly from the World Bank.
    Idempotent: skips rows that already exist in commodity_prices.
    Re-run monthly after World Bank publishes an update (usually first week of month).
    """
    from app.services.ingestion.worldbank_pinksheet import ingest_pink_sheet, PINK_SHEET_URL

    s = _session()
    try:
        result = ingest_pink_sheet(
            session=s,
            url=url or PINK_SHEET_URL,
            since_year=since_year,
        )
        typer.echo(json.dumps({"ok": True, **result}, indent=2))
    except Exception as exc:
        typer.echo(json.dumps({"ok": False, "error": str(exc)}), err=True)
        raise typer.Exit(code=1)
    finally:
        s.close()
```

### 4. `pyproject.toml` — add hatch scripts

Add to `[tool.hatch.envs.default.scripts]`:

```toml
ingest-worldbank = "bdi-ingest ingest-worldbank"
ingest-worldbank-full = "bdi-ingest ingest-worldbank --since-year 1960"
```

### 5. Tests — `tests/test_worldbank_pinksheet.py`

Create this file. Use `pytest` (already in dev deps). Do not make real HTTP requests.

Test `parse_pink_sheet`:
- Construct a minimal in-memory Excel workbook using `openpyxl` with the correct row structure (metadata rows 1–4, commodity names in row 5, units in row 6, two data rows in rows 7–8)
- Verify that dates parse correctly from `"YYYYMmm"` format
- Verify that unit strings are normalised correctly
- Verify that rows with empty price cells are skipped
- Verify that rows with non-numeric price values are skipped

Test `ingest_pink_sheet`:
- Mock `download_pink_sheet` to return bytes of the in-memory workbook
- Use a real SQLAlchemy in-memory SQLite session (or mock the session)
- Verify that `inserted` count matches the number of valid rows after material lookup
- Verify that running twice does not increase `inserted` (idempotency)

## Constraints

- Do not write the Excel file to disk at any point — work entirely in memory via `io.BytesIO`
- The `since_year` default of 2015 is intentional — full history back to 1960 is large (~700 rows per commodity); limit to recent data for the initial load
- Do not fail the entire run if a single row is malformed — log a warning and skip
- All logging must use `structlog` (already in project deps), not `print` or `logging` directly — follow the pattern used elsewhere in the project (e.g. `log = structlog.get_logger()` at module level, `log.warning("skipping row", reason=..., row=...)`)
- The unique constraint `(material_id, price_date, source)` is the idempotency guard — rely on it rather than doing a delete-and-reinsert
- Do not add any dependency other than `openpyxl`

## Reference: existing ORM model

```python
# app/models/supply.py
class CommodityPrice(Base):
    __tablename__ = "commodity_prices"
    __table_args__ = (
        UniqueConstraint("material_id", "price_date", "source", name="uq_commodity_price"),
    )

    id: Mapped[int]
    material_id: Mapped[int]          # FK → materials.id
    price_date: Mapped[date]
    price_usd: Mapped[float]
    price_unit: Mapped[str]           # "per_mt" | "per_kg" | "per_troy_oz"
    source: Mapped[str]               # use "worldbank_pink_sheet"
    metadata_json: Mapped[Optional[Any]]  # JSONB
    created_at: Mapped[datetime]
```

## Reference: existing CLI pattern

Follow the existing `ingest_usgs_cmd` function in `app/cli.py` for session management, error handling, and JSON output format.
