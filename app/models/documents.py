"""Stored documents and text chunks (RAG-ready)."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any, Optional

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.source import Source
    from app.models.supply import TradeFlow
    from app.models.regulatory import Regulation, RiskEvent


class SourceDocument(Base):
    __tablename__ = "source_documents"
    __table_args__ = (
        UniqueConstraint("source_id", "external_id", name="uq_source_external_doc"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    source_id: Mapped[int] = mapped_column(
        ForeignKey("sources.id", ondelete="CASCADE"), nullable=False, index=True
    )
    external_id: Mapped[str] = mapped_column(String(512), nullable=False)
    title: Mapped[Optional[str]] = mapped_column(String(1024))
    url: Mapped[Optional[str]] = mapped_column(Text)
    published_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    document_type: Mapped[str] = mapped_column(
        String(64), nullable=False, default="unknown"
    )
    mime_type: Mapped[Optional[str]] = mapped_column(String(128))
    raw_storage_path: Mapped[Optional[str]] = mapped_column(String(1024))
    # R2 object path: e.g. raw/federal_register/2024-01-15/{checksum}.json
    raw_text: Mapped[Optional[str]] = mapped_column(Text)
    checksum: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    # SHA-256 of raw content — used for deduplication at ingestion time
    metadata_json: Mapped[Optional[Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    source: Mapped["Source"] = relationship(back_populates="documents")
    chunks: Mapped[list["DocumentChunk"]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )
    regulations: Mapped[list["Regulation"]] = relationship(back_populates="source_document")
    risk_events: Mapped[list["RiskEvent"]] = relationship(back_populates="source_document")
    trade_flows: Mapped[list["TradeFlow"]] = relationship(back_populates="source_document")


class DocumentChunk(Base):
    """
    A text chunk from a source document, ready for embedding and semantic search.
    The embedding column (vector(1536)) is added via raw SQL in the migration —
    SQLAlchemy does not have a native Vector type; pgvector's sqlalchemy-pgvector
    package is a future dependency if ORM-level vector queries are needed.

    embedding_id may store the OpenAI batch ID or a reference to an external
    vector store if we migrate to Pinecone/Weaviate later.
    """

    __tablename__ = "document_chunks"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    source_document_id: Mapped[int] = mapped_column(
        ForeignKey("source_documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    chunk_index: Mapped[int] = mapped_column(nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    embedding_id: Mapped[Optional[str]] = mapped_column(String(128))
    metadata_json: Mapped[Optional[Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    document: Mapped["SourceDocument"] = relationship(back_populates="chunks")
