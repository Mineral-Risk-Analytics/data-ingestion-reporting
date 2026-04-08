# Battery Data Intelligence Engine

Backend-only **Phase 1** ingestion foundation for North American EV battery supply-chain intelligence: Federal Register, U.S. Census trade, SEC EDGAR, and a **stub** news provider. Phase 2/3 sources are registered in the database and ship as typed adapter stubs.

**Recommended runtime:** Python **3.12** (supported: **3.10+**). Dependencies: `pyproject.toml` (install with `uv` or `pip`).

## Documentation

Architecture and ingestion details live in **[`docs/`](docs/README.md)** (overview, pipeline, parsing, scoring, reports/AI, data model, and API).

## Quick start

```bash
# Start Postgres
docker compose up -d postgres

# Configure env
cp .env.example .env

# Install (pick one)
uv sync --extra dev
# or: python -m pip install -e ".[dev]"

# Migrations
alembic upgrade head

# Seed sources + demo materials/suppliers
python -m app.cli seed

# API
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Internal API base path: **`/api/v1`**.

| Endpoint | Purpose |
|----------|---------|
| `GET /api/v1/health` | Health check |
| `GET /api/v1/sources` | List configured sources |
| `POST /api/v1/sources/{id}/ingest` | Trigger ingestion (`{"extra": {...}}` body merges into source `config_json`) |
| `GET /api/v1/ingestion-runs` | Recent runs |
| `GET /api/v1/risk-events` | Risk events |
| `GET /api/v1/regulations` | Regulations |
| `GET /api/v1/suppliers` | Suppliers |
| `GET /api/v1/trade-flows` | Trade flows |

## CLI ingestion

After `seed`, resolve source IDs or use aliases:

```bash
python -m app.cli ingest federal-register --params '{"per_page": 5}'
python -m app.cli ingest census-trade
python -m app.cli ingest sec-edgar
python -m app.cli ingest news
```

**Census API:** set `CENSUS_API_KEY` in `.env` ([key signup](https://api.census.gov/data/key_signup.html)).

**SEC EDGAR:** set `SEC_EDGAR_USER_AGENT` to a descriptive contact string per [SEC fair-access guidance](https://www.sec.gov/os/accessing-edgar-data).

## Project layout

- `app/api/` — FastAPI routers  
- `app/core/` — settings, logging  
- `app/db/` — engine, session, Alembic env, `seed.py`  
- `app/models/` — SQLAlchemy 2.x models  
- `app/schemas/` — Pydantic v2 API schemas  
- `app/services/ingestion/` — adapters, parsers, normalizers, `pipeline.py`  
- `app/services/scoring/` — rule-based scores + rationale  
- `app/services/ai/` — summarization / classification / clustering protocols (stubs)  
- `app/services/reports/` — report builder placeholders  
- `app/utils/storage.py` — local filesystem raw storage (S3/R2-ready abstraction)  
- `alembic/versions/` — migrations  
- `storage/` — default root for raw payloads (gitignored)

## Phase 1 field mappings

Mappings describe how external fields land in **`source_documents`**, domain tables, and **`risk_events`**.

### 1. Federal Register API → `source_documents`, `regulations`, `risk_events`

| API field | Internal use |
|-----------|----------------|
| `document_number` | `source_documents.external_id`, `regulations.regulation_key` |
| `title` | `source_documents.title`, `regulations.title`, `risk_events.title` |
| `publication_date` | `source_documents.published_at`, `regulations.publication_date`, `risk_events.event_date` (date→datetime UTC) |
| `abstract` | `source_documents.raw_text`, `regulations.summary` |
| `html_url`, `pdf_url` / `raw_text_url` | `source_documents.url` |
| `agencies[]` | `regulations.issuing_body` (joined), `risk_events.metadata_json.agencies` |
| `type` | `regulations.status`, metadata |
| `effective_on` | `regulations.effective_date` |
| `topics[]` | `regulations.policy_theme`, geography/topics facets in `risk_events` |

Full API JSON per request is stored under `raw_api_payloads.response_body_path` (see `storage/`).

### 2. U.S. Census trade API → `source_documents`, `trade_flows`

Census returns a header row plus arrays of values. Parser normalizes rows into `ParsedTradeRow`.

| API / column | Internal use |
|--------------|----------------|
| `time` | `trade_flows.period`, batch `source_documents.external_id` suffix |
| `CTY_CODE`, `CTY_NAME` | `trade_flows.metadata_json.census_partner_code`, `partner_name`; partner resolved toward ISO2 when mapped |
| `I_COMMODITY` / `E_COMMODITY` | `trade_flows.hs_code` |
| `I_COMMODITY_LDESC` / `E_COMMODITY_LDESC` | `trade_flows.hs_description` |
| Import vs export dataset path | `trade_flows.import_export_flag` (`import` / `export`) |
| `GEN_VAL_MO` (or `ALL_VAL_MO`) | `trade_flows.trade_value_usd` |
| `QTY_1_MO`, `UNIT_QY1` | `trade_flows.quantity`, `quantity_unit` |
| Reporter | `trade_flows.reporter_country` = `US` for Phase 1 |

HS codes are mapped to `materials.id` when prefixes match seed rules (`MaterialResolver`). One **`risk_events`** row summarizes the batch (trade signal).

### 3. SEC EDGAR (`submissions`) → `source_documents`, `risk_events`, suppliers

| SEC field | Internal use |
|-----------|----------------|
| `cik`, `name`, `tickers` | `source_documents.metadata_json`; narrative excerpt for filings |
| `filings.recent.accessionNumber` | `source_documents.external_id` (normalized) |
| `filings.recent.filingDate` | `source_documents.published_at` |
| `filings.recent.form` | `source_documents.metadata_json.form`, `risk_events.metadata_json` |
| `filings.recent.primaryDocument` | primary doc filename; URL built under `www.sec.gov/Archives/edgar/data/...` |
| Derived narrative | `source_documents.raw_text` (placeholder until full-text pull) |
| **Future** | Map tickers/CIK updates into `suppliers` / `entity_links` |

### 4. News stub → `source_documents`, `risk_events`

| Stub field | Internal use |
|------------|----------------|
| `id` / `article_id` | `source_documents.external_id` |
| `title` | `source_documents.title`, `risk_events.title` |
| `source` | `source_documents.metadata_json.source` |
| `url` | `source_documents.url` |
| `published_at` | `source_documents.published_at`, `risk_events.event_date` |
| `body` / `body_text` | `source_documents.raw_text`, `risk_events.summary` |
| `entities` | `risk_events.metadata_json.entities` |
| `event_classification` | feeds `risk_events` category heuristic / labels |

Replace `StubNewsProvider` with an HTTP provider implementing `NewsProviderProtocol`.

## Risk categories (constants)

Defined in `app/constants.py`: `material_supply`, `regulatory_policy`, `supplier_operational`, `market_demand`, `infrastructure_ecosystem`.

## Makefile

`make db-up`, `make upgrade`, `make test`, `make run-api`, `make ingest-federal`, etc. (see `Makefile`).

## Tests

```bash
pytest tests/ -v
```

## Docker

`Dockerfile` runs the API with `uvicorn`. For local dev, Postgres via `docker-compose.yml` is usually enough.

## License

Proprietary / internal — adjust as needed.
