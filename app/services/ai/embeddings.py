"""
Embedding service — thin wrapper around a text-embedding provider.

Current provider: OpenAI ``text-embedding-3-small`` (1536 dimensions).

To swap providers:
  1. Update ``EMBEDDING_MODEL`` and ``EMBEDDING_DIMENSIONS`` in the environment
     (or .env) to match the new provider's model name and output dimension.
  2. Update the implementation block inside ``embed_texts`` to call the new
     provider's API (everything above and below that block stays the same).
  3. Run a new Alembic migration to change the ``document_chunks.embedding``
     column dimension: ``ALTER TABLE document_chunks ALTER COLUMN embedding
     TYPE vector(NEW_DIM);``
  4. Re-run ingestion (or a backfill job) to regenerate all embeddings with
     the new model — existing vectors are incompatible with a different dimension.
"""

from __future__ import annotations

import functools
import logging
from typing import TYPE_CHECKING

import structlog

from app.core.config import get_settings

if TYPE_CHECKING:
    import openai as _openai_types

log = structlog.get_logger(__name__)


class EmbeddingError(Exception):
    """Raised when an embedding API call fails.

    The original provider exception is always chained (``raise ... from exc``).
    Callers should catch this instead of provider-specific exceptions so that
    swapping providers does not require changes outside this module.
    """


@functools.lru_cache(maxsize=1)
def _get_client() -> "_openai_types.OpenAI":
    """Return a cached OpenAI client.

    Instantiated lazily on first call so tests can patch ``openai.OpenAI``
    (or this function directly) without import-time side effects.
    """
    try:
        import openai  # noqa: PLC0415
    except ImportError as exc:
        raise ImportError(
            "The 'openai' package is required for embedding. "
            "Add it to pyproject.toml and run 'uv sync'."
        ) from exc

    settings = get_settings()
    return openai.OpenAI(api_key=settings.openai_api_key)


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Return one embedding vector per input text.

    Batches requests to stay within ``EMBEDDING_BATCH_SIZE``. Order is
    preserved across batch boundaries.

    Args:
        texts: List of strings to embed. Empty strings are accepted but will
            return zero-length content vectors from the API; callers should
            filter them upstream.

    Returns:
        A flat ``list[list[float]]`` of the same length as ``texts``, where
        each inner list has ``EMBEDDING_DIMENSIONS`` elements.

    Raises:
        EmbeddingError: If any API call fails. The original exception is
            chained so ``raise ... from exc`` gives the full traceback.
    """
    if not texts:
        return []

    settings = get_settings()
    model = settings.embedding_model
    dimensions = settings.embedding_dimensions
    batch_size = settings.embedding_batch_size

    results: list[list[float]] = []

    for batch_start in range(0, len(texts), batch_size):
        batch = texts[batch_start : batch_start + batch_size]

        log.debug(
            "embeddings.batch",
            model=model,
            batch_start=batch_start,
            batch_size=len(batch),
            total=len(texts),
        )

        try:
            # --- Provider implementation block --------------------------------
            # Replace everything in this block when switching providers.
            # Contract: produce a list of float vectors, one per input text,
            # in the same order as ``batch``.
            client = _get_client()
            response = client.embeddings.create(
                model=model,
                input=batch,
                dimensions=dimensions,
            )
            batch_vectors: list[list[float]] = [
                item.embedding for item in sorted(response.data, key=lambda x: x.index)
            ]
            # --- End provider block ------------------------------------------

        except Exception as exc:
            raise EmbeddingError(
                f"Embedding API call failed for batch starting at index "
                f"{batch_start} (model={model!r}): {exc}"
            ) from exc

        results.extend(batch_vectors)

    log.debug("embeddings.done", model=model, total=len(results))
    return results
