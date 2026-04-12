"""
Pipeline step: chunk a source document's text, generate embeddings, and
persist ``DocumentChunk`` rows with their vector embeddings.

Idempotency guarantee: existing chunks for a ``source_document_id`` are
deleted before new ones are inserted. Re-running this function on the same
document is safe and produces a clean replacement.

The ``embedding`` column is a pgvector ``vector(1536)`` type not represented
in the SQLAlchemy ORM. Vectors are written via a raw SQL UPDATE immediately
after each chunk row is flushed, using a ``CAST(:vec AS vector)`` expression.
pgvector accepts the Python ``str([...])`` format ``'[0.1, 0.2, ...]'``
as its text input.
"""

from __future__ import annotations

import structlog
from sqlalchemy import delete, text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.documents import DocumentChunk
from app.services.ai.embeddings import embed_texts
from app.services.ingestion.chunker import chunk_text

log = structlog.get_logger(__name__)


def embed_and_store_chunks(
    db: Session,
    source_document_id: int,
    text_content: str,
    chunk_size: int = 512,
    overlap: int = 64,
) -> int:
    """Chunk ``text_content``, embed all chunks, and persist to ``document_chunks``.

    Args:
        db:                   SQLAlchemy session. The caller owns the
                              transaction and must call ``db.commit()``.
        source_document_id:   PK of the parent ``source_documents`` row.
        text_content:         Plain text to embed. If empty, the function
                              deletes any existing chunks and returns 0.
        chunk_size:           Target maximum words per chunk (passed to
                              ``chunk_text``).
        overlap:              Overlap words between consecutive chunks.

    Returns:
        Number of ``DocumentChunk`` rows written.

    Side effects:
        - Deletes all existing ``DocumentChunk`` rows for this document first
          (idempotent replacement).
        - Inserts new ``DocumentChunk`` rows, then issues one raw SQL UPDATE
          per row to set the ``embedding`` vector column.
        - Does NOT call ``db.commit()`` — caller is responsible.
    """
    settings = get_settings()

    # --- Idempotent: remove stale chunks ------------------------------------
    db.execute(
        delete(DocumentChunk).where(
            DocumentChunk.source_document_id == source_document_id
        )
    )
    db.flush()

    if not text_content or not text_content.strip():
        log.debug(
            "document_embedder.skip_empty",
            source_document_id=source_document_id,
        )
        return 0

    # --- Chunk the text -----------------------------------------------------
    chunks = chunk_text(text_content, chunk_size=chunk_size, overlap=overlap)
    if not chunks:
        return 0

    log.debug(
        "document_embedder.chunked",
        source_document_id=source_document_id,
        chunk_count=len(chunks),
    )

    # --- Embed all chunks in one batched call --------------------------------
    vectors = embed_texts(chunks)  # may raise EmbeddingError — caller handles

    if len(vectors) != len(chunks):
        raise RuntimeError(
            f"embed_texts returned {len(vectors)} vectors for {len(chunks)} chunks "
            f"(source_document_id={source_document_id})"
        )

    # --- Persist ORM rows then set vector column via raw SQL -----------------
    chunk_metadata = {
        "model": settings.embedding_model,
        "dimensions": settings.embedding_dimensions,
    }

    for idx, (chunk_text_str, vector) in enumerate(zip(chunks, vectors)):
        chunk_row = DocumentChunk(
            source_document_id=source_document_id,
            chunk_index=idx,
            text=chunk_text_str,
            metadata_json=chunk_metadata,
        )
        db.add(chunk_row)
        db.flush()  # populate chunk_row.id before the vector UPDATE

        # pgvector accepts '[x, y, z, ...]' text format.
        # CAST is used instead of relying on implicit coercion.
        db.execute(
            text(
                "UPDATE document_chunks "
                "SET embedding = CAST(:vec AS vector) "
                "WHERE id = :chunk_id"
            ),
            {"vec": str(vector), "chunk_id": chunk_row.id},
        )

    log.info(
        "document_embedder.done",
        source_document_id=source_document_id,
        chunks_written=len(chunks),
        model=settings.embedding_model,
    )
    return len(chunks)
