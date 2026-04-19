"""Regulations, risk events, and all event-entity junction tables.

The intelligence graph lives here. Every risk_event connects to the companies,
materials, regulations, and geographies it affects via dedicated junction tables.
Intelligence is derived by traversing these links — not by re-parsing raw text at
query time. The richer the junction data, the more powerful the scoring and reports.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import TYPE_CHECKING, Any, Optional

from sqlalchemy import Date, DateTime, Float, ForeignKey, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.company import Company
    from app.models.documents import SourceDocument
    from app.models.facility import Facility
    from app.models.supply import Material


class Regulation(Base):
    """
    A regulatory instrument that may affect battery supply chains.
    regulation_key is a stable human-readable identifier (e.g. EU_BATTERY_REG_2023,
    UFLPA, IRA_DOMESTIC). Used as the primary lookup key — more stable than id
    across environments and data resets.
    """

    __tablename__ = "regulations"
    __table_args__ = (
        UniqueConstraint("regulation_key", name="uq_regulation_key"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    source_document_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("source_documents.id", ondelete="SET NULL"), index=True
    )
    regulation_key: Mapped[str] = mapped_column(String(256), nullable=False, unique=True)
    title: Mapped[Optional[str]] = mapped_column(String(1024))
    issuing_body: Mapped[Optional[str]] = mapped_column(String(512))
    geography: Mapped[Optional[str]] = mapped_column(String(256))  # ISO2 or region
    policy_theme: Mapped[Optional[str]] = mapped_column(String(256))
    status: Mapped[Optional[str]] = mapped_column(String(128))
    # proposed | enacted | effective | superseded
    publication_date: Mapped[Optional[date]] = mapped_column(Date)
    effective_date: Mapped[Optional[date]] = mapped_column(Date)
    summary: Mapped[Optional[str]] = mapped_column(Text)
    metadata_json: Mapped[Optional[Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    source_document: Mapped[Optional["SourceDocument"]] = relationship(
        back_populates="regulations"
    )
    material_scopes: Mapped[list["RegulationMaterialScope"]] = relationship(
        back_populates="regulation", cascade="all, delete-orphan"
    )
    geography_scopes: Mapped[list["RegulationGeographyScope"]] = relationship(
        back_populates="regulation", cascade="all, delete-orphan"
    )
    company_exposures: Mapped[list["CompanyRegulationExposure"]] = relationship(
        back_populates="regulation", cascade="all, delete-orphan"
    )


class RegulationMaterialScope(Base):
    """Which materials a regulation covers, restricts, or bans."""

    __tablename__ = "regulation_material_scope"
    __table_args__ = (
        UniqueConstraint("regulation_id", "material_id", name="uq_reg_material"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    regulation_id: Mapped[int] = mapped_column(
        ForeignKey("regulations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    material_id: Mapped[int] = mapped_column(
        ForeignKey("materials.id", ondelete="CASCADE"), nullable=False, index=True
    )
    scope_type: Mapped[str] = mapped_column(String(64), nullable=False, default="covered")
    # covered | restricted | banned | disclosure_required
    notes: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    regulation: Mapped["Regulation"] = relationship(back_populates="material_scopes")


class RegulationGeographyScope(Base):
    """Which geographies (jurisdictions or origin countries) a regulation applies to."""

    __tablename__ = "regulation_geography_scope"
    __table_args__ = (
        UniqueConstraint("regulation_id", "country_code", name="uq_reg_geography"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    regulation_id: Mapped[int] = mapped_column(
        ForeignKey("regulations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    country_code: Mapped[str] = mapped_column(String(8), nullable=False)
    # ISO2 or bloc identifier e.g. "EU", "US"
    scope_type: Mapped[str] = mapped_column(String(64), nullable=False, default="jurisdiction")
    # jurisdiction | origin_country | targeted_country
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    regulation: Mapped["Regulation"] = relationship(back_populates="geography_scopes")


class CompanyRegulationExposure(Base):
    """
    A company's exposure to a specific regulation, with compliance status.
    This table replaces the text-derived get_active_compliance_obligations() approach
    in evidence_query.py with a structured, queryable record. Data source for the
    compliance_obligation_uplift points in regulatory_risk.py.
    """

    __tablename__ = "company_regulation_exposure"
    __table_args__ = (
        UniqueConstraint("company_id", "regulation_id", name="uq_company_regulation"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    regulation_id: Mapped[int] = mapped_column(
        ForeignKey("regulations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    compliance_status: Mapped[str] = mapped_column(
        String(64), nullable=False, default="unknown"
    )
    # compliant | non_compliant | partial | unknown
    exposure_reason: Mapped[Optional[str]] = mapped_column(Text)
    assessed_at: Mapped[Optional[date]] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    company: Mapped["Company"] = relationship()
    regulation: Mapped["Regulation"] = relationship(back_populates="company_exposures")


# ---------------------------------------------------------------------------
# Risk Events — the core intelligence stream
# ---------------------------------------------------------------------------

class RiskEvent(Base):
    """
    A structured supply chain risk event derived from ingested documents.
    Append-only — never modify existing rows.

    content_hash (SHA-256 of title + summary + event_date) provides idempotency:
    the ingestion pipeline checks this before inserting to avoid duplicate events
    from the same underlying news item or filing.

    The four junction tables (company_links, material_links, regulation_links,
    geography_links) connect this event to the entity graph. Scoring reads through
    these junction tables rather than parsing raw text at query time.
    """

    __tablename__ = "risk_events"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    source_document_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("source_documents.id", ondelete="SET NULL"), index=True
    )
    event_type: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    event_date: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), index=True
    )
    title: Mapped[str] = mapped_column(String(1024), nullable=False)
    summary: Mapped[Optional[str]] = mapped_column(Text)
    severity_score: Mapped[Optional[float]] = mapped_column(Float)  # 0.0–1.0
    confidence_score: Mapped[Optional[float]] = mapped_column(Float)  # 0.0–1.0
    risk_categories_json: Mapped[Optional[Any]] = mapped_column(JSONB)
    # array of RiskCategory values: ["material_concentration", "geopolitical_trade"]
    geography_json: Mapped[Optional[Any]] = mapped_column(JSONB)
    # {"primary": "CN", "secondary": ["RU", "CD"]}
    content_hash: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    # SHA-256 of (title + summary + event_date) — used for deduplication
    metadata_json: Mapped[Optional[Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    source_document: Mapped[Optional["SourceDocument"]] = relationship(
        back_populates="risk_events"
    )
    company_links: Mapped[list["RiskEventCompany"]] = relationship(
        back_populates="risk_event", cascade="all, delete-orphan"
    )
    material_links: Mapped[list["RiskEventMaterial"]] = relationship(
        back_populates="risk_event", cascade="all, delete-orphan"
    )
    regulation_links: Mapped[list["RiskEventRegulation"]] = relationship(
        back_populates="risk_event", cascade="all, delete-orphan"
    )
    geography_links: Mapped[list["RiskEventGeography"]] = relationship(
        back_populates="risk_event", cascade="all, delete-orphan"
    )
    facility_links: Mapped[list["RiskEventFacility"]] = relationship(
        back_populates="risk_event", cascade="all, delete-orphan"
    )


class RiskEventCompany(Base):
    """
    Junction: a risk event's relevance to a specific company.
    Renamed from risk_event_suppliers. relevance_score maps directly to
    relevance_multiplier in compute_event_impact().

    Calibration:
      1.00 — direct / named company match
      0.85 — strong geographic or material match
      0.70 — indirect match (industry-level, broad geography)

    Never insert a row with relevance_score < 0.70 — the scoring formula
    minimum is 0.70.
    """

    __tablename__ = "risk_event_companies"
    __table_args__ = (
        UniqueConstraint("risk_event_id", "company_id", name="uq_risk_event_company"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    risk_event_id: Mapped[int] = mapped_column(
        ForeignKey("risk_events.id", ondelete="CASCADE"), nullable=False, index=True
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    relevance_score: Mapped[float] = mapped_column(Float, nullable=False)
    match_reason: Mapped[Optional[str]] = mapped_column(String(64))
    # named_company | geography | material_hs | category_broad
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    risk_event: Mapped["RiskEvent"] = relationship(back_populates="company_links")
    company: Mapped["Company"] = relationship()


class RiskEventMaterial(Base):
    """
    Junction: a risk event's relevance to a specific material.
    Enables material-anchored scoring and market intelligence briefs without
    requiring any company data.
    """

    __tablename__ = "risk_event_materials"
    __table_args__ = (
        UniqueConstraint("risk_event_id", "material_id", name="uq_risk_event_material"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    risk_event_id: Mapped[int] = mapped_column(
        ForeignKey("risk_events.id", ondelete="CASCADE"), nullable=False, index=True
    )
    material_id: Mapped[int] = mapped_column(
        ForeignKey("materials.id", ondelete="CASCADE"), nullable=False, index=True
    )
    relevance_score: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    match_reason: Mapped[Optional[str]] = mapped_column(String(64))
    # named_material | hs_code | keyword_match
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    risk_event: Mapped["RiskEvent"] = relationship(back_populates="material_links")


class RiskEventRegulation(Base):
    """Junction: a risk event's relevance to a specific regulation."""

    __tablename__ = "risk_event_regulations"
    __table_args__ = (
        UniqueConstraint("risk_event_id", "regulation_id", name="uq_risk_event_regulation"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    risk_event_id: Mapped[int] = mapped_column(
        ForeignKey("risk_events.id", ondelete="CASCADE"), nullable=False, index=True
    )
    regulation_id: Mapped[int] = mapped_column(
        ForeignKey("regulations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    relevance_score: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    match_reason: Mapped[Optional[str]] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    risk_event: Mapped["RiskEvent"] = relationship(back_populates="regulation_links")


class RiskEventGeography(Base):
    """
    Junction: a risk event's relevance to a specific geography (country).
    geography_context indicates whether this is the primary affected geography,
    a secondary affected geography, or merely mentioned in context.
    """

    __tablename__ = "risk_event_geographies"
    __table_args__ = (
        UniqueConstraint("risk_event_id", "country_code", name="uq_risk_event_geography"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    risk_event_id: Mapped[int] = mapped_column(
        ForeignKey("risk_events.id", ondelete="CASCADE"), nullable=False, index=True
    )
    country_code: Mapped[str] = mapped_column(String(2), nullable=False, index=True)  # ISO2
    geography_context: Mapped[str] = mapped_column(
        String(64), nullable=False, default="primary"
    )
    # primary | secondary | mentioned
    relevance_score: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    risk_event: Mapped["RiskEvent"] = relationship(back_populates="geography_links")


class RiskEventFacility(Base):
    """
    Junction: a risk event's relevance to a specific facility.

    Mirrors :class:`RiskEventGeography` and :class:`RiskEventCompany`. Use this
    when an event is known to affect a specific physical site (a mine fire, a
    refinery sanction, a permit revocation) rather than the operating company
    or the country at large. The ``relevance_score`` follows the same convention
    as the other junctions and is passed through as the relevance multiplier in
    :func:`compute_event_impact`.
    """

    __tablename__ = "risk_event_facilities"
    __table_args__ = (
        UniqueConstraint(
            "risk_event_id", "facility_id", name="uq_risk_event_facility"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    risk_event_id: Mapped[int] = mapped_column(
        ForeignKey("risk_events.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    facility_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("facilities.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    relevance_score: Mapped[float] = mapped_column(
        Float, nullable=False, default=1.0
    )
    match_reason: Mapped[Optional[str]] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    risk_event: Mapped["RiskEvent"] = relationship(back_populates="facility_links")
    facility: Mapped["Facility"] = relationship()
