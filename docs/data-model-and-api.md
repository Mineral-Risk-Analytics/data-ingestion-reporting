# Data model and internal API

This document summarizes **primary tables** touched by ingestion and the **HTTP surface** used to operate the system without a frontend.

## Core entity groups

### Source and provenance

- **`sources`** — Catalog of feeds (`source_type`, `phase`, `is_active`, `config_json`).
- **`ingestion_runs`** — One run per pipeline execution; `stats_json`, `error_message`, timestamps.
- **`raw_api_payloads`** — Links to stored raw bytes (`response_body_path`, checksum, request metadata).
- **`source_documents`** — One logical document per `(source_id, external_id)`; holds title, URL, `raw_text`, `metadata_json`.

### Supply chain

- **`countries`**, **`organizations`** — Reference / future enrichment.
- **`suppliers`**, **`supplier_aliases`** — Canonical names and resolution.
- **`materials`** — Canonical materials (HS-driven mapping in Phase 1).
- **`trade_flows`** — Normalized trade observations (Census-driven).
- **`supplier_material_exposure`**, **`supplier_scores`** — For analyst + scoring workflows.

### Regulatory and risk

- **`regulations`** — Extracted from regulatory documents (Federal Register in Phase 1).
- **`risk_events`** — Signals for reporting (all Phase 1 sources contribute).

### Reporting and notes

- **`report_runs`**, **`report_insights`** — Future assembled reports.
- **`analyst_notes`** — Human annotations on arbitrary entity types.

### Supporting

- **`document_chunks`** — RAG / embedding preparation.
- **`entity_links`** — Resolved links between entities.

Full DDL is in **`alembic/versions/001_initial_schema.py`** and ORM in **`app/models/`**.

## Internal API (`/api/v1`)

Mounted from **`app/main.py`**. No authentication in Phase 1 — restrict at network edge.

| Method & path | Description |
|---------------|-------------|
| `GET /health` | Liveness |
| `GET /sources` | List **`sources`** |
| `POST /sources/{id}/ingest` | Body optional `{"extra": {}}` merged into `config_json`; runs **`IngestionPipeline`** |
| `GET /ingestion-runs` | Recent runs (`limit` query) |
| `GET /risk-events` | Recent **`risk_events`** |
| `GET /regulations` | Recent **`regulations`** |
| `GET /suppliers` | **`suppliers`** list |
| `GET /trade-flows` | Recent **`trade_flows`** |

Pydantic response models live in **`app/schemas/`**.

## CLI parity

- **`python -m app.cli seed`** — Idempotent reference data + sources.
- **`python -m app.cli ingest <federal-register|census-trade|sec-edgar|news>`** — Same pipeline as HTTP for dev and cron.

## Field mapping reference

Source-specific API → column mappings are documented in the root **`README.md`** (Phase 1 field mapping section).

## Related reading

- [Ingestion pipeline](ingestion-pipeline.md)
- [Overview](overview.md)
