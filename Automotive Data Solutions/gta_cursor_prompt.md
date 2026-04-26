Create a Global Trade Alert (GTA) ingestion script for the battery-data-intelligence-engine FastAPI project.

## Context

The `risk_events` table is the core intelligence stream feeding the **Geopolitical/Trade pillar (20% weight)** of the supply chain risk scoring model. Currently this table is populated by SEC EDGAR filings, Federal Register notices, and news adapters — but it is missing a critical category: **export restrictions and trade interventions on raw materials**.

Global Trade Alert (GTA), maintained by the University of St. Gallen, is the authoritative free database of government trade policy interventions globally. It covers:
- China's 2023 graphite export controls
- Indonesia's nickel export ban and ore processing mandate
- DRC cobalt export restrictions
- US export controls on battery technology
- EU import restrictions and CBAM-adjacent trade measures

These are among the most material geopolitical risk events for battery supply chains in the last five years. Without them, the geopolitical/trade scores significantly understate actual risk for graphite (China), nickel (Indonesia), and cobalt (DRC).

The project already has `RiskEvent`, `RiskEventMaterial`, `RiskEventGeography`, `SourceDocument`, and `Source` ORM models. No schema changes are needed.

---

## Data source

GTA provides bulk CSV exports via their data extraction page at `https://www.globaltradealert.org/data_extraction`. The relevant download is the "State Acts" CSV containing all recorded trade policy interventions.

**Download URL (direct bulk CSV):**
```
https://www.globaltradealert.org/data_extraction/download?dataset=state-acts&format=csv
```

If this URL does not return the CSV directly (GTA occasionally restructures their download URLs), fall back to checking `https://www.globaltradealert.org/data_extraction` for the current download link. Log the URL used in `metadata_json`.

**GTA intervention traffic light:**
- **Red** = harmful (export restrictions, import barriers, subsidies distorting competition) — **this is what we want**
- **Amber** = pending/announced but not yet implemented
- **Green** = liberalising interventions

For supply chain risk scoring, ingest **Red** interventions only.

---

## What to build

### 1. `app/services/ingestion/gta.py` — ingestion module

Create this file.

#### 1a. Constants and HS code filter

```python
GTA_DOWNLOAD_URL = (
    "https://www.globaltradealert.org/data_extraction/download"
    "?dataset=state-acts&format=csv"
)

# HS code prefixes for battery-critical materials.
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
# This map covers the countries most likely to appear in battery-material interventions.
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

# Map GTA intervention_type to our RiskCategory
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
# Default for unmapped types:
DEFAULT_GTA_CATEGORY = "geopolitical_trade"
```

#### 1b. CSV download

```python
def download_gta_csv(url: str = GTA_DOWNLOAD_URL, timeout: int = 120) -> bytes:
    """Download the GTA State Acts CSV and return raw bytes.

    Streams the response to handle large files (~50–100 MB uncompressed).
    Raises httpx.HTTPStatusError on non-2xx.
    """
```

Use `httpx.stream("GET", url, follow_redirects=True)` to handle the GTA download URL which may redirect. Return full bytes for passing to `io.StringIO` / `csv.DictReader`.

#### 1c. CSV parsing and filtering

The GTA CSV has the following relevant columns (column names may vary slightly — use case-insensitive matching):

| Column | Description |
|---|---|
| `id` | GTA intervention ID (integer) — primary idempotency key |
| `title` | Intervention title / description |
| `date_announced` | Date announced (YYYY-MM-DD or YYYY-MM) |
| `date_implemented` | Date the intervention came into force |
| `date_removed` | Date the intervention was removed (may be empty) |
| `implementing_jurisdiction` | Country implementing the measure (name or ISO2) |
| `gta_evaluation` | "Red", "Amber", or "Green" |
| `intervention_type` | Type of intervention (maps to risk category) |
| `affected_hs_codes` | Semicolon- or comma-separated HS codes (6-digit) |
| `description` | Full text description of the intervention |
| `in_force` | Boolean — whether the intervention is currently active |

**Column name discovery:** GTA CSV column names may change between releases. Read the header row and build a case-insensitive column mapping before processing rows. If a required column is missing, raise `ValueError` with the available column names so Cursor can adjust the mapping.

```python
def parse_gta_csv(
    raw_bytes: bytes,
    hs_prefixes: list[str] = BATTERY_HS_PREFIXES,
    since_year: Optional[int] = 2018,
) -> list[dict]:
    """Parse GTA CSV bytes into a list of normalised intervention dicts.

    Filters to:
      - gta_evaluation == "Red" (harmful interventions only)
      - At least one affected HS code matches a prefix in hs_prefixes
      - date_announced or date_implemented is on or after since_year (if set)

    Returns dicts with keys:
        gta_id              (int — from the "id" column)
        title               (str)
        event_date          (datetime — from date_implemented if available, else date_announced)
        implementing_iso2   (str | None — ISO2 country code)
        intervention_type   (str)
        risk_category       (str — from GTA_INTERVENTION_CATEGORY_MAP)
        matched_hs_codes    (list[str] — only the HS codes matching our prefixes)
        summary             (str — truncated description, max 500 chars)
        in_force            (bool)
        raw_row             (dict — full original row for metadata_json)

    Skips rows where:
      - gta_evaluation != "Red"
      - No HS code matches any battery prefix
      - date parsing fails completely (log warning)
    """
```

Parse dates tolerantly — GTA sometimes uses `YYYY-MM-DD`, sometimes `YYYY-MM`. Accept both. Use `datetime.strptime` with `try/except`. If only year-month is available, set day=1.

#### 1d. Material lookup from HS codes

```python
def _build_hs_material_map(session: Session) -> dict[str, Optional[int]]:
    """Return dict of hs_code_prefix → material_id from hs_code_material_mappings.

    For a 6-digit HS code, find the material by checking if any 4-digit prefix
    in the map is a prefix of the code. Returns None if no match.
    """
```

Query all `HsCodeMaterialMapping` rows once. This is the same function pattern used in `comtrade.py` — if it's already there, import it rather than duplicating.

#### 1e. Idempotency via content_hash

`RiskEvent.content_hash` is a SHA-256 of `(title + summary + event_date.isoformat())`. GTA rows also have a stable `gta_id`. Store `gta_id` in `metadata_json` so duplicates can be detected by either mechanism.

Primary idempotency check: query `SELECT id FROM risk_events WHERE content_hash = :hash`. Secondary check: query `WHERE metadata_json->>'gta_id' = :gta_id`. If either matches, skip the row.

#### 1f. Source document management

```python
def _get_or_create_gta_source(session: Session) -> int:
    """Get or create the Source row for Global Trade Alert. Returns source.id."""
    # name="Global Trade Alert", source_type="gta", phase="1"
    # config_json={"base_url": "https://www.globaltradealert.org", "method": "bulk_csv"}
```

Create one `SourceDocument` per ingestion run (not per row):
```python
def _create_gta_source_document(
    session: Session,
    source_id: int,
    row_count: int,
    run_date: date,
) -> int:
    """Create a SourceDocument for this ingestion run.

    external_id = f"gta_bulk_csv_{run_date.isoformat()}"
    title = f"Global Trade Alert — bulk CSV download {run_date}"
    document_type = "trade_policy_database"
    """
```

If a `SourceDocument` with this `external_id` already exists, reuse its ID. This means re-running on the same date is idempotent at the document level, while `content_hash` handles individual event deduplication.

#### 1g. Main ingest function

```python
def ingest_gta(
    session: Session,
    url: str = GTA_DOWNLOAD_URL,
    since_year: Optional[int] = 2018,
    hs_prefixes: Optional[list[str]] = None,
) -> dict[str, int]:
    """Download, parse, and ingest GTA harmful interventions into risk_events.

    Args:
        session: SQLAlchemy session.
        url: Override download URL (useful for tests with local CSV).
        since_year: Only ingest interventions from this year onward. Default: 2018.
                    China graphite and Indonesia nickel restrictions are 2019–2023.
        hs_prefixes: Override HS prefix filter. Defaults to BATTERY_HS_PREFIXES.

    Returns:
        {
            "downloaded_rows": int,        # rows in CSV after filtering to Red + HS match
            "inserted": int,               # new RiskEvent rows created
            "skipped_existing": int,       # rows skipped due to content_hash match
            "material_links": int,         # RiskEventMaterial rows created
            "geography_links": int,        # RiskEventGeography rows created
            "skipped_unknown_country": int,
        }
    """
```

Implementation:

1. Download CSV via `download_gta_csv(url)`.
2. Parse via `parse_gta_csv(raw_bytes, hs_prefixes=hs_prefixes or BATTERY_HS_PREFIXES, since_year=since_year)`.
3. Call `_get_or_create_gta_source()` once.
4. Call `_create_gta_source_document()` for today's date.
5. Build `hs_material_map` via `_build_hs_material_map()`.
6. For each parsed intervention dict:
   - Compute `content_hash = hashlib.sha256(f"{title}{summary}{event_date.isoformat()}".encode()).hexdigest()`.
   - Check for existing `RiskEvent` by `content_hash`. If found, increment `skipped_existing`, continue.
   - Determine `risk_categories_json`: `[risk_category]` from `GTA_INTERVENTION_CATEGORY_MAP`.
   - Create `RiskEvent`:
     - `event_type = intervention_type` (from GTA, max 128 chars — truncate if needed)
     - `event_date = event_date` (timezone-aware UTC)
     - `title = title`
     - `summary = summary`
     - `severity_score`: derive from `in_force` — active interventions score 0.7, inactive score 0.3. Export bans score 0.9.
     - `confidence_score = 0.9` (GTA is a curated, peer-reviewed dataset)
     - `risk_categories_json = [risk_category]`
     - `geography_json = {"primary": implementing_iso2}` if country resolved
     - `content_hash = content_hash`
     - `metadata_json = {"gta_id": gta_id, "gta_hs_codes": matched_hs_codes, "raw_intervention_type": intervention_type, "source_url": url}`
     - `verified = False` — GTA events should go through the existing review workflow
   - Create `RiskEventGeography` for `implementing_iso2` with `geography_context="primary"`, `relevance_score=1.0`. If country is not resolved (implementing_iso2 is None), increment `skipped_unknown_country` and still create the event — geography link is optional.
   - For each HS code in `matched_hs_codes`: look up `material_id` from `hs_material_map`. If found, create `RiskEventMaterial` with `relevance_score=0.9`, `match_reason="hs_code"`. Check for existing `(risk_event_id, material_id)` before inserting.
   - `session.flush()` every 100 rows.
   - Increment `inserted`.

7. `session.commit()` once at the end.

Log at INFO per 100 rows processed. Log unresolved countries at DEBUG.

### 2. `app/cli.py` — add `ingest-gta` command

```python
@app.command("ingest-gta")
def ingest_gta_cmd(
    since_year: int = typer.Option(
        2018,
        "--since-year",
        help="Only ingest interventions from this year onward. Default: 2018.",
    ),
    url: Optional[str] = typer.Option(
        None,
        "--url",
        help="Override the GTA CSV download URL.",
    ),
    hs_prefixes: Optional[str] = typer.Option(
        None,
        "--hs-prefixes",
        help="Comma-separated HS prefixes to filter (e.g. '2604,2602'). Default: all battery-material prefixes.",
    ),
) -> None:
    """Download and ingest Global Trade Alert harmful trade interventions.

    Fetches the GTA State Acts bulk CSV and ingests Red (harmful) interventions
    whose affected HS codes match battery-critical materials. Creates RiskEvent
    rows with RiskEventGeography and RiskEventMaterial junction records.

    Idempotent: skips events already present (checked via content_hash).
    Re-run weekly to pick up new GTA interventions.

    Run this after seed-materials so HS code → material_id lookups resolve:
      bdi-ingest seed-materials
      bdi-ingest ingest-gta --since-year 2018

    Note: GTA interventions are inserted with verified=False. Use the admin
    event review interface to confirm or exclude specific events.
    """
    from app.services.ingestion.gta import GTA_DOWNLOAD_URL, BATTERY_HS_PREFIXES, ingest_gta

    hs_list = [h.strip() for h in hs_prefixes.split(",")] if hs_prefixes else None

    s = _session()
    try:
        result = ingest_gta(
            session=s,
            url=url or GTA_DOWNLOAD_URL,
            since_year=since_year,
            hs_prefixes=hs_list,
        )
        typer.echo(json.dumps({"ok": True, **result}, indent=2))
    except Exception as exc:
        typer.echo(json.dumps({"ok": False, "error": str(exc)}), err=True)
        raise typer.Exit(code=1)
    finally:
        s.close()
```

### 3. `pyproject.toml` — add hatch scripts

```toml
ingest-gta = "bdi-ingest ingest-gta"
ingest-gta-recent = "bdi-ingest ingest-gta --since-year 2020"
```

No new dependencies — only `httpx`, `csv`, `hashlib`, `io` (all stdlib or already present).

### 4. Tests — `tests/test_gta.py`

Create this file. Do not make real HTTP calls.

**Test `parse_gta_csv`:**
- Construct a minimal in-memory CSV bytes object with 5 rows:
  - 2 Red rows with matching HS codes (should be returned)
  - 1 Red row with no matching HS code (should be skipped)
  - 1 Amber row with matching HS code (should be skipped — Red only)
  - 1 Red row with date before `since_year` (should be skipped)
- Verify returned list has exactly 2 items
- Verify `risk_category` is mapped correctly for "Export bans" → "geopolitical_trade"
- Verify `implementing_iso2` is resolved via `GTA_COUNTRY_MAP`
- Verify unresolvable country returns `None` for `implementing_iso2` (does not fail)
- Verify `matched_hs_codes` only contains codes matching the prefix list

**Test `ingest_gta` (mocked):**
- Mock `download_gta_csv` to return minimal CSV bytes (same as above)
- Pre-seed materials + HS code mappings in test session
- Call `ingest_gta(session, since_year=2018)`
- Verify `inserted == 2`
- Verify `RiskEventGeography` rows created for rows with resolved countries
- Verify `RiskEventMaterial` rows created for rows with mapped HS codes
- Call again — verify `skipped_existing == 2` (idempotency via content_hash)

## Constraints

- **`verified=False` for all GTA events** — GTA events must go through the existing admin review flow (`review_status = "pending"` in the admin UI) before they affect scores. This is consistent with the `LINK_EVENTS_TO_COMPANIES = False` architecture: market-level events are ingested but reviewed before being promoted to scoring.
- Do not fail the entire run if a single row's country cannot be resolved — log at DEBUG and continue
- Do not add `pandas` or `lxml` — use stdlib `csv.DictReader` for CSV parsing
- The `content_hash` check is the primary deduplication mechanism. Do not rely on GTA row IDs alone — GTA occasionally re-publishes the same intervention with a new ID
- Use `structlog` throughout, module-level `log = structlog.get_logger()`
- `severity_score` derivation: `0.9` for export bans (explicitly blocks supply), `0.7` for all other active Red interventions, `0.3` for inactive/removed interventions. This is a conservative initial calibration — the scoring engine applies decay on top of it
- The GTA bulk CSV can be large (~50–100 MB). Use streaming download and `io.StringIO` for parsing — do not load the entire CSV into a Python list before filtering

## Reference: ORM models used

```python
# app/models/regulatory.py
class RiskEvent(Base):
    __tablename__ = "risk_events"

    id: Mapped[int]
    source_document_id: Mapped[Optional[int]]    # FK → source_documents.id
    event_type: Mapped[str]                       # intervention_type from GTA (max 128 chars)
    event_date: Mapped[Optional[datetime]]        # timezone-aware UTC
    title: Mapped[str]
    summary: Mapped[Optional[str]]
    severity_score: Mapped[Optional[float]]       # 0.0–1.0
    confidence_score: Mapped[Optional[float]]     # 0.9 for GTA (curated source)
    risk_categories_json: Mapped[Optional[Any]]   # ["geopolitical_trade"]
    geography_json: Mapped[Optional[Any]]         # {"primary": "CN"}
    content_hash: Mapped[Optional[str]]           # SHA-256 deduplication key
    metadata_json: Mapped[Optional[Any]]          # gta_id, hs_codes, etc.
    verified: Mapped[bool]                        # False for GTA events

class RiskEventMaterial(Base):
    __tablename__ = "risk_event_materials"
    __table_args__ = (UniqueConstraint("risk_event_id", "material_id", name="uq_risk_event_material"),)

    id: Mapped[int]
    risk_event_id: Mapped[int]
    material_id: Mapped[int]
    relevance_score: Mapped[float]                # 0.9 for HS code match
    match_reason: Mapped[Optional[str]]           # "hs_code"

class RiskEventGeography(Base):
    __tablename__ = "risk_event_geographies"
    __table_args__ = (UniqueConstraint("risk_event_id", "country_code", name="uq_risk_event_geography"),)

    id: Mapped[int]
    risk_event_id: Mapped[int]
    country_code: Mapped[str]                     # ISO2
    geography_context: Mapped[str]                # "primary"
    relevance_score: Mapped[float]                # 1.0

# app/models/supply.py
class HsCodeMaterialMapping(Base):
    __tablename__ = "hs_code_material_mappings"
    hs_code_prefix: Mapped[str]    # 4-digit prefix
    material_id: Mapped[int]

# app/models/documents.py — same as comtrade.py
class SourceDocument(Base):
    __tablename__ = "source_documents"
    __table_args__ = UniqueConstraint("source_id", "external_id")
    id: Mapped[int]
    source_id: Mapped[int]
    external_id: Mapped[str]     # "gta_bulk_csv_{YYYY-MM-DD}"
    title: Mapped[Optional[str]]
    document_type: Mapped[str]   # "trade_policy_database"
    metadata_json: Mapped[Optional[Any]]
```

## Reference: existing CLI pattern

Follow `ingest_usgs_cmd` in `app/cli.py` for session management, error handling, and JSON output.

## Important implementation notes (add as module docstring)

- **GTA download URL stability**: GTA occasionally changes their download URL. If the primary URL fails, log the error clearly with the URL attempted. The download page at `https://www.globaltradealert.org/data_extraction` shows the current bulk download option — instruct the user to update `GTA_DOWNLOAD_URL` in the module if needed.
- **Column name resilience**: GTA CSV column names have changed between releases (e.g. `implementing_jurisdiction` vs `implementing_country`). Always build a lowercase column mapping from the actual header row — never assume fixed column positions.
- **HS code format in GTA**: GTA stores HS codes as 6-digit strings, sometimes with leading zeros. Always zero-pad when comparing against prefixes.
- **Export bans vs export taxes**: Both are classified as "geopolitical_trade" but have different severity. China's 2023 graphite export controls were "Export licensing requirements" + "Export quota". Indonesia's nickel export ban was "Export ban". Severity scoring above captures this distinction.
- **Update cadence**: GTA updates weekly. Re-running `ingest-gta` weekly will pick up new interventions via `content_hash` deduplication — existing events are not re-processed.
- **Review workflow integration**: GTA events are inserted with `verified=False`. The existing admin event review UI (built in a prior phase) handles promotion. Do not auto-verify GTA events — the partner should review the most significant ones before they affect published scores.
