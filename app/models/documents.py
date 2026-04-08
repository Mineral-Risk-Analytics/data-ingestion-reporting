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
    __table_args__ = (UniqueConstraint("source_id", "external_id", name="uq_source_external_doc"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"), index=True)
    external_id: Mapped[str] = mapped_column(String(512), nullable=False)
    title: Mapped[Optional[str]] = mapped_column(String(1024))
    url: Mapped[Optional[str]] = mapped_column(Text)
    published_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    document_type: Mapped[str] = mapped_column(String(64), nullable=False, default="unknown")
    mime_type: Mapped[Optional[str]] = mapped_column(String(128))
    raw_storage_path: Mapped[Optional[str]] = mapped_column(String(1024))
    raw_text: Mapped[Optional[str]] = mapped_column(Text)
    metadata_json: Mapped[Optional[Any]] = mapped_column(JSONB)
    checksum: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    source: Mapped["Source"] = relationship(back_populates="documents")
    chunks: Mapped[list["DocumentChunk"]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )
    regulations: Mapped[list["Regulation"]] = relationship(back_populates="source_document")
    risk_events: Mapped[list["RiskEvent"]] = relationship(back_populates="source_document")
    trade_flows: Mapped[list["TradeFlow"]] = relationship(back_populates="source_document")


class DocumentChunk(Base):
    __tablename__ = "document_chunks"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    source_document_id: Mapped[int] = mapped_column(
        ForeignKey("source_documents.id", ondelete="CASCADE"), index=True
    )
    chunk_index: Mapped[int] = mapped_column(nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    embedding_id: Mapped[Optional[str]] = mapped_column(String(128))
    metadata_json: Mapped[Optional[Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    document: Mapped["SourceDocument"] = relationship(back_populates="chunks")


class EntityLink(Base):
    __tablename__ = "entity_links"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    from_entity_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    from_entity_id: Mapped[int] = mapped_column(nullable=False, index=True)
    to_entity_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    to_entity_id: Mapped[int] = mapped_column(nullable=False, index=True)
    link_type: Mapped[str] = mapped_column(String(64), nullable=False)
    confidence: Mapped[Optional[float]] = mapped_column()
    metadata_json: Mapped[Optional[Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
