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

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.company import Company
    from app.models.documents import SourceDocument
    from app.models.facility import Facility
    from app.models.supply import HsCodeMaterialMapping, Material


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
    geography_compliance_weights: Mapped[Optional[Any]] = mapped_column(
        JSONB,
        nullable=True,
        comment=(
            "Per-geography compliance risk weights (0.0–1.0). "
            "Keys: ISO2 country codes or 'DEFAULT'. "
            "1.0 = highest risk (targeted/non-compliant); 0.0 = exempt. "
            "NULL = use 0.50 universal default (no curation)."
        ),
    )
    verified: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
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
    source_aliases: Mapped[list["RegulationSourceAlias"]] = relationship(
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


class RegulationSourceAlias(Base):
    """
    Reference table mapping external source identifiers to canonical regulations.

    Each row says: when source_system X publishes source_key Y, it means
    regulation_id Z (or, if is_skipped=True, the row was deliberately not
    ingested — preserving the audit trail of considered-and-rejected
    external IDs).

    Mirrors ``MaterialSourceAlias`` so that the alias-resolver pattern is
    uniform across reference data.  Each ingester translates its own
    external IDs (CELEX, Federal Register doc number, US Code citation,
    OFAC SDN entity ID, GTA case ID, etc.) to ``regulation_id`` via this
    table — no auto-creation of regulations from arbitrary inputs.

    Lookup pattern: ``(source_system, lower(btrim(source_key)))`` is unique
    per the partial expression index in migration 039.  The
    ``RegulationAliasResolver`` helper handles the normalisation.

    See migration 039 for column comments.
    """

    __tablename__ = "regulation_aliases"
    __table_args__ = (
        # Unique constraint enforced by the expression index in the
        # migration; SQLAlchemy can't model the lower(btrim(...)) expression
        # cleanly here.  Insertions go through PG ON CONFLICT DO UPDATE on
        # that index.
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_system: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    source_key: Mapped[str] = mapped_column(String(255), nullable=False)
    regulation_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("regulations.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    is_skipped: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false",
    )
    skip_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(),
    )

    regulation: Mapped[Optional["Regulation"]] = relationship(
        back_populates="source_aliases"
    )


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
    verified: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
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
    # Precise event classification, distinct from the ingester-specific
    # ``event_type``.  Read by ``hs_node_scorer`` (tariff_exposure /
    # export_restriction sub-scores) and the geopolitical / regulatory
    # aggregators.  Canonical values (May 2026 — extend in
    # alembic/versions/040 docstring before adding new ones):
    #     TARIFF                — duties / tariff increases / threats
    #     EXPORT_RESTRICTION    — export bans, quotas, licensing, taxes
    #     IMPORT_DISRUPTION     — import tariffs / quotas / bans (demand-side)
    #     TRADE_CONCENTRATION   — derived from trade flow concentration
    #     REGULATORY_COMPLIANCE — compliance / due-diligence regulations
    #     TRADE_POLICY          — general trade policy without a specific measure
    event_subtype: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True, index=True,
    )
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
    verified: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # Refreshed on every ORM-mediated UPDATE.  Used by ingesters that
    # switched to parameter-stable content_hash + UPSERT semantics
    # (trade_signal_builder, opensanctions company + geo events,
    # 2026-05-11) so operators can query for rows touched in a recent
    # re-ingest pass.  See migration 041 for backfill details.
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
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
    hs_mapping_links: Mapped[list["RiskEventHsMapping"]] = relationship(
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
    review_status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="pending", index=True
    )
    # pending | confirmed | excluded
    # pending/confirmed both count toward score; excluded is filtered out by
    # evidence_query.get_events_for_company() and get_filing_signals().
    review_note: Mapped[Optional[str]] = mapped_column(Text)
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
    # Copied from RegulationMaterialScope.scope_type when the junction is
    # written by the EUR-Lex ingester.  Consumed downstream by
    # evidence_aggregator._impact via apply_scope_severity_multiplier so the
    # event's severity is amplified (banned 1.50×) or attenuated
    # (disclosure_required 0.50×) per-material.  Nullable: non-regulation
    # events and pre-migration rows have no scope_type recorded.
    scope_type: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
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


class RiskEventHsMapping(Base):
    """
    Junction: a risk event's relevance to a specific HS code mapping node.

    Complements :class:`RiskEventMaterial` with stage-level granularity.
    Both rows are written when an event has HS attribution; only
    ``RiskEventMaterial`` is written for pre-redesign historical data and
    keyword-only matches that lack stage resolution.

    A single event may link to multiple hs_mapping rows when multiple
    supply chain stages are mentioned (e.g. an article that covers both
    cobalt mining disruption and battery-grade cobalt sulfate shortages).

    relevance_score convention mirrors risk_event_materials:
      1.00 — direct HS code match (HS prefix found in document text)
      0.85 — keyword match with stage disambiguation via mappings table

    This table is the primary input for stage-level evidence scoring in
    ``evidence_query.get_events_for_material()`` and the trade signal
    builder's stage attribution logic.
    """

    __tablename__ = "risk_event_hs_mappings"
    __table_args__ = (
        UniqueConstraint(
            "risk_event_id", "hs_mapping_id", name="uq_risk_event_hs_mapping"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    risk_event_id: Mapped[int] = mapped_column(
        ForeignKey("risk_events.id", ondelete="CASCADE"), nullable=False, index=True
    )
    hs_mapping_id: Mapped[int] = mapped_column(
        ForeignKey("hs_code_material_mappings.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    relevance_score: Mapped[float] = mapped_column(
        Float,
        nullable=False,
        default=1.0,
        comment=(
            "1.00 = direct HS code match; 0.85 = keyword match with stage "
            "disambiguation.  Mirrors risk_event_materials convention."
        ),
    )
    match_reason: Mapped[Optional[str]] = mapped_column(
        String(64),
        nullable=True,
        comment="hs_code_match | keyword_match | trade_flow_match",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    risk_event: Mapped["RiskEvent"] = relationship(back_populates="hs_mapping_links")
    hs_mapping: Mapped["HsCodeMaterialMapping"] = relationship()
