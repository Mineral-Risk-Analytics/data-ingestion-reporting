# Ingestion pipeline

> **Last updated: April 2026**

The platform has two ingestion paths:

1. **Pipeline-based** (`IngestionPipeline`) — for event-driven sources that emit risk signals (Federal Register, Census trade, SEC EDGAR, news). Follows the pattern: **configure → fetch → persist raw → parse → normalize → write domain rows → emit risk signals → entity resolution → score**.
2. **CLI-based** — for reference data and commodity sources that don't emit risk events (USGS, World Bank, OpenSanctions, Comtrade). Each is a standalone command invoked via `uv run bdi-ingest <command>`.

This document covers the pipeline path. See [Data sources](data-sources.md) for CLI-based sources.

## Entry points

| Trigger | How it works |
|---------|----------------|
| **HTTP** | `POST /api/v1/sources/{source_id}/ingest` with optional body `{"extra": { ... }}` |
| **CLI** | `python -m app.cli ingest <alias>` with optional `--params '{"key": "value"}'` |

Both paths:

1. Load `Source` by id (HTTP) or resolve by `source_type` (CLI).
2. Instantiate `IngestionPipeline(db)` and call **`run(source_id, params=...)`**.
3. On success: entity resolution + post-run rescoring run, then the session is **committed** inside `run`; on failure an `ingestion_runs` row is marked failed and the exception is re-raised.

## Run tracking (`IngestionRunTracker`)

`app/services/ingestion/run_tracker.py` manages **`ingestion_runs`**:

- **`start`** — status `running`, seeds `stats_json` (`items_fetched`, `items_written`, `errors`).
- **`add_stat`** — increments counters as batches progress.
- **`complete`** — sets status to `success` and `completed_at`.
- **`fail`** — status `failed`, stores truncated `error_message`.

Runs are **1:1 with a pipeline execution** for a given source at a point in time.

## Adapter contract

All adapters inherit **`SourceAdapter`** (`app/services/ingestion/base.py`):

- **`source_type`** — must match `sources.source_type` and `SourceType` enum string values.
- **`fetch(client, *, params)`** — returns **`list[FetchBundle]`**.

### `FetchBundle`

| Field | Purpose |
|-------|---------|
| `endpoint` | Request URL or logical id (e.g. `stub://news-provider`) for audit |
| `raw_body` | Exact bytes written to storage (typically JSON) |
| `items` | List of dicts the pipeline will iterate |
| `request_params` | Serialized query/body for forensics |

Adapters **must not** assume database access. Registry: **`ADAPTER_BY_TYPE`** in `app/services/ingestion/adapters/__init__.py`.

## Raw payload persistence

For each bundle the pipeline:

1. Writes **`raw_body`** under `STORAGE_ROOT` (e.g. `storage/ingestion/run-{id}/batch-{n}.json`) via **`LocalFilesystemStorage`**. This backend is swappable with Cloudflare R2 in production via the same storage abstraction in `app/utils/storage.py`.
2. Inserts **`raw_api_payloads`** with `endpoint`, `request_params_json`, `response_body_path`, `checksum` (SHA-256 of `raw_body`).

## Per–source-type handling

After each batch is stored, **`IngestionPipeline`** dispatches on **`source.source_type`**:

### Federal Register (`federal_register`)

1. Each `item` is one API document object.
2. **`parse_federal_register_document`** → `ParsedRegulation`.
3. **`_upsert_document`** → `source_documents`.
4. **`_upsert_regulation`** → `regulations`.
5. **`build_regulatory_risk_event`** → `RiskEventDraft` → insert `risk_events`.
6. `_add_risk_event` flushes the new row, then calls **entity resolution** (see below).

### Census trade (`census_trade`)

1. Each `item` wraps a Census JSON table plus `time` / `dataset_path`.
2. **`parse_census_trade_rows`** expands rows; `GeographyResolver` and `MaterialResolver` enrich partner and `material_id`.
3. One `source_document` per batch; many `trade_flows` rows; optional batch-level `build_trade_risk_event`.

### SEC EDGAR (`sec_edgar`)

1. Each `item` is a full `submissions` JSON for one CIK.
2. **`parse_sec_filing`** expands recent filings to `ParsedFiling` list.
3. Per filing: `source_document` + `build_sec_filing_event` → entity resolution.

### News stub (`news`)

1. **`StubNewsProvider`** (or injectable `NewsProviderProtocol`) returns article dicts.
2. **`parse_article`** → document + `build_news_event` → entity resolution.

Phase 2/3 adapters are registered but `fetch` raises `NotImplementedError`. Triggering them via API returns **HTTP 501**.

## Entity resolution (`_add_risk_event`)

After every `RiskEvent` row is inserted, the pipeline calls `entity_resolution.resolve_suppliers_for_event` (which reads a pre-built `CachedSupplierInfo` list) and `entity_resolution.persist_supplier_links`. This writes `risk_event_companies` junction rows with:

- `company_id` — which company is relevant
- `relevance_score` — 0.70–1.00, fed into `relevance_multiplier` during scoring
- `match_reason` — `named` | `geography` | `material` | `category_broad`

The supplier cache (`build_supplier_cache`) is built **once per run** at the start to avoid N+1 queries. The set of touched `company_id` values is tracked in `touched_company_ids` for the post-run rescore step.

## Post-ingestion scoring

After all bundles are processed and `ingestion_runs` is marked complete, the pipeline rescores every company that had new events linked to it during this run:

```python
for company_id in touched_company_ids:
    try:
        rescore_company(db, company_id, run_id=str(run.id))
    except Exception as e:
        # Log but do not fail the ingestion run — scoring failure must never
        # roll back successfully ingested events.
        logger.error(...)

db.commit()  # single commit covers both ingestion writes and new score rows
```

This means every ingestion run produces fresh `company_scores` rows for affected companies automatically. Use `POST /companies/{id}/rescore` to trigger scoring outside of an ingestion run.

## Configuration and safety checks

Before fetch:

- Source must exist and `is_active`.
- `ADAPTER_BY_TYPE` must contain the source's `source_type`.

HTTP client:

- `User-Agent` is taken from `SEC_EDGAR_USER_AGENT` (satisfies SEC policy; also used for other hosts).

## Extending the pipeline

**To add a new pipeline source (event-emitting):**

1. Add `SourceType` value and migration if needed.
2. Implement adapter `fetch` returning appropriate `FetchBundle`(s).
3. Register class in `ADAPTER_BY_TYPE`.
4. Add a branch in `IngestionPipeline.run` that calls a new `_ingest_*_items` method, passing `supplier_cache` and `touched_company_ids`.
5. Seed a `sources` row with `config_json` defaults.
6. Call `_add_risk_event` for each new event so entity resolution and scoring are wired automatically.

**To add a new CLI-based source (reference data):**

1. Create `app/services/ingestion/<source_name>.py` with a standalone function (e.g. `ingest_<source>(session, ...)`).
2. Add idempotency: use `ON CONFLICT DO NOTHING` or check-before-insert patterns.
3. Register a Typer command in `app/cli.py`.
4. Add a `hatch` script shortcut in `pyproject.toml` if useful.
5. Write tests with mocked SQLAlchemy sessions.

See also: [Data sources](data-sources.md), [Parsing & normalization](parsing-and-normalization.md), [Scoring](scoring.md).


**Order of operations for running scripts and scoring**
# 1. Reference data (once)
bdi-ingest seed-companies
bdi-ingest ingest-usgs path/to/MCS2025_World_Data.csv
bdi-ingest seed-materials
bdi-ingest seed-hs-mappings

# 2. Scoring data
bdi-ingest seed-material-exposures   # Material + Geopolitical pillars
bdi-ingest seed-regulations          # Regulatory pillar
bdi-ingest ingest-sec-edgar          # partial Financial signal

# 3. Score and view
bdi-ingest rescore-all
bdi-ingest show-scores