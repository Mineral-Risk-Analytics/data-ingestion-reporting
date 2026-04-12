# Parsing and normalization

> **Last updated: April 2026**

This layer turns **raw adapter items** (JSON-shaped dicts) into **structured fields** and **risk-oriented drafts**. It stays free of HTTP and, except for resolvers that query the DB, mostly free of side effects.

## Parsers (`app/services/ingestion/parsers/`)

Parsers are **pure-ish functions** or small helpers that return **dataclasses** (or lists of them). They are unit-tested without Postgres.

| Module | Input | Output | Phase |
|--------|-------|--------|-------|
| `regulation_parser.py` | Federal Register document dict | `ParsedRegulation` | 1 |
| `tabular_parser.py` | Census `[[headers], row, …]` JSON | `list[ParsedTradeRow]` | 1 |
| `filing_parser.py` | SEC `submissions` JSON | `list[ParsedFiling]` | 1 |
| `article_parser.py` | News/stub article dict | `ParsedArticle` | 1 |
| `pdf_report_parser.py` | PDF bytes (future) | `PdfReportParseResult` (placeholder) | 2+ |

### Design notes

- **Federal Register**: maps `document_number`, `title`, `abstract`, dates, agencies, topics, URLs into `ParsedRegulation`; tucks extra API fields into `raw_metadata`.
- **Census**: column names must match what the adapter requested (`CTY_CODE`, `GEN_VAL_MO`, etc.). The parser is defensive about missing columns.
- **SEC**: reads `filings.recent` parallel arrays; builds archive URLs from CIK + accession + primary document.
- **Article**: normalizes `published_at` to timezone-aware datetime; preserves `entities` and `event_classification` for risk metadata.

Exports are re-exported from `app/services/ingestion/parsers/__init__.py` for stable imports.

## Normalizers (`app/services/ingestion/normalizers/`)

Normalizers reconcile messy strings and codes with your canonical model and produce **`RiskEventDraft`** instances.

### `GeographyResolver`

- Maps Census `CTY_CODE` values to ISO2 (extend `_CTY_TO_ISO2` as you add partners).
- `region_for_country` groups ISO2 into coarse regions (`north_america`, `asia_pacific`, …) for tag breadth.

### `MaterialResolver`

- Takes a SQLAlchemy `Session`; resolves **HS prefixes** to `materials.id` using `_HS_PREFIX_RULES` aligned with seed data.
- `resolve_by_canonical_name` for exact lookups.
- Tuning: add rows to `materials` via migrations/seed, then extend prefix rules or replace with DB-driven rules via `hs_code_material_mappings`.

### `SupplierResolver` / Entity resolution

The legacy `SupplierResolver` (alias-based substring matching) is used during early-stage normalisation to locate candidate companies from raw text. **Definitive entity resolution** — which companies are relevant to a completed `RiskEvent` — happens in a dedicated post-insert step:

- `app/services/ingestion/entity_resolution.py` → `resolve_suppliers_for_event`
- Applies four priority-ordered rules: named match, geography match, material/HS match, broad category match.
- Writes `risk_event_companies` junction rows with `relevance_score` and `match_reason`.
- See [Ingestion pipeline](ingestion-pipeline.md) for how this integrates with `_add_risk_event`.

### `event_normalizer.py`

Builds `RiskEventDraft` dataclass instances:

| Function | When used |
|----------|-----------|
| `build_regulatory_risk_event` | After Federal Register parse |
| `build_sec_filing_event` | Per SEC filing |
| `build_news_event` | Per news article |
| `build_trade_risk_event` | After Census batch (summary signal) |

Drafts carry **heuristic `severity_score` / `confidence_score`** on [0, 1.0] (not 0-100), **`risk_categories_json`** tags from `RiskCategory` (see taxonomy below), **`geography_json`**, and **`metadata_json`** for analysts. They are rule-based in Phase 1, not ML-based.

## RiskCategory taxonomy (v2)

`RiskCategory` lives in `app/constants.py`. All five values must be used in event tags — the old v1 values are retired:

| v2 enum value | String tag | Old v1 value |
|---------------|-----------|--------------|
| `MATERIAL_CONCENTRATION` | `"material_concentration"` | `MATERIAL_SUPPLY` |
| `GEOPOLITICAL_TRADE` | `"geopolitical_trade"` | `MARKET_DEMAND`, `INFRASTRUCTURE_ECOSYSTEM` |
| `REGULATORY_COMPLIANCE` | `"regulatory_compliance"` | `REGULATORY_POLICY` |
| `OPERATIONAL` | `"operational"` | `SUPPLIER_OPERATIONAL` |
| `FINANCIAL_PRESSURE` | `"financial_pressure"` | — (new in v2) |

These string values are stored in `risk_events.risk_categories_json` as a JSON array (e.g. `["geopolitical_trade", "material_concentration"]`) and are used by the evidence query layer to select events for each scoring pillar.

## Order of operations in the pipeline

For each source type, the pipeline generally:

1. **Parse** adapter `item` → typed structure.
2. **Upsert** `source_documents` (dedupe key: `source_id` + `external_id`).
3. **Insert/update** domain entity (`regulations`, `trade_flows`, …).
4. **Build draft** via normalizer → `RiskEvent` row(s).
5. **Entity resolve** — `_add_risk_event` calls `entity_resolution` to write `risk_event_companies`.

Idempotency: re-running ingest **updates** existing documents by external id; `risk_events` may duplicate unless you add dedupe on `content_hash`.

## Adding a new parser

1. Add module under `parsers/` returning a clear dataclass.
2. Export from `parsers/__init__.py`.
3. Call from a new `_ingest_*_items` method in `IngestionPipeline`.
4. Add pytest cases with fixture JSON (see `tests/test_parsers.py`).

## Adding a new resolver

1. Keep DB access isolated in the resolver class `__init__(db: Session)`.
2. Prefer read-only lookups in hot paths; bulk cache if necessary.
3. Unit-test with mocked `session.execute` / scalars (see `tests/test_normalizers.py`).

## Related reading

- [Ingestion pipeline](ingestion-pipeline.md)
- [Scoring](scoring.md) — RiskCategory tags drive evidence selection
- [Overview](overview.md)
