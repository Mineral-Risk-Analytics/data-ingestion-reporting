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

### Source and provenance

- **`sources`** — Catalogue of feeds (`source_type`, `phase`, `is_active`, `config_json`).
- **`ingestion_runs`** — One run per pipeline execution; `stats_json`, `error_message`, timestamps.
- **`raw_api_payloads`** — Links to stored raw bytes (`response_body_path`, checksum, request metadata).
- **`source_documents`** — One logical document per `(source_id, external_id)`.
- **`document_chunks`** — Text chunks from source documents with 1536-dim pgvector embeddings for semantic search.

### Regulatory and risk

- **`regulations`** — Extracted regulatory records (Federal Register, EU directives). Unique on `regulation_key`.
- **`risk_events`** — Structured risk signals produced by the normaliser after each ingestion run. Severity and confidence on [0, 1].
- **`regulation_material_scope`** — Which regulations cover which materials.
- **`regulation_geography_scope`** — Which countries each regulation applies to.
- **`company_regulation_exposure`** — Per-company compliance status for each regulation.

### Relationship / junction tables

- **`risk_event_companies`** — Links risk events to affected companies with `relevance_score` (set by entity resolution, used as `relevance_multiplier` in scoring).
- **`risk_event_materials`** — Links risk events to affected materials.
- **`risk_event_regulations`** — Links risk events to specific regulations they reference.
- **`risk_event_geographies`** — Country-level geography tags for risk events.
- **`hs_code_material_mappings`** — Lookup: HS code prefix → material, used during trade flow ingestion.

### Trade data

- **`trade_flows`** — Normalised import/export observations from Census data. `material_id` is set via HS code lookup.
- **`commodity_prices`** — Historical spot prices per material, date, and source.

### Scoring (append-only)

- **`company_scores`** — Five-pillar risk scores per company per `as_of_date`. Never overwritten; new row appended per scoring run. Contains full `rationale_json` (`SupplierScoreRationale` schema).
- **`material_scores`** — Aggregate scores rolled up by material.
- **`geography_scores`** — Country-level risk roll-ups.

### Reporting and notes

- **`report_templates`** — Configures report structure and focus entities. Can be tenant-scoped or platform-wide.
- **`report_template_focus_entities`** — Which companies/materials/geographies a template targets.
- **`report_runs`** — Each report generation attempt; links to template and tenant.
- **`report_insights`** — Individual findings written into a report with optional entity links.
- **`analyst_notes`** — Free-text human annotations on any entity via generic `(entity_type, entity_id)`.

Full DDL is in **`alembic/versions/001_baseline.py`** and ORM models in **`app/models/`**.

> **Note:** The `001_baseline.py` migration reflects the current target schema (companies, company_scores, etc.). Some ORM models in `app/models/` still use legacy names from earlier development (e.g. `Supplier`, `SupplierScore`). These are being updated to match the migration. The migration is the source of truth for the database schema.

---

## Internal API (`/api/v1`)

Mounted from **`app/main.py`**. Authentication is handled by Clerk at the network edge; no in-process auth middleware in Phase 1.

| Method & path | Description |
|---------------|-------------|
| `GET /health` | Liveness check |
| `GET /sources` | List all configured data sources |
| `POST /sources/{id}/ingest` | Body optional `{"extra": {}}` merged into `config_json`; runs **`IngestionPipeline`** then scores touched companies |
| `GET /ingestion-runs` | Recent ingestion runs (`limit` query param) |
| `GET /risk-events` | Recent risk events |
| `GET /regulations` | Recent regulations |
| `GET /companies` | Companies list |
| `POST /companies/{id}/rescore` | Manually trigger scoring for one company; returns `202` with new `company_score_id` |
| `GET /trade-flows` | Recent trade flow records |

> **Deprecated:** `GET /suppliers` and `POST /suppliers/{id}/rescore` are aliases that re-export the companies router. Use `/companies` routes directly.

Pydantic response models live in **`app/schemas/`**.

---

## CLI parity

- **`python -m app.cli seed`** — Idempotent seed: reference data + sources rows.
- **`python -m app.cli ingest <federal-register|census-trade|sec-edgar|news>`** — Same pipeline as the HTTP trigger; used for dev and cron.

---

## Related reading

- [Database architecture](database_architecture.md) — column-level detail for every table
- [Ingestion pipeline](ingestion-pipeline.md)
- [Scoring](scoring.md)
- [Overview](overview.md)
