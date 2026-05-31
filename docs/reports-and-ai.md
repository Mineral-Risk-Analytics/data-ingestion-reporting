# Reports and AI services

> **Last updated: April 2026**

## Embedding service (`app/services/ai/embeddings.py`) — **implemented**

The embedding service is live and provides a swappable abstraction over text embedding providers.

**Public interface:**

```python
from app.services.ai.embeddings import embed_texts, EmbeddingError

vectors: list[list[float]] = embed_texts(["text 1", "text 2", ...])
```

- Current provider: OpenAI `text-embedding-3-small` (configured via `EMBEDDING_MODEL` in settings).
- Output dimension: 1536 (configured via `EMBEDDING_DIMENSIONS`).
- Batches automatically to respect `EMBEDDING_BATCH_SIZE` (default 64).
- Raises `EmbeddingError` (with original exception chained) on API failure.
- OpenAI client is lazily instantiated (`functools.lru_cache`) so tests can patch it without import-time side effects.

**Swapping providers:**
1. Update `EMBEDDING_MODEL` and `EMBEDDING_DIMENSIONS` in `.env`.
2. Update the implementation block inside `embed_texts` in `embeddings.py`.
3. Run a new Alembic migration to change the `vector(N)` column dimension.
4. Re-run ingestion to regenerate all embeddings.

## Document chunker and embedder (`app/services/ingestion/`)

Two pipeline steps handle chunking and storage:

### `chunker.py`

```python
from app.services.ingestion.chunker import chunk_text

chunks: list[str] = chunk_text(text, chunk_size=512, overlap=64)
```

- Splits on sentence boundaries (`". "`) then regroups by approximate word count.
- `chunk_size` and `overlap` are in words (not characters).
- Returns no empty strings.

### `document_embedder.py`

```python
from app.services.ingestion.document_embedder import embed_and_store_chunks

n_written = embed_and_store_chunks(db, source_document_id=doc.id, text=doc.raw_text)
```

- **Idempotent**: deletes existing chunks for `source_document_id` before inserting new ones.
- Writes one `DocumentChunk` row per chunk with `chunk_index`, `text`, and `metadata_json` (includes `model` and `dimensions`).
- Stores vectors using raw SQL `UPDATE ... SET embedding = CAST(:vec AS vector)` because SQLAlchemy has no native pgvector type.
- Returns count of chunks written.

Embeddings are stored in `document_chunks.embedding` as `vector(1536)` with an IVFFlat index for approximate nearest-neighbour search.

---

## AI service stubs (`app/services/ai/`)

These modules use `typing.Protocol` so OpenAI, Anthropic, or an internal model server can replace stub logic without changing call sites.

### `summarize_document.py`

- `DocumentSummarizer` protocol: `summarize(text, metadata=…) -> str`
- `StubDocumentSummarizer` — extractive first-sentence summary (no network call).
- **Future:** `OpenAiSummarizer` implementing the same protocol.

### `classify_event.py`

- `EventClassifier` protocol: `classify(title, summary) -> list[str]` (RiskCategory tags).
- `StubEventClassifier` — keyword rules mapped to `RiskCategory` values.
- **Future:** constrained JSON schema from an LLM; validate against `RiskCategory`.

### `cluster_duplicates.py`

- `DuplicateClusterer` protocol: `cluster_key(title, url) -> str`
- `StubDuplicateClusterer` — URL-first; else normalized title token signature.
- **Future:** embedding + clustering (HDBSCAN, online dedupe service) using the `document_chunks.embedding` column.

---

## Reports (`app/services/reports/`)

| Module | Intended audience | Phase 1 status |
|--------|------------------|--------------------|
| `build_oem_report.py` | OEM-facing brief | `NotImplementedError` |
| `build_supplier_report.py` | Company deep-dive | `NotImplementedError` |
| `build_investor_report.py` | Investor / macro | `NotImplementedError` |

### Data model hooks

Persistence is already modelled for a full reports workflow:

- **`report_templates`** — configures report structure, scope, and focus entities; can be tenant-scoped or platform-wide.
- **`report_template_focus_entities`** — companies, materials, or geographies a template targets.
- **`report_runs`** — one row per generation attempt (`queued` → `running` → `completed` / `failed`).
- **`report_insights`** — ordered content blocks (`insight_type`, `title`, `body`), with optional FK to company, material, regulation, or geography.

### `report_insights` vs `insight_posts`

These two surfaces are deliberately separate and easy to confuse:

| | `report_insights` | `insight_posts` (migration 012) |
|---|---|---|
| **Audience** | A specific paying customer | Public — `mineralriskanalytics.com` intelligence hub |
| **Authoring** | Auto-generated inside a `report_runs` row | Manually authored by the partner |
| **FK shape** | Tied to a `report_run_id` + optional company/material/regulation FKs | No FKs; tagged via `materials TEXT[]` / `geographies TEXT[]` arrays |
| **Lifecycle** | Created on report generation; immutable | `draft` → `published` → `archived`, with `published_at` timestamp |
| **Distribution** | Rendered into a customer PDF/HTML | Served to the public Next.js intelligence hub |

When the LLM narrative work in this section is wired up, it generates `report_insights` rows. The intelligence-hub content surface stays manual and is not driven by any of the AI stubs below.

Planned generation flow:

1. Create `report_runs` row.
2. Query `company_scores`, `risk_events`, `regulations`, `trade_flows`, `analyst_notes`.
3. (Optional) Use `embed_texts` + pgvector similarity to surface relevant `document_chunks`.
4. Generate `report_insights` rows via LLM narrative call.
5. Render to PDF/HTML (outside current scope).

### Integration ideas (not wired)

- After ingestion, async tasks could call `embed_and_store_chunks` on new `source_documents.raw_text` — embedding storage in `document_chunks` is ready.
- `classify_event` could refine `risk_events.risk_categories_json` in a post-processing pass.
- `cluster_duplicates` could collapse duplicate risk events by populating a deduplication table.

---

## Related reading

- [Overview](overview.md)
- [Data model & internal API](data-model-and-api.md) — `report_runs`, `report_insights`, `insight_posts`, `document_chunks`
- [Database architecture](database_architecture.md) — `insight_posts` schema (migration 012)
- [Scoring](scoring.md) — inputs to narrative insight generation; market-layer scores feeding the public hub
