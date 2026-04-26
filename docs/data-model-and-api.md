# Data model and internal API

> **Last updated: April 2026**

This document summarises the **primary table groups** touched by the pipeline and the **HTTP surface** used to operate the system. For the full schema with column-level detail, see [Database architecture](database_architecture.md).

## Core entity groups

### Platform and auth

- **`tenants`** — One row per customer organisation (`clerk_org_id`, `plan`). All multi-tenant data traces back here.
- **`users`** — Platform users authenticated via Clerk; belong to a tenant (`role`: `owner`, `admin`, `member`, `viewer`).
- **`usage_events`** — Append-only log of user actions for billing and audit.

### Domain configuration

- **`supply_chain_contexts`** — Externalises scoring constants (pillar weights, high-concentration geographies, HS code prefixes, supply chain stage values) per domain. Seeded with the EV battery domain at migration time.

### Supply chain entities

- **`companies`** — Canonical entity for every participant in the supply chain (miners, refiners, cell makers, OEMs). UUID PK. Self-references for parent/subsidiary relationships via `parent_company_id`.
- **`company_aliases`** — Alternative names, tickers, and legacy names used by entity resolution to match incoming raw text.
- **`facilities`** — Physical locations (mines, factories) associated with a company, with optional lat/lon.
- **`materials`** — Critical minerals and compounds, with IRA and EU CRMA criticality flags and HS code lists.
- **`company_material_exposures`** — Which materials each company depends on, at which supply chain stage, and from which source geography.
- **`company_supply_relationships`** — Direct buyer–supplier relationships between companies, optionally scoped to a material.
- **`company_vehicle_models`** + **`vehicle_model_chemistries`** — Time-windowed product-level chemistry exposure for OEMs (added in migration 007).

### Source and provenance

- **`sources`** — Catalogue of feeds (`source_type`, `phase`, `is_active`, `config_json`).
- **`ingestion_runs`** — One run per pipeline execution; `stats_json`, `error_message`, timestamps.
- **`raw_api_payloads`** — Links to stored raw bytes (`response_body_path`, checksum, request metadata).
- **`source_documents`** — One logical document per `(source_id, external_id)`.
- **`document_chunks`** — Text chunks from source documents with 1536-dim pgvector embeddings for semantic search.

### Regulatory and risk

- **`regulations`** — Extracted regulatory records (Federal Register, EU directives via EUR-Lex). Unique on `regulation_key`.
- **`risk_events`** — Structured risk signals produced by the normaliser after each ingestion run. Severity and confidence on [0, 1].
- **`regulation_material_scope`** — Which regulations cover which materials.
- **`regulation_geography_scope`** — Which countries each regulation applies to.
- **`company_regulation_exposure`** — Per-company compliance status for each regulation.

### Relationship / junction tables

- **`risk_event_companies`** — Links risk events to affected companies with `relevance_score` (set by entity resolution, used as `relevance_multiplier` in scoring). **Writes are gated by `LINK_EVENTS_TO_COMPANIES` (default `False`)** as of Phase 3 — see [Ingestion pipeline](ingestion-pipeline.md).
- **`risk_event_materials`** — Links risk events to affected materials. Powers the market layer.
- **`risk_event_regulations`** — Links risk events to specific regulations they reference.
- **`risk_event_geographies`** — Country-level geography tags for risk events. Powers the market layer.
- **`risk_event_facilities`** — Facility-level relevance for events (added in migration 006).
- **`hs_code_material_mappings`** — Lookup: HS code prefix → material, used during trade flow ingestion.

### Trade data

- **`trade_flows`** — Normalised import/export observations from Census + Comtrade data. `material_id` is set via HS code lookup.
- **`commodity_prices`** — Historical spot prices per material, date, and source. Drives the reframed financial-pressure pillar in the market layer.

### Scoring (append-only)

- **`company_scores`** — Six-pillar risk scores per company per `as_of_date` (v3.0). Never overwritten; new row appended per scoring run. Contains full `rationale_json` (`SupplierScoreRationale` schema).
- **`chemistry_risk_scores`** — Per-chemistry composite scores from `score_chemistry`. Append-only.
- **`material_geography_risk_scores`** — **Market-level scores** per `(material_id, geography_code, as_of_date)` from `score_material_geography`. Powers the public intelligence hub. Migration `011_material_geography_risk_scores`.
- **`material_scores`** / **`geography_scores`** — Aggregate roll-ups (legacy; the market layer is the canonical company-agnostic view going forward).

### Reporting and intelligence

- **`report_templates`** — Configures customer-facing report structure and focus entities. Can be tenant-scoped or platform-wide.
- **`report_template_focus_entities`** — Which companies/materials/geographies a template targets.
- **`report_runs`** — Each report generation attempt; links to template and tenant.
- **`report_insights`** — Individual findings written into a customer report with optional entity links.
- **`insight_posts`** — **Public intelligence-hub content** (manually authored). Migration `012_insight_posts`. Distinct from `report_insights`: `insight_posts` are the source of articles on `mineralriskanalytics.com`; `report_insights` are per-customer report findings. Includes `slug`, `title`, `content_type`, `pillar`, `materials` / `geographies` (`ARRAY(String)`), Markdown `body`, and publishing status.
- **`analyst_notes`** — Free-text human annotations on any entity via generic `(entity_type, entity_id)`. Phase 2 widened the `entity_type` enum to cover materials, HS-mapping rows, regulations, risk events, facilities, and chemistries.

Full DDL is in **`alembic/versions/`** (current head: `012_insight_posts`) and ORM models in **`app/models/`** (with `app/models/scoring.py` and `app/models/intelligence.py` housing the new layers).

---

## Internal API (`/api/v1`)

Mounted from **`app/main.py`**. Authentication uses Clerk JWT verification (`app/api/deps.py`); admin endpoints additionally require an `admin`/`owner` role.

### Phase 1 — operations

| Method & path | Description |
|---------------|-------------|
| `GET /health` | Liveness check |
| `GET /sources` | List all configured data sources |
| `POST /sources/{id}/ingest` | Body optional `{"extra": {}}` merged into `config_json`; runs **`IngestionPipeline`**. Phase 3: no longer auto-rescores companies. |
| `GET /ingestion-runs` | Recent ingestion runs (`limit` query param) |
| `GET /risk-events` | Recent risk events (filterable) |
| `GET /regulations` | Regulations browser |
| `GET /companies` | Companies list |
| `POST /companies/{id}/rescore` | Manually trigger company scoring; returns `202` with new `company_score_id` |
| `GET /trade-flows` | Recent trade flow records |
| `GET /dashboard/*` | KPI roll-ups powering the admin dashboard |

### Phase 2 — reference data + flag-issue

Per-entity browsers and generic notes endpoints under `/materials`, `/facilities`, `/chemistries`, `/regulations`, `/risk-events`. Each entity supports the shared analyst-note pattern via:

```
POST /materials/{id}/notes
GET  /materials/{id}/notes
POST /materials/{material_id}/hs-mappings/{mapping_id}/notes
GET  /materials/{material_id}/hs-mappings/{mapping_id}/notes
POST /regulations/{id}/notes
GET  /regulations/{id}/notes
POST /risk-events/{id}/notes
GET  /risk-events/{id}/notes
POST /facilities/{id}/notes
GET  /facilities/{id}/notes
POST /chemistries/{id}/notes
GET  /chemistries/{id}/notes
```

The body is `{ "note_type": "data_error" | "missing_data" | "outdated" | "other", "note_text": str }`. Server hardcodes `entity_type` and `entity_id` from the URL.

### Foundation Phase 2 — Market intelligence scores

Mounted from **`app/api/routes/market_scores.py`**.

| Method & path | Description |
|---------------|-------------|
| `GET /materials/{material_id}/market-scores` | Latest score per geography for one material |
| `GET /materials/{material_id}/market-scores/{geo}` | Full rationale (one pair) — returns `404` until `POST /market/rescore` populates the table |
| `GET /market/scores` | Paginated browse across all pairs. Filters: `material_id`, `geography_code`, `min_overall`, `page`, `limit` |
| `POST /market/rescore` | Synchronously rescore every active material × geography pair. Returns `{ scored: int, as_of_date, run_id }`. Each pair commits independently inside `score_all_active_materials()`. |

The intelligence-hub article surface (`insight_posts`) is exposed via a separate router on the public Next.js frontend; the engine simply persists the rows.

### Inngest serve route

| Method & path | Description |
|---------------|-------------|
| `GET/POST /api/inngest` | Inngest discovery + invocation endpoint. Registers two cron functions (`rescore-all-chemistries`, `rescore-market-scores`). See [Scoring → Scheduled rescores](scoring.md#scheduled-rescores-inngest). |

> **Deprecated:** `GET /suppliers` and `POST /suppliers/{id}/rescore` are aliases that re-export the companies router. Use `/companies` routes directly.

Pydantic response models live in **`app/schemas/`**.

---

## CLI parity

Every operational pipeline trigger is also available via Typer (`bdi-ingest <command>` or `python -m app.cli <command>`).

### Reference data + seeds

| Command | Purpose |
|---------|---------|
| `seed` | Load reference countries, materials, suppliers, aliases, and sources |
| `seed-companies` | Seed canonical company list |
| `seed-materials` | Seed REEs and other materials absent from USGS MCS |
| `seed-hs-mappings` | Seed HS-code → material mappings |
| `seed-facilities` | Seed facility roster |
| `seed-supply-relationships` | Seed `company_supply_relationships` |
| `seed-material-exposures` | Seed `company_material_exposures` |
| `seed-regulations` | Seed US regulations |
| `ingest-usgs <file>` | Load USGS MCS CSV |
| `review-seeds` | Stale-data review hooks |

### Live ingestion

| Command | Purpose |
|---------|---------|
| `ingest <federal-register\|census-trade\|sec-edgar\|news>` | Pipeline-based source by alias |
| `ingest-federal-register` | US Federal Register (CLI variant of pipeline trigger) |
| `ingest-eurlex` | EU regulations (Phase 1 — CLI source) |
| `ingest-gta` | Global Trade Alert harmful trade interventions → `RiskEvent` rows |
| `ingest-comtrade` | UN Comtrade trade flows |
| `ingest-opensanctions` | Sanctions / PEP entities |
| `ingest-worldbank` | World Bank Pink Sheet commodity prices |
| `ingest-sec-edgar` | SEC EDGAR filings |
| `ingest-gleif` | GLEIF LEI roster (entity normalisation) |
| `ingest-vpic` | NHTSA vPIC vehicle decoder |
| `build-trade-signals` | Materialise trade-volatility risk events from `trade_flows` |

### Scoring

| Command | Purpose |
|---------|---------|
| `rescore-chemistry [--slug <slug>] [--as-of YYYY-MM-DD]` | Single or all chemistries → `chemistry_risk_scores`. Also Mondays 02:00 UTC via Inngest. |
| `rescore-market [--material-id <n>] [--geographies CN,CL,...] [--as-of YYYY-MM-DD]` | Market layer → `material_geography_risk_scores`. Also Mondays 03:00 UTC via Inngest. |
| `rescore-all` | Rescore every company. Phase 3: this is the **only** way company scores are refreshed (ingestion no longer triggers it). |
| `show-scores` | Print latest company scores |

Hatch script shortcuts for `rescore-chemistry`, `rescore-market`, and `rescore-all` are registered in `pyproject.toml`.

---

## Related reading

- [Database architecture](database_architecture.md) — column-level detail for every table
- [Ingestion pipeline](ingestion-pipeline.md) — `LINK_EVENTS_TO_COMPANIES` gate and CLI flow
- [Scoring](scoring.md) — six-pillar formulas, market layer, scheduled rescores
- [Data sources](data-sources.md) — what each ingestion command consumes and writes
- [Overview](overview.md)
