# Parsing and normalization

This layer turns **raw adapter items** (JSON-shaped dicts) into **structured fields** and **risk-oriented drafts**. It stays free of HTTP and, except for resolvers that query the DB, mostly free of side effects.

## Parsers (`app/services/ingestion/parsers/`)

Parsers are **pure-ish functions** or small helpers that return **dataclasses** (or lists of them). They are unit-tested without Postgres.


| Module                 | Input                             | Output                                     | Phase |
| ---------------------- | --------------------------------- | ------------------------------------------ | ----- |
| `regulation_parser.py` | Federal Register document dict    | `ParsedRegulation`                         | 1     |
| `tabular_parser.py`    | Census `[[headers], row, …]` JSON | `list[ParsedTradeRow]`                     | 1     |
| `filing_parser.py`     | SEC `submissions` JSON            | `list[ParsedFiling]`                       | 1     |
| `article_parser.py`    | News/stub article dict            | `ParsedArticle`                            | 1     |
| `pdf_report_parser.py` | PDF bytes (future)                | `PdfReportParseResult` (empty placeholder) | 2+    |


### Design notes

- **Federal Register**: maps `document_number`, `title`, `abstract`, dates, agencies, topics, URLs into `ParsedRegulation` and tucks extra API fields into `raw_metadata`.
- **Census**: column names must match what the adapter requested (`CTY_CODE`, `GEN_VAL_MO`, etc.). The parser is defensive about missing columns.
- **SEC**: reads `**filings.recent`** parallel arrays; builds archive URLs from CIK + accession + primary document. Narrative is **placeholder text** until full-text filing fetch exists.
- **Article**: normalizes `published_at` to timezone-aware datetime when possible; preserves `entities` and `event_classification` for risk metadata.

**Exports** are re-exported from `app/services/ingestion/parsers/__init__.py` for stable imports.

## Normalizers (`app/services/ingestion/normalizers/`)

Normalizers reconcile messy strings and codes with your **canonical model** (materials, suppliers, geography) and prepare `**RiskEventDraft`** instances.

### `GeographyResolver`

- Maps selected **Census `CTY_CODE`** values to **ISO2** (extend `_CTY_TO_ISO2` as you add partners).
- `**region_for_country`** groups ISO2 into coarse regions (`north_america`, `asia_pacific`, …) for macro-style tags.

### `MaterialResolver`

- Takes a SQLAlchemy `**Session**`: resolves **HS prefixes** to `**materials.id`** using `_HS_PREFIX_RULES` aligned with seed data (e.g. `8507` → “Lithium-ion battery cells”).
- `**resolve_by_canonical_name**` for exact lookups.

Tuning guide: add rows to `**materials**` via migrations/seed, then extend prefix rules or replace with DB-driven rules.

### `SupplierResolver`

- Matches `**supplier_aliases.alias**` (case-insensitive) then `**suppliers.canonical_name**`.
- Light **substring heuristic** as last resort (watch for false positives in production; replace with fuzzy/NER later).

### `event_normalizer.py`

Builds `**RiskEventDraft`** dataclass instances:


| Function                      | When used                           |
| ----------------------------- | ----------------------------------- |
| `build_regulatory_risk_event` | After Federal Register parse        |
| `build_sec_filing_event`      | Per SEC filing                      |
| `build_news_event`            | Per news article                    |
| `build_trade_risk_event`      | After Census batch (summary signal) |


Drafts carry **heuristic `severity_score` / `confidence_score`**, **category tags** (from `RiskCategory`), **geography JSON**, and **metadata** for analysts. They are **not** ML-based in Phase 1.

## Order of operations in the pipeline

For each source type, the pipeline generally:

1. **Parse** adapter `item` → typed structure.
2. **Upsert** `source_documents` (dedupe key: `source_id` + `external_id`).
3. **Insert/update** domain entity (`regulations`, `trade_flows`, …).
4. **Build draft** via normalizer → `**RiskEvent`** row(s).

Idempotency: re-running ingest **updates** existing documents by external id; **risk events** may duplicate unless you add dedupe on checksum or external ids later.

## Adding a new parser

1. Add module under `parsers/` returning a clear dataclass.
2. Export from `parsers/__init__.py`.
3. Call from a new `**_ingest_*_items`** method in `**IngestionPipeline**`.
4. Add **pytest** cases with fixture JSON (see `tests/test_parsers.py`).

## Adding a new resolver

1. Keep DB access isolated in the resolver class `**__init__(db: Session)`**.
2. Prefer **read-only** lookups in hot paths; bulk cache if necessary.
3. Unit-test with mocked `**session.execute`** / scalars (see `tests/test_normalizers.py`).

## Related reading

- [Ingestion pipeline](ingestion-pipeline.md)
- [Scoring](scoring.md) — uses similar concepts but separate from parse output
- [Overview](overview.md)

