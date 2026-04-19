"""Seed staleness review tables.

SeedReviewRun is the parent record for one invocation of the review process.
SeedReviewFinding is the per-candidate finding a reviewer emitted. Findings are
polymorphic on ``seed_type``; the typed subject identifier lives in
``seed_identifier_json`` and the human-readable form in ``seed_key``.

The UI consumes these tables to present "these seeded rows may be stale; here
is the evidence" and to record an analyst's triage decision (``status`` column,
``reviewed_at`` / ``reviewed_by`` / ``reviewer_note``).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import TYPE_CHECKING, Any, Optional

from sqlalchemy import (
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.documents import SourceDocument
    from app.models.regulatory import RiskEvent


class SeedReviewRun(Base):
    """One invocation of the seed staleness review."""

    __tablename__ = "seed_review_runs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), unique=True, nullable=False, default=uuid.uuid4
    )
    seed_types: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    # JSON array of seed_type strings, e.g. ["regulation","company"]. Stored
    # as JSONB (not a PG ARRAY) so the ORM is portable to SQLite for tests.
    since_date: Mapped[Optional[date]] = mapped_column(Date)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    total_seeds_checked: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    total_findings: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    parameters_json: Mapped[Optional[Any]] = mapped_column(JSONB)

    findings: Mapped[list["SeedReviewFinding"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )


class SeedReviewFinding(Base):
    """A single candidate the reviewer flagged for human review.

    ``seed_identifier_json`` carries the typed components of the subject so the
    UI can query "all findings about company CATL" via a GIN index on the JSONB
    without adding a column per seed type. ``seed_key`` is the human-readable
    form for display.
    """

    __tablename__ = "seed_review_findings"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("seed_review_runs.run_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Subject — what seed row is being reviewed
    seed_type: Mapped[str] = mapped_column(String(64), nullable=False)
    # regulation | company | material_exposure | supply_relationship | facility
    seed_key: Mapped[str] = mapped_column(String(512), nullable=False)
    seed_display_name: Mapped[Optional[str]] = mapped_column(Text)
    seed_identifier_json: Mapped[Any] = mapped_column(JSONB, nullable=False)

    # Evidence — what signal triggered the finding
    evidence_type: Mapped[str] = mapped_column(String(64), nullable=False)
    # federal_register_document | risk_event_company_link
    # | risk_event_material_link | sec_filing_mention | news_mention
    # | facility_text_mention
    source_document_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("source_documents.id", ondelete="CASCADE")
    )
    risk_event_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("risk_events.id", ondelete="SET NULL")
    )
    evidence_json: Mapped[Any] = mapped_column(JSONB, nullable=False)

    # Scoring and triage state
    relevance: Mapped[str] = mapped_column(String(16), nullable=False)
    # high | medium | low
    match_signals_json: Mapped[Any] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="open"
    )
    # open | acknowledged | dismissed
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    reviewed_by: Mapped[Optional[str]] = mapped_column(String(256))
    reviewer_note: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    run: Mapped["SeedReviewRun"] = relationship(back_populates="findings")
    source_document: Mapped[Optional["SourceDocument"]] = relationship()
    risk_event: Mapped[Optional["RiskEvent"]] = relationship()
