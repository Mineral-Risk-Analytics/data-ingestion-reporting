Create an embedding service for the battery-data-intelligence-engine FastAPI project.

## Context

The project uses pgvector (`vector(1536)` column on `document_chunks`) for semantic search over ingested supply chain documents. The embedding model and vector dimension are not yet implemented — only the schema exists. The goal is a thin, swappable abstraction so the provider (currently OpenAI) can be changed by updating config, not code.

## What to build

### 1. `app/core/config.py` — add embedding settings

Add the following fields to the existing `Settings` class (or create the file if it does not exist):

```python
EMBEDDING_MODEL: str = "text-embedding-3-small"
EMBEDDING_DIMENSIONS: int = 1536
EMBEDDING_BATCH_SIZE: int = 64  # max texts per API call
```

These must be readable from environment variables via `pydantic-settings` (the project already uses pydantic). Add them to `.env.example` as:

```
EMBEDDING_MODEL=text-embedding-3-small
EMBEDDING_DIMENSIONS=1536
EMBEDDING_BATCH_SIZE=64
```

### 2. `app/services/ai/embeddings.py` — embedding service

Create this file. Requirements:

- A single public function: `embed_texts(texts: list[str]) -> list[list[float]]`
- Reads `EMBEDDING_MODEL`, `EMBEDDING_DIMENSIONS`, and `EMBEDDING_BATCH_SIZE` from `app.core.config.settings`
- Batches input texts to respect `EMBEDDING_BATCH_SIZE`
- Currently implemented against the OpenAI client (`openai.OpenAI`), reading `OPENAI_API_KEY` from settings/env
- Raises a clear `EmbeddingError` (define it in this file) if the API call fails, with the original exception chained
- Returns a flat `list[list[float]]` in the same order as the input texts
- Includes a module-level docstring explaining that swapping providers means: (1) update `EMBEDDING_MODEL` and `EMBEDDING_DIMENSIONS` in env, (2) update the implementation block inside `embed_texts`, (3) run a new Alembic migration to change the vector column dimension, (4) re-run ingestion to regenerate all embeddings

Example signature:

```python
def embed_texts(texts: list[str]) -> list[list[float]]:
    """Return one embedding vector per input text.

    Batches requests to stay within EMBEDDING_BATCH_SIZE. Order is preserved.
    Raises EmbeddingError on API failure.
    """
```

Do not build a class — a module-level function is sufficient. Do not hardcode the model name or dimension anywhere in this file; always read from settings.

### 3. `app/services/ingestion/chunker.py` — document chunker

Create this file. Requirements:

- A function `chunk_text(text: str, chunk_size: int = 512, overlap: int = 64) -> list[str]`
- Splits on sentence boundaries where possible (use a simple approach — split on `. ` then regroup into windows, do not add an NLP dependency)
- `chunk_size` and `overlap` are in tokens approximated as `len(text.split())` (word count), not character count
- Returns a list of chunk strings with no empty strings

### 4. `app/services/ingestion/document_embedder.py` — pipeline step

Create this file. Requirements:

- A function `embed_and_store_chunks(db: Session, source_document_id: int, text: str) -> int`
- Calls `chunk_text` to split the document text
- Calls `embed_texts` to get vectors for all chunks in one batched call
- Writes one `DocumentChunk` ORM row per chunk, setting `chunk_index`, `text`, and `metadata_json` (include `{"model": settings.EMBEDDING_MODEL, "dimensions": settings.EMBEDDING_DIMENSIONS}`)
- Sets the `embedding` column using a raw SQL update (`UPDATE document_chunks SET embedding = :vec WHERE id = :id`) since SQLAlchemy has no native pgvector type in this project
- Returns the number of chunks written
- Is idempotent: delete existing chunks for `source_document_id` before inserting new ones

### 5. Tests

Create `tests/test_embedding_service.py`:

- Mock the OpenAI client — do not make real API calls
- Test that `embed_texts` batches correctly when input exceeds `EMBEDDING_BATCH_SIZE`
- Test that `embed_texts` raises `EmbeddingError` when the OpenAI call raises an exception
- Test that output length matches input length

## Constraints

- Do not add any new dependencies beyond `openai` (already in requirements) and standard library
- Do not use `langchain`, `llama-index`, or any vector store abstraction library
- Keep the OpenAI client instantiation lazy (inside the function or cached at module level with `functools.lru_cache`) so tests can patch it without import-time side effects
- All settings must flow through `app.core.config.settings`, never `os.environ` directly
