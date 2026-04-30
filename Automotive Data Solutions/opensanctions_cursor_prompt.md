Create an OpenSanctions ingestion script for the battery-data-intelligence-engine FastAPI project.

## Context

OpenSanctions publishes a free bulk download of consolidated sanctions lists (OFAC SDN, EU Financial Sanctions, UN Security Council, and ~100 others). The data is updated daily. This ingestion serves two purposes:

1. **Company matching** — check if any company in the `companies` table appears on a sanctions list. Each match becomes a `RiskEvent` of type `"sanctions_listing"` linked via `RiskEventCompany`.
2. **Geography signalling** — track the volume of sanctioned entities by country as `RiskEvent` rows tagged via `RiskEventGeography`, feeding the `geopolitical_trade_score` pillar of `MaterialGeographyRiskScore` for high-concentration-geo countries (CN, CD, RU, IR, KP).

The schema uses an append-only `risk_events` table with a `content_hash` column (SHA-256 of title + summary + event_date) for idempotency. No raw sanctions data is persisted — only structured `RiskEvent` rows for confirmed matches and geography signals.

## What to build

### 1. `app/services/ingestion/opensanctions.py` — ingestion module

Create this file.

#### 1a. Download URL

```python
OPENSANCTIONS_CSV_URL = (
    "https://data.opensanctions.org/datasets/latest/sanctions/targets.simple.csv"
)
```

This is the free consolidated bulk export. It requires no API key. Use `httpx` (already in project deps) to stream-download.

#### 1b. Data format

The CSV has these columns (all string, multi-values pipe-separated):
- `id` — OpenSanctions entity ID (e.g. `NK-6FWxqvEb4fNrMj9yS7bZjX`)
- `schema` — entity type: `Person`, `Company`, `Organization`, `LegalEntity`, `Vessel`, etc.
- `name` — primary display name
- `aliases` — pipe-separated alternative names (may be empty)
- `birth_date` — for persons only (ignore)
- `countries` — pipe-separated ISO2 codes (e.g. `CN|RU`)
- `datasets` — pipe-separated source dataset names (e.g. `us_ofac_sdn|eu_fsf`)
- `first_seen` — ISO date string
- `last_seen` — ISO date string

#### 1c. Download and parse

```python
def download_sanctions_csv(url: str = OPENSANCTIONS_CSV_URL, timeout: int = 120) -> io.BytesIO:
    """Stream-download the sanctions CSV and return an in-memory BytesIO buffer.

    File is ~15–30 MB. Do not write to disk.
    Raises httpx.HTTPStatusError on non-2xx responses.
    """
```

```python
def parse_sanctions_csv(buf: io.BytesIO) -> list[dict]:
    """Parse the CSV buffer into a list of sanctioned entity dicts.

    Only keeps rows where schema is one of:
        Company, Organization, LegalEntity, PublicBody

    (Excludes Person, Vessel, Aircraft — not relevant for company matching.)

    Returns dicts with keys:
        opensanctions_id  (str)
        name              (str — primary name)
        aliases           (list[str] — split on pipe, stripped, deduped, lowercased)
        countries         (list[str] — ISO2 codes)
        datasets          (list[str])
        first_seen        (datetime.date | None)
        last_seen         (datetime.date | None)
    """
```

Use Python's `csv.DictReader` with `io.TextIOWrapper(buf, encoding="utf-8")`. Keep only the schema types listed above. The CSV is large (~150K–500K rows); filter in one pass — do not load all rows into memory before filtering.

#### 1d. Company matching

```python
def _build_name_index(entities: list[dict]) -> dict[str, list[dict]]:
    """Build a lookup dict: normalised_name → list of matching sanctioned entities.

    Normalisation: lowercase, strip, collapse internal whitespace.
    Index includes both `name` and each entry in `aliases`.
    """
```

```python
def match_companies(
    session: Session,
    entities: list[dict],
) -> list[tuple[Any, list[dict]]]:
    """Return (Company ORM row, list of matching sanctioned entities) pairs.

    Queries all Company rows and CompanyAlias rows. Normalises names the same
    way as _build_name_index(). Returns only companies with at least one match.
    """
```

Query `companies` and `company_aliases` in two separate bulk queries (not per-company). Do not do N+1 lookups.

#### 1e. Content hash helper

```python
def _content_hash(title: str, summary: str, event_date: Optional[datetime]) -> str:
    """SHA-256 of '{title}|{summary}|{event_date.isoformat()}' (date portion only if present)."""
    import hashlib
    parts = f"{title}|{summary}|{event_date.date().isoformat() if event_date else ''}"
    return hashlib.sha256(parts.encode()).hexdigest()
```

#### 1f. Main upsert function

```python
def ingest_opensanctions(
    session: Session,
    url: str = OPENSANCTIONS_CSV_URL,
    high_concentration_geos: Optional[list[str]] = None,
) -> dict[str, int]:
    """Download, parse, match, and insert OpenSanctions data as RiskEvents.

    Args:
        session: SQLAlchemy session.
        url: Override download URL.
        high_concentration_geos: ISO2 codes to treat as high-risk geographies.
            Defaults to ["CN", "CD", "RU", "IR", "KP"] if not provided.

    Returns:
        {
            "company_events_inserted": int,
            "company_events_skipped_existing": int,
            "geography_events_inserted": int,
            "companies_matched": int,
            "total_entities_parsed": int,
        }
    """
```

Implementation steps inside this function:

**Step 1 — Download and parse**

Stream-download and parse the CSV. Log the entity count.

**Step 2 — Company match events**

For each `(company, matched_entities)` pair from `match_companies()`:
- Build a deduplicated list of datasets across all matched entities
- Construct a `RiskEvent`:
  - `event_type = "sanctions_listing"`
  - `event_date = datetime.utcnow()` (use today — sanctions are ongoing)
  - `title = f"{company.canonical_name} — Active Sanctions Match"`
  - `summary = f"Matched against {len(matched_entities)} sanctioned entity record(s) in: {', '.join(sorted(datasets))}"`
  - `severity_score = 1.0` — sanctions are the highest severity signal
  - `confidence_score = 1.0` — direct name match
  - `risk_categories_json = ["geopolitical_trade"]`
  - `geography_json = {"primary": matched_entities[0]["countries"][0] if matched_entities[0]["countries"] else None}`
  - `metadata_json = {"opensanctions_ids": [e["opensanctions_id"] for e in matched_entities], "datasets": sorted(datasets)}`
  - `content_hash = _content_hash(title, summary, event_date)` — this is the idempotency key
- Check if a `RiskEvent` with this `content_hash` already exists. If yes, skip (increment `company_events_skipped_existing`).
- If no, insert `RiskEvent`, then:
  - Insert `RiskEventCompany(risk_event_id=..., company_id=company.id, relevance_score=1.0, match_reason="named_company")`
  - For each country in the matched entities' country lists, insert `RiskEventGeography(country_code=..., geography_context="primary", relevance_score=1.0)`

**Step 3 — Geography signal events**

For each country in `high_concentration_geos`, count how many sanctioned entities have that country in their `countries` list. If count > 0:
- Construct a `RiskEvent`:
  - `event_type = "geography_sanctions_exposure"`
  - `event_date = datetime.utcnow()`
  - `title = f"{country} — {count} Sanctioned Entities (OpenSanctions)"`
  - `summary = f"OpenSanctions bulk data contains {count} company/organisation records linked to {country} across datasets: {', '.join(sorted(dataset_sample))}"`
    (where `dataset_sample` is the top 5 most common datasets for that country's entities)
  - `severity_score = min(1.0, count / 500)` — scale: 500 entities → severity 1.0
  - `confidence_score = 0.85` — country attribution in OpenSanctions is high-quality but not perfect
  - `risk_categories_json = ["geopolitical_trade"]`
  - `geography_json = {"primary": country}`
  - `metadata_json = {"sanctioned_entity_count": count, "datasets": sorted(dataset_sample)}`
  - `content_hash = _content_hash(title, summary, event_date)`
- Check `content_hash` for existing row. If already exists, skip.
- If new, insert `RiskEvent` + `RiskEventGeography(country_code=country, geography_context="primary", relevance_score=1.0)`

**Step 4 — Commit**

`session.flush()` every 100 inserts. `session.commit()` at end.

Use `structlog` throughout (`log = structlog.get_logger()` at module level). Log skipped duplicates at DEBUG, new insertions at INFO.

### 2. `app/cli.py` — add `ingest-opensanctions` command

```python
@app.command("ingest-opensanctions")
def ingest_opensanctions_cmd(
    url: Optional[str] = typer.Option(
        None,
        "--url",
        help="Override the OpenSanctions bulk CSV URL.",
    ),
    geos: Optional[str] = typer.Option(
        None,
        "--geos",
        help='Comma-separated ISO2 high-concentration geo codes. Default: "CN,CD,RU,IR,KP"',
    ),
) -> None:
    """Match companies against OpenSanctions consolidated sanctions lists.

    Downloads the free daily bulk export (no API key required).
    Creates RiskEvent rows for company name matches and geography-level signals.
    Idempotent: skips events whose content_hash already exists.
    Re-run daily or weekly to catch new listings.
    """
    from app.services.ingestion.opensanctions import OPENSANCTIONS_CSV_URL, ingest_opensanctions

    geo_list = [g.strip().upper() for g in geos.split(",")] if geos else None

    s = _session()
    try:
        result = ingest_opensanctions(
            session=s,
            url=url or OPENSANCTIONS_CSV_URL,
            high_concentration_geos=geo_list,
        )
        typer.echo(json.dumps({"ok": True, **result}, indent=2))
    except Exception as exc:
        typer.echo(json.dumps({"ok": False, "error": str(exc)}), err=True)
        raise typer.Exit(code=1)
    finally:
        s.close()
```

### 3. `pyproject.toml` — add hatch script

```toml
ingest-opensanctions = "bdi-ingest ingest-opensanctions"
```

No new dependencies — only `httpx` (already present) and Python stdlib (`csv`, `io`, `hashlib`, `datetime`).

### 4. Tests — `tests/test_opensanctions.py`

Create this file. Do not make real HTTP requests.

**Test `parse_sanctions_csv`:**
- Build a minimal in-memory CSV with one Person row, one Company row, one Organization row
- Verify Person row is filtered out
- Verify Company and Organization rows are returned
- Verify aliases are split on pipe and lowercased
- Verify countries are split on pipe

**Test `_build_name_index`:**
- Verify primary name and all aliases appear as keys
- Verify normalisation: case-insensitive, whitespace-collapsed

**Test `match_companies`:**
- Mock a session returning one Company with `canonical_name = "Glencore International"` and one CompanyAlias of `"Glencore PLC"`
- Provide an entity list with `name = "GLENCORE INTERNATIONAL AG"` and `aliases = ["glencore plc"]`
- Verify match is found for both the alias hit

**Test `ingest_opensanctions` (integration-style, mocked session):**
- Mock `download_sanctions_csv` and `parse_sanctions_csv`
- Use a mock SQLAlchemy session
- Verify `company_events_inserted` = 1 for a single company match
- Verify running twice produces `company_events_skipped_existing` = 1 (content_hash check)

## Constraints

- Do not store raw sanctions entity records in the database — only structured `RiskEvent` rows
- Do not add any new dependencies — use only stdlib and `httpx`
- The CSV is large. Parse in a streaming fashion: do not `readlines()` or `.read()` the entire buffer into a string before parsing
- `RiskEvent` rows are append-only — never update or delete existing rows
- All geography codes must be uppercase ISO2 — strip and upper() any codes from the CSV before using them
- Use `structlog` for all logging, following the module-level `log = structlog.get_logger()` pattern used elsewhere in the project
- `content_hash` is the idempotency mechanism — check it before every insert, not after

## Reference: ORM models used

```python
# app/models/regulatory.py
class RiskEvent(Base):
    __tablename__ = "risk_events"
    id: Mapped[int]
    source_document_id: Mapped[Optional[int]]   # leave None for OpenSanctions
    event_type: Mapped[str]                      # "sanctions_listing" | "geography_sanctions_exposure"
    event_date: Mapped[Optional[datetime]]
    title: Mapped[str]
    summary: Mapped[Optional[str]]
    severity_score: Mapped[Optional[float]]      # 0.0–1.0
    confidence_score: Mapped[Optional[float]]    # 0.0–1.0
    risk_categories_json: Mapped[Optional[Any]]  # JSONB array
    geography_json: Mapped[Optional[Any]]        # JSONB {"primary": "CN", ...}
    content_hash: Mapped[Optional[str]]          # SHA-256 — idempotency key
    metadata_json: Mapped[Optional[Any]]         # JSONB

class RiskEventCompany(Base):
    __tablename__ = "risk_event_companies"
    risk_event_id: Mapped[int]
    company_id: Mapped[uuid.UUID]
    relevance_score: Mapped[float]               # 1.0 for direct name match
    match_reason: Mapped[Optional[str]]          # "named_company"

class RiskEventGeography(Base):
    __tablename__ = "risk_event_geographies"
    risk_event_id: Mapped[int]
    country_code: Mapped[str]                    # ISO2
    geography_context: Mapped[str]               # "primary"
    relevance_score: Mapped[float]               # 1.0

# app/models/company.py
class Company(Base):
    __tablename__ = "companies"
    id: Mapped[uuid.UUID]
    canonical_name: Mapped[str]

class CompanyAlias(Base):
    __tablename__ = "company_aliases"
    company_id: Mapped[uuid.UUID]
    alias: Mapped[str]
```

## Reference: existing CLI pattern

Follow `ingest_worldbank_cmd` in `app/cli.py` for session management, error handling, and JSON output format.

## Important caveats to implement as code comments

Add a module docstring to `opensanctions.py` that includes:
- Note that name matching is fuzzy by normalisation only — there will be false positives. A human review step is recommended before acting on matches.
- Note that the `companies` table may be empty on first run, in which case company matching produces zero events. This is expected — run after seeding companies from SEC EDGAR or manual data.
- Note that geography events are based on entity count, not risk severity — a country with 1,000 sanctioned shell companies is not necessarily riskier than one with 10 major state-owned enterprises. Treat geography event severity as a volume signal, not a quality signal.
