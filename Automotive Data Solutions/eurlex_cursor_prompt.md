Create an EUR-Lex regulation ingestion script for the battery-data-intelligence-engine FastAPI project.

## Context

The `regulations` table stores structured regulatory instruments that feed the **Regulatory Compliance pillar (20% weight)** of the supply chain risk scoring model. Currently, the only regulations in the database are manually seeded via `seed_regulations.py` — a small static list with no connection to an authoritative source.

EUR-Lex is the official EU law repository. It provides free access to EU Battery Regulation (2023/1542), the Critical Raw Materials Act (CRMA), CBAM, and other instruments that directly govern battery supply chain compliance. Without these records, regulatory compliance scores for EU-relevant materials are incomplete or zero.

The project already has `Regulation`, `RegulationMaterialScope`, `RegulationGeographyScope`, `SourceDocument`, and `Source` ORM models. No schema changes are needed.

---

## Approach

EUR-Lex does not have a simple REST API that returns structured JSON. The practical approach for this use case is a **hardcoded manifest** of the target regulations (we know exactly which ones matter for battery supply chains), combined with fetching each regulation's summary text from the EUR-Lex HTML endpoint for the `summary` field. This is more reliable than a dynamic search because:

- Battery supply chain regulations are a well-defined, small set
- EUR-Lex HTML structure is stable for `CELEX`-based URLs
- Dynamic keyword search would require NLP classification to avoid noise

---

## What to build

### 1. `app/services/ingestion/eurlex.py` — ingestion module

Create this file.

#### 1a. Regulation manifest

Define the target regulations as a list of dicts. These are the regulations that directly affect battery material supply chains:

```python
BATTERY_REGULATIONS: list[dict] = [
    {
        "regulation_key": "EU_BATTERY_REG_2023",
        "celex": "32023R1542",
        "title": "EU Battery Regulation (EU) 2023/1542 — batteries and waste batteries",
        "issuing_body": "European Parliament and Council",
        "geography": "EU",
        "status": "effective",
        "publication_date": date(2023, 7, 28),
        "effective_date": date(2024, 8, 18),
        "policy_theme": "battery_lifecycle_compliance",
        "material_scopes": [
            # (canonical_name, scope_type)
            ("Lithium", "disclosure_required"),
            ("Cobalt", "disclosure_required"),
            ("Nickel", "disclosure_required"),
            ("Manganese", "disclosure_required"),
            ("Natural Graphite", "disclosure_required"),
        ],
        "geography_scopes": [
            # (country_code, scope_type)
            ("EU", "jurisdiction"),
        ],
    },
    {
        "regulation_key": "CRMA_2024",
        "celex": "32024R1252",
        "title": "Critical Raw Materials Act (EU) 2024/1252 — ensuring secure supply of critical raw materials",
        "issuing_body": "European Parliament and Council",
        "geography": "EU",
        "status": "effective",
        "publication_date": date(2024, 5, 23),
        "effective_date": date(2024, 5, 23),
        "policy_theme": "critical_materials_supply_security",
        "material_scopes": [
            # All six battery-relevant materials are in the EU's 17 Strategic Raw
            # Materials list (CRMA Annex II). Strategic classification carries
            # binding 2030 benchmarks: ≥10% domestic extraction, ≥40% processing,
            # ≥15% recycling. Use scope_type "strategic_raw_material" so scoring
            # queries can distinguish this higher-burden tier from plain "covered".
            ("Lithium",          "strategic_raw_material"),
            ("Cobalt",           "strategic_raw_material"),
            ("Nickel",           "strategic_raw_material"),
            ("Manganese",        "strategic_raw_material"),
            ("Natural Graphite", "strategic_raw_material"),
            ("Copper",           "strategic_raw_material"),
        ],
        "geography_scopes": [
            ("EU", "jurisdiction"),
        ],
    },
    {
        "regulation_key": "EU_CBAM",
        "celex": "32023R0956",
        "title": "Carbon Border Adjustment Mechanism (EU) 2023/956 — CBAM",
        "issuing_body": "European Parliament and Council",
        "geography": "EU",
        "status": "effective",
        "publication_date": date(2023, 5, 16),
        "effective_date": date(2023, 10, 1),
        "policy_theme": "carbon_pricing_trade",
        "material_scopes": [
            ("Aluminum", "covered"),
            ("Nickel", "covered"),
        ],
        "geography_scopes": [
            ("EU", "jurisdiction"),
            ("CN", "targeted_country"),
            ("RU", "targeted_country"),
        ],
    },
    {
        "regulation_key": "EU_CSDDD",
        "celex": "32024L1760",
        "title": "Corporate Sustainability Due Diligence Directive (EU) 2024/1760 — CSDDD",
        "issuing_body": "European Parliament and Council",
        "geography": "EU",
        "status": "enacted",
        "publication_date": date(2024, 7, 5),
        "effective_date": date(2027, 7, 26),
        "policy_theme": "supply_chain_due_diligence",
        "material_scopes": [
            ("Cobalt", "disclosure_required"),
            ("Lithium", "disclosure_required"),
            ("Natural Graphite", "disclosure_required"),
        ],
        "geography_scopes": [
            ("EU", "jurisdiction"),
            ("CD", "targeted_country"),
            ("CN", "targeted_country"),
        ],
    },
    {
        "regulation_key": "EU_CONFLICT_MINERALS",
        "celex": "32017R0821",
        "title": "EU Conflict Minerals Regulation (EU) 2017/821 — responsible sourcing of minerals",
        "issuing_body": "European Parliament and Council",
        "geography": "EU",
        "status": "effective",
        "publication_date": date(2017, 5, 17),
        "effective_date": date(2021, 1, 1),
        "policy_theme": "responsible_sourcing",
        "material_scopes": [
            ("Cobalt", "disclosure_required"),
        ],
        "geography_scopes": [
            ("EU", "jurisdiction"),
            ("CD", "targeted_country"),
        ],
    },
    {
        "regulation_key": "EU_REACH_COBALT",
        "celex": "32006R1907",
        "title": "REACH Regulation (EC) 1907/2006 — Registration, Evaluation, Authorisation of Chemicals (cobalt compounds SVHC)",
        "issuing_body": "European Parliament and Council",
        "geography": "EU",
        "status": "effective",
        "publication_date": date(2006, 12, 18),
        "effective_date": date(2009, 6, 1),
        "policy_theme": "chemicals_regulatory",
        "material_scopes": [
            ("Cobalt", "restricted"),
        ],
        "geography_scopes": [
            ("EU", "jurisdiction"),
        ],
    },
]
```

#### 1b. EUR-Lex summary fetcher

```python
EURLEX_HTML_URL = "https://eur-lex.europa.eu/legal-content/EN/TXT/HTML/?uri=CELEX:{celex}"

def fetch_eurlex_summary(celex: str, timeout: int = 30) -> Optional[str]:
    """Fetch the regulation's introductory text from EUR-Lex HTML.

    Uses CELEX number to construct the URL. Extracts the first 800 characters
    of readable text from the document preamble (the <p> elements inside
    the main content area, before the articles begin).

    Returns a truncated plain-text summary, or None if the fetch fails or
    the content cannot be parsed. Never raises — log warning and return None.
    """
```

Use `httpx.get()` (already in project deps). Parse the response HTML with Python's `html.parser` via `html.unescape` and a minimal tag stripper — do NOT add `beautifulsoup4` as a dependency. Strip HTML tags manually using `re.sub(r'<[^>]+>', ' ', html_text)` then normalise whitespace. Extract the first 800 characters of non-empty content as the summary.

#### 1c. Source management

```python
def _get_or_create_eurlex_source(session: Session) -> int:
    """Get or create the Source row for EUR-Lex. Returns source.id."""
    # name="EUR-Lex", source_type="eurlex", phase="1"
    # config_json={"base_url": "https://eur-lex.europa.eu", "method": "manifest"}
```

#### 1d. Main ingest function

```python
def ingest_eurlex(
    session: Session,
    fetch_summaries: bool = True,
) -> dict[str, int]:
    """Upsert EUR-Lex battery regulations into the regulations table.

    Args:
        session: SQLAlchemy session.
        fetch_summaries: If True (default), fetch regulation summary text from
                         EUR-Lex HTML. Set False to run without network (tests).

    Returns:
        {
            "inserted": int,       # new Regulation rows created
            "updated": int,        # existing rows refreshed (summary text)
            "skipped": int,        # rows where regulation_key already exists and was not updated
            "material_scopes": int,
            "geography_scopes": int,
        }
    """
```

Implementation:

1. Call `_get_or_create_eurlex_source()` once.

2. Build a material name → material_id lookup dict by querying `materials` table once (WHERE canonical_name IN all material names referenced in the manifest). Log a warning for any material in the manifest that is not found in the DB.

3. For each regulation in `BATTERY_REGULATIONS`:
   - Check if a `Regulation` with `regulation_key` already exists.
   - If it does NOT exist:
     - If `fetch_summaries=True`, call `fetch_eurlex_summary(celex)` and store the result as `summary`.
     - Create a `SourceDocument`:
       - `external_id = f"eurlex_{celex}"`
       - `title = regulation["title"]`
       - `document_type = "regulation"`
       - `metadata_json = {"celex": celex, "url": EURLEX_HTML_URL.format(celex=celex)}`
     - Create the `Regulation` row. Set `verified=True` — manifest regulations are manually reviewed.
     - Increment `inserted`.
   - If it DOES exist but `summary` is None and `fetch_summaries=True`:
     - Fetch and update `summary` only. Increment `updated`.
   - If it exists and has a summary: increment `skipped`.
   - Whether inserting or skipping, upsert `RegulationMaterialScope` and `RegulationGeographyScope`:
     - For each material scope: look up `material_id` from the prebuilt dict. Skip if not found (log warning). Use INSERT … ON CONFLICT DO NOTHING (via `session.merge()` or check-then-insert).
     - For each geography scope: check for existing `(regulation_id, country_code)` before inserting.
   - Increment `material_scopes` and `geography_scopes` counters.

4. `session.commit()` once at the end.

Log each regulation at INFO level. Log any EUR-Lex fetch failure at WARNING (never abort the run for a failed summary fetch).

### 2. `app/cli.py` — add `ingest-eurlex` command

```python
@app.command("ingest-eurlex")
def ingest_eurlex_cmd(
    no_fetch: bool = typer.Option(
        False,
        "--no-fetch",
        help="Skip fetching summary text from EUR-Lex (faster, no network required).",
    ),
) -> None:
    """Upsert EU battery supply chain regulations from the EUR-Lex manifest.

    Seeds the regulations table with the EU Battery Regulation (2023/1542),
    CRMA (2024/1252), CBAM, CSDDD, Conflict Minerals Regulation, and REACH
    cobalt restrictions — along with their material and geography scope records.

    Idempotent: re-running will not duplicate rows. Fetches plain-text summaries
    from EUR-Lex HTML unless --no-fetch is passed.

    Run this after seed-materials so material_id lookups resolve correctly:
      bdi-ingest seed-materials
      bdi-ingest ingest-eurlex
    """
    from app.services.ingestion.eurlex import ingest_eurlex

    s = _session()
    try:
        result = ingest_eurlex(session=s, fetch_summaries=not no_fetch)
        typer.echo(json.dumps({"ok": True, **result}, indent=2))
    except Exception as exc:
        typer.echo(json.dumps({"ok": False, "error": str(exc)}), err=True)
        raise typer.Exit(code=1)
    finally:
        s.close()
```

### 3. `pyproject.toml` — add hatch scripts

```toml
ingest-eurlex = "bdi-ingest ingest-eurlex"
ingest-eurlex-no-fetch = "bdi-ingest ingest-eurlex --no-fetch"
```

No new dependencies — only `httpx` (already present) and stdlib (`re`, `html`).

### 4. Tests — `tests/test_eurlex.py`

Create this file. Do not make real HTTP requests.

**Test `fetch_eurlex_summary` (mocked):**
- Mock `httpx.get` to return a minimal HTML string with known paragraph text
- Verify the function returns a non-empty string with HTML tags stripped
- Mock `httpx.get` to raise `httpx.RequestError` — verify the function returns `None` (never raises)

**Test `ingest_eurlex`:**
- Use an in-memory SQLite session (or mock)
- Pre-seed the `materials` table with Lithium, Cobalt, Nickel, Manganese, Natural Graphite, Copper, Aluminum
- Call `ingest_eurlex(session, fetch_summaries=False)`
- Verify `inserted == len(BATTERY_REGULATIONS)` on first run
- Verify `RegulationMaterialScope` and `RegulationGeographyScope` rows are created
- Verify `skipped == len(BATTERY_REGULATIONS)` on second run (idempotency)
- Verify `Regulation.verified == True` for all inserted rows

## Constraints

- Do not add `beautifulsoup4` or any HTML parsing library — use stdlib `re` and `html.unescape` only
- The manifest is the source of truth for material and geography scopes — do not attempt to parse scopes from EUR-Lex HTML
- `Regulation.verified` must be `True` for all manifest regulations — they are manually reviewed by the team
- Never hard-delete or overwrite existing `RegulationMaterialScope` / `RegulationGeographyScope` rows — use check-then-insert pattern
- All logging must use `structlog` (module-level `log = structlog.get_logger()`)
- EUR-Lex HTML fetches must fail gracefully — a network error should log a warning and allow the regulation to be inserted without a summary, not abort the run
- The EUR-Lex HTML URL structure uses CELEX number: `https://eur-lex.europa.eu/legal-content/EN/TXT/HTML/?uri=CELEX:{celex}` — this is a stable URL format that has been consistent since 2014

## Reference: ORM models used

```python
# app/models/regulatory.py
class Regulation(Base):
    __tablename__ = "regulations"
    __table_args__ = (UniqueConstraint("regulation_key", name="uq_regulation_key"),)

    id: Mapped[int]
    source_document_id: Mapped[Optional[int]]   # FK → source_documents.id
    regulation_key: Mapped[str]                  # unique stable key
    title: Mapped[Optional[str]]
    issuing_body: Mapped[Optional[str]]
    geography: Mapped[Optional[str]]             # "EU", "US", ISO2
    policy_theme: Mapped[Optional[str]]
    status: Mapped[Optional[str]]                # proposed | enacted | effective | superseded
    publication_date: Mapped[Optional[date]]
    effective_date: Mapped[Optional[date]]
    summary: Mapped[Optional[str]]
    metadata_json: Mapped[Optional[Any]]
    verified: Mapped[bool]                       # server_default="false"

class RegulationMaterialScope(Base):
    __tablename__ = "regulation_material_scope"
    __table_args__ = (UniqueConstraint("regulation_id", "material_id", name="uq_reg_material"),)

    id: Mapped[int]
    regulation_id: Mapped[int]   # FK → regulations.id
    material_id: Mapped[int]     # FK → materials.id
    scope_type: Mapped[str]      # covered | restricted | banned | disclosure_required
    notes: Mapped[Optional[str]]

class RegulationGeographyScope(Base):
    __tablename__ = "regulation_geography_scope"
    __table_args__ = (UniqueConstraint("regulation_id", "country_code", name="uq_reg_geography"),)

    id: Mapped[int]
    regulation_id: Mapped[int]
    country_code: Mapped[str]   # ISO2 or "EU"
    scope_type: Mapped[str]     # jurisdiction | origin_country | targeted_country

# app/models/documents.py
class SourceDocument(Base):
    __tablename__ = "source_documents"
    __table_args__ = UniqueConstraint("source_id", "external_id")
    id: Mapped[int]
    source_id: Mapped[int]
    external_id: Mapped[str]       # "eurlex_{celex}" — idempotency key
    title: Mapped[Optional[str]]
    document_type: Mapped[str]     # "regulation"
    metadata_json: Mapped[Optional[Any]]

# app/models/source.py
class Source(Base):
    __tablename__ = "sources"
    id: Mapped[int]
    name: Mapped[str]              # "EUR-Lex"
    source_type: Mapped[str]       # "eurlex"
    phase: Mapped[str]             # "1"
    config_json: Mapped[Optional[Any]]
```

## Reference: existing CLI pattern

Follow `ingest_usgs_cmd` in `app/cli.py` for session management, error handling, and JSON output format.

## Important implementation notes (add as module docstring)

- **Manifest-first approach**: This ingester targets a small, known set of regulations. It is not a general EUR-Lex crawler. When new regulations become relevant (e.g. a future EU Critical Minerals Partnership regulation), add them to the `BATTERY_REGULATIONS` manifest.
- **CELEX number format**: `3` (regulation) + `YYYY` (year) + `R` (regulation type) + `NNNN` (number). Directives use `L`. This format has been stable since the 1990s.
- **Scoring connection**: `regulatory_risk.py` has a `COMPLIANCE_OBLIGATIONS` dict keyed by `regulation_key`. Keys in that dict must exactly match the `regulation_key` values used here. The current dict includes `"EU_BATTERY_REG_2023"` (20 pts) and `"CRMA_2024"` (15 pts) — both wired. `EU_CBAM` is a candidate for a future uplift entry. Do not use abbreviated keys like `"EU_BATTERY_REG"` — the scoring lookup is exact-match.
- **Update cadence**: Run `ingest-eurlex` when a new EU regulation is enacted. No need to run more than quarterly — these instruments do not change frequently.
- **Geography code "EU"**: Used as a bloc identifier in `regulation_geography_scope.country_code`. The scoring engine in `regulatory_risk.py` treats "EU" as applicable to all 27 member states when querying by geography.
