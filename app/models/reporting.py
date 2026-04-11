"""Report templates, report runs, insights, and analyst notes."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any, Optional

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class ReportTemplate(Base):
    """
    Defines a report type — focus entity type, audience, section structure, and
    pillar weight overrides. Platform-provided templates (is_platform_template=True)
    are available to all tenants. Customer-created templates are org-scoped.

    sections_config is an ordered JSONB array of section definitions:
    [{"section_type": "executive_summary", "depth": "brief"},
     {"section_type": "material_risk", "depth": "detailed"}, ...]

    weight_overrides allows per-template pillar weight adjustments for specialized
    reports (e.g. a regulatory compliance report may upweight regulatory to 0.40).
    """

    __tablename__ = "report_templates"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    org_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )  # NULL = platform template available to all tenants
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    focus_type: Mapped[str] = mapped_column(String(64), nullable=False)
    # material | geography | regulation | company
    audience_type: Mapped[str] = mapped_column(String(64), nullable=False)
    # internal | oem | investor | public
    is_platform_template: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    weight_overrides: Mapped[Optional[Any]] = mapped_column(JSONB)
    sections_config: Mapped[Optional[Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    focus_entities: Mapped[list["ReportTemplateFocusEntity"]] = relationship(
        back_populates="template", cascade="all, delete-orphan"
    )


class ReportTemplateFocusEntity(Base):
    """
    Links a report template to the specific entities it covers. entity_id is a
    string (not a typed FK) because it may reference different entity tables
    (materials, companies, regulations) depending on entity_type. The application
    layer enforces referential integrity.

    Example: a graphite market intelligence brief has entity_type="material",
    entity_id="3" (the materials.id for graphite).
    """

    __tablename__ = "report_template_focus_entities"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    template_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("report_templates.id", ondelete="CASCADE"), nullable=False, index=True
    )
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    # material | company | regulation | geography
    entity_id: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    template: Mapped["ReportTemplate"] = relationship(back_populates="focus_entities")


class ReportRun(Base):
    __tablename__ = "report_runs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    org_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("tenants.id", ondelete="SET NULL"), index=True
    )
    template_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("report_templates.id", ondelete="SET NULL")
    )
    report_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    audience_type: Mapped[str] = mapped_column(String(64), nullable=False)
    as_of_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    # queued | running | completed | failed
    parameters_json: Mapped[Optional[Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    insights: Mapped[list["ReportInsight"]] = relationship(
        back_populates="report_run", cascade="all, delete-orphan"
    )


class ReportInsight(Base):
    __tablename__ = "report_insights"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    report_run_id: Mapped[int] = mapped_column(
        ForeignKey("report_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    insight_type: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    related_company_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("companies.id", ondelete="SET NULL")
    )
    related_material_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("materials.id", ondelete="SET NULL")
    )
    related_regulation_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("regulations.id", ondelete="SET NULL")
    )
    related_geography_code: Mapped[Optional[str]] = mapped_column(String(2))
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    metadata_json: Mapped[Optional[Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    report_run: Mapped["ReportRun"] = relationship(back_populates="insights")


class AnalystNote(Base):
    """
    Free-text analyst annotation on any entity. entity_id is a string to support
    both UUID (companies) and integer (materials, regulations) PKs without a
    polymorphic FK setup.
    """

    __tablename__ = "analyst_notes"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # company | material | regulation | geography | risk_event
    entity_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    note_type: Mapped[str] = mapped_column(String(64), nullable=False)
    note_text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
