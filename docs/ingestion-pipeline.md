# Ingestion pipeline

The ingestion subsystem follows a consistent pattern: **configure → fetch → persist raw → parse → normalize → write domain rows → emit risk signals**. The class responsible for tying this together is **`IngestionPipeline`** in `app/services/ingestion/pipeline.py`.

## Entry points

| Trigger | How it works |
|---------|----------------|
| **HTTP** | `POST /api/v1/sources/{source_id}/ingest` with optional body `{"extra": { ... }}` |
| **CLI** | `python -m app.cli ingest <alias>` with optional `--params '{"key": "value"}'` |

Both paths:

1. Load `Source` by id (HTTP) or resolve by `source_type` (CLI).
2. Instantiate `IngestionPipeline(db)` and call **`run(source_id, params=...)`**.
3. On success the session is **committed** inside `run`; on failure an **`ingestion_runs`** row is marked failed and the exception is re-raised.

## Run tracking (`IngestionRunTracker`)

`app/services/ingestion/run_tracker.py` manages **`ingestion_runs`**:

- **`start`** — status `running`, seeds `stats_json` (`items_fetched`, `items_written`, `errors`).
- **`add_stat`** — increments counters as batches progress.
- **`complete`** — sets status to `success` (or supplied status) and `completed_at`.
- **`fail`** — status `failed`, stores truncated `error_message`.

Runs are **1:1 with a pipeline execution** for a given source at a point in time, not per logical document.

## Adapter contract

All adapters inherit **`SourceAdapter`** (`app/services/ingestion/base.py`):

- **`source_type`** — must match `sources.source_type` and `SourceType` enum string values.
- **`fetch(client, *, params)`** — returns **`list[FetchBundle]`**.

### `FetchBundle`

| Field | Purpose |
|-------|---------|
| `endpoint` | Request URL or logical id (e.g. `stub://news-provider`) for audit |
| `raw_body` | Exact bytes written to disk (typically JSON) |
| `items` | List of dicts the pipeline will iterate (one FR document, one Census table wrapper, one SEC submissions blob per CIK, etc.) |
| `request_params` | Serialized query/body for forensics |

Adapters **must not** assume database access. SEC and Federal Register use **`httpx.Client`** passed in (shared User-Agent from settings).

Registry: **`ADAPTER_BY_TYPE`** in `app/services/ingestion/adapters/__init__.py` maps `source_type` string → adapter class. **`NewsAdapter`** is constructed explicitly in the pipeline (optional provider injection).

## Raw payload persistence

For each bundle the pipeline:

1. Writes **`raw_body`** under `STORAGE_ROOT` (e.g. `storage/ingestion/run-{id}/batch-{n}.json`) via **`LocalFilesystemStorage`**.
2. Inserts **`raw_api_payloads`** with `endpoint`, `request_params_json`, `response_body_path`, `checksum` (SHA-256 of `raw_body`).

This gives a reproducible audit trail independent of normalized rows. Swapping to S3/R2 later means implementing another backend behind the same storage abstraction in `app/utils/storage.py`.

## Per–source-type handling

After each batch is stored, **`IngestionPipeline`** dispatches on **`source.source_type`**:

### Federal Register (`federal_register`)

1. Each `item` is one API **document** object.
2. **`parse_federal_register_document`** → `ParsedRegulation`.
3. **`_upsert_document`** → `source_documents` (unique on `source_id` + `external_id`).
4. **`_upsert_regulation`** → `regulations`.
5. **`build_regulatory_risk_event`** → **`RiskEventDraft`** → insert **`risk_events`**.

### Census trade (`census_trade`)

1. Each `item` wraps **`census_table`** (Census JSON table) plus `time` / `dataset_path`.
2. **`parse_census_trade_rows`** expands rows; **`GeographyResolver`** and **`MaterialResolver`** enrich partner and `material_id`.
3. One **`source_document`** per batch (tabular export).
4. Many **`trade_flows`** rows; optional batch-level **`build_trade_risk_event`**.

### SEC EDGAR (`sec_edgar`)

1. Each `item` is a full **`submissions`** JSON for one CIK.
2. **`parse_sec_filing`** expands **recent** filings to **`ParsedFiling`** list.
3. Per filing: **`source_document`** + **`build_sec_filing_event`**.

### News stub (`news`)

1. **`StubNewsProvider`** (or injectable **`NewsProviderProtocol`**) returns article dicts.
2. **`parse_article`** → document + **`build_news_event`**.

Phase 2/3 adapters are registered but **`fetch`** raises **`NotImplementedError`**. Triggering them via API returns **HTTP 501** with the error detail.

## Configuration and safety checks

Before fetch:

- Source must exist and **`is_active`**.
- **`ADAPTER_BY_TYPE`** must contain the source’s `source_type`.

HTTP client:

- **`User-Agent`** is taken from **`SEC_EDGAR_USER_AGENT`** (satisfies SEC policy; also used for other hosts).

## Extending the pipeline

1. Add **`SourceType`** value and migration if needed.
2. Implement adapter **`fetch`** returning appropriate **`FetchBundle`(s)**.
3. Register class in **`ADAPTER_BY_TYPE`**.
4. Add a branch in **`IngestionPipeline.run`** (or refactor to a strategy dict) that calls new `_ingest_*_items` using existing parsers/normalizers or new ones.
5. Seed a **`sources`** row with `config_json` defaults.

See also: [Parsing & normalization](parsing-and-normalization.md), [Data model & internal API](data-model-and-api.md).
