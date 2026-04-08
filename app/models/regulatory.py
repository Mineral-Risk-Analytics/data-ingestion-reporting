"""Regulations and derived risk events."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Optional

from sqlalchemy import Date, DateTime, Float, ForeignKey, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class Regulation(Base):
    __tablename__ = "regulations"
    __table_args__ = (
        UniqueConstraint("source_document_id", "regulation_key", name="uq_regulation_doc_key"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    source_document_id: Mapped[int] = mapped_column(
        ForeignKey("source_documents.id", ondelete="CASCADE"), index=True
    )
    regulation_key: Mapped[str] = mapped_column(String(256), nullable=False)
    title: Mapped[Optional[str]] = mapped_column(String(1024))
    issuing_body: Mapped[Optional[str]] = mapped_column(String(512))
    geography: Mapped[Optional[str]] = mapped_column(String(256))
    policy_theme: Mapped[Optional[str]] = mapped_column(String(256))
    status: Mapped[Optional[str]] = mapped_column(String(128))
    publication_date: Mapped[Optional[date]] = mapped_column(Date)
    effective_date: Mapped[Optional[date]] = mapped_column(Date)
    summary: Mapped[Optional[str]] = mapped_column(Text)
    metadata_json: Mapped[Optional[Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    source_document: Mapped["SourceDocument"] = relationship(back_populates="regulations")


class RiskEvent(Base):
    __tablename__ = "risk_events"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    source_document_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("source_documents.id", ondelete="SET NULL"), index=True
    )
    event_type: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    event_date: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), index=True)
    title: Mapped[str] = mapped_column(String(1024), nullable=False)
    summary: Mapped[Optional[str]] = mapped_column(Text)
    severity_score: Mapped[Optional[float]] = mapped_column(Float)
    confidence_score: Mapped[Optional[float]] = mapped_column(Float)
    risk_categories_json: Mapped[Optional[Any]] = mapped_column(JSONB)
    geography_json: Mapped[Optional[Any]] = mapped_column(JSONB)
    metadata_json: Mapped[Optional[Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    source_document: Mapped[Optional["SourceDocument"]] = relationship(back_populates="risk_events")
