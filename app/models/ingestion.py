"""Ingestion run tracking and raw API payload storage."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any, Optional

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.source import Source


class IngestionRun(Base):
    __tablename__ = "ingestion_runs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    parameters_json: Mapped[Optional[Any]] = mapped_column(JSONB)
    error_message: Mapped[Optional[str]] = mapped_column(Text)
    stats_json: Mapped[Optional[Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    source: Mapped["Source"] = relationship(back_populates="ingestion_runs")
    raw_payloads: Mapped[list["RawApiPayload"]] = relationship(back_populates="ingestion_run")


class RawApiPayload(Base):
    __tablename__ = "raw_api_payloads"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    ingestion_run_id: Mapped[int] = mapped_column(
        ForeignKey("ingestion_runs.id", ondelete="CASCADE"), index=True
    )
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"), index=True)
    endpoint: Mapped[str] = mapped_column(String(1024), nullable=False)
    http_status: Mapped[Optional[int]] = mapped_column(Integer)
    request_params_json: Mapped[Optional[Any]] = mapped_column(JSONB)
    response_body_path: Mapped[Optional[str]] = mapped_column(String(1024))
    response_body_text: Mapped[Optional[str]] = mapped_column(Text)
    checksum: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    ingestion_run: Mapped["IngestionRun"] = relationship(back_populates="raw_payloads")
    source: Mapped["Source"] = relationship(back_populates="raw_payloads")
