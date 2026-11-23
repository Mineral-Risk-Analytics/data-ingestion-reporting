"""Companies and their aliases, material exposures, supply relationships, and scores."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import TYPE_CHECKING, Any, Optional

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.documents import SourceDocument
    from app.models.facility import CompanyFacility
    from app.models.supply import Material


class Company(Base):
    """
    Every organization in the battery supply chain the platform tracks, scores,
    or references — from miners to OEMs. Replaces and broadens the former
    suppliers table. parent_company_id allows modeling corporate families
    (subsidiaries, joint ventures).
    """

    __tablename__ = "companies"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    canonical_name: Mapped[str] = mapped_column(
        String(512), unique=True, nullable=False, index=True
    )
    # ── public-profile columns (migration 054) ──────────────────────────
    slug: Mapped[Optional[str]] = mapped_column(
        String(160), unique=True, index=True,
        comment="URL-safe public identifier, backfilled from canonical_name.",
    )
    is_published: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false", index=True,
        comment=(
            "Public-site visibility gate. TRUE only for partner-reviewed "
            "companies (workbook publish flag). Loader never un-sets it."
        ),
    )
    legal_name: Mapped[Optional[str]] = mapped_column(String(512))
    # Free-string stage (legacy, Axis-B activity vocabulary). Migration 044
    # added the normalised FK column ``primary_activity_stage_fk`` alongside
    # this; both coexist during Phase 1. This column gets dropped in a
    # later cleanup once scoring code migrates to the FK.
    supply_chain_stage: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    # miner | refiner | cell_maker | pack_maker | oem | trader | other
    primary_activity_stage_fk: Mapped[Optional[str]] = mapped_column(
        String(64),
        ForeignKey("supply_chain_stages.stage_code", ondelete="RESTRICT"),
        index=True,
        comment=(
            "FK to supply_chain_stages.stage_code (Axis B / activity).  "
            "Distinct from facilities.supply_chain_stage and "
            "hs_code_material_mappings.supply_chain_stage which carry the "
            "product-form / Axis A taxonomy."
        ),
    )
    headquarters_country: Mapped[Optional[str]] = mapped_column(String(2), index=True)  # ISO2
    headquarters_region: Mapped[Optional[str]] = mapped_column(String(128))
    incorporated_country: Mapped[Optional[str]] = mapped_column(String(2))  # ISO2; FK-ready
    public_ticker: Mapped[Optional[str]] = mapped_column(String(32), index=True)
    is_public: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    duns_number: Mapped[Optional[str]] = mapped_column(String(32))
    lei: Mapped[Optional[str]] = mapped_column(String(20))
    # SEC enrichment (migration 044). cik is unique (partial index on
    # non-NULL); non-SEC filers leave it NULL.
    cik: Mapped[Optional[str]] = mapped_column(String(10))
    sic: Mapped[Optional[str]] = mapped_column(String(4))
    sic_description: Mapped[Optional[str]] = mapped_column(String(256))
    exchanges: Mapped[Optional[Any]] = mapped_column(JSONB)
    sec_metadata: Mapped[Optional[Any]] = mapped_column(JSONB)
    # Risk flags (migration 044)
    is_state_owned_or_influenced: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    has_facilities: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="true"
    )
    operates_as_trader: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    is_sanctioned: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    sanctioning_jurisdictions: Mapped[Optional[Any]] = mapped_column(JSONB)
    has_uflpa_designation: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    uflpa_status: Mapped[Optional[str]] = mapped_column(String(32))
    parent_company_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("companies.id", ondelete="SET NULL")
    )
    data_confidence: Mapped[Optional[float]] = mapped_column(Float)  # 0.0–1.0
    data_source: Mapped[Optional[str]] = mapped_column(String(128))  # sec_edgar | manual | etc.
    notes: Mapped[Optional[str]] = mapped_column(Text)
    # Public profile copy (migration 057).  The ONLY free-text company
    # field the public intelligence API exposes — written by the partner
    # workbook's public_intro column (or a future admin flow), NEVER
    # derived from ``notes`` (internal commentary must not leak).
    public_intro: Mapped[Optional[str]] = mapped_column(Text)
    verified: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    aliases: Mapped[list["CompanyAlias"]] = relationship(
        back_populates="company", cascade="all, delete-orphan"
    )
    exposures: Mapped[list["CompanyMaterialExposure"]] = relationship(
        back_populates="company", cascade="all, delete-orphan"
    )
    scores: Mapped[list["CompanyScore"]] = relationship(
        back_populates="company", cascade="all, delete-orphan"
    )
    facility_links: Mapped[list["CompanyFacility"]] = relationship(
        back_populates="company", cascade="all, delete-orphan"
    )
    subsidiaries: Mapped[list["Company"]] = relationship(
        foreign_keys=[parent_company_id]
    )


class CompanyAlias(Base):
    __tablename__ = "company_aliases"
    __table_args__ = (
        UniqueConstraint("company_id", "alias", name="uq_company_alias"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    alias: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    alias_type: Mapped[str] = mapped_column(String(64), nullable=False, default="aka")
    # aka | ticker | lei | former_name | abbreviation
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    company: Mapped["Company"] = relationship(back_populates="aliases")


class CompanyMaterialExposure(Base):
    """
    A company's exposure to a specific material at a specific supply chain stage.
    Primary input for material concentration and geopolitical scoring.
    source_geography is the ISO2 of where the material is primarily sourced from —
    this feeds the country_concentration sub-score in geopolitical_risk.py.
    """

    __tablename__ = "company_material_exposures"
    __table_args__ = (
        UniqueConstraint(
            "company_id", "material_id", "supply_chain_stage",
            name="uq_company_material_stage",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    material_id: Mapped[int] = mapped_column(
        ForeignKey("materials.id", ondelete="CASCADE"), nullable=False, index=True
    )
    supply_chain_stage: Mapped[str] = mapped_column(String(64), nullable=False)
    # mining | refining | cell | pack | oem
    exposure_score: Mapped[float] = mapped_column(Float, nullable=False)  # 0.0–1.0
    source_geography: Mapped[Optional[str]] = mapped_column(String(2))  # ISO2 primary sourcing country
    data_confidence: Mapped[Optional[float]] = mapped_column(Float)  # 0.0–1.0
    rationale: Mapped[Optional[str]] = mapped_column(Text)
    as_of_date: Mapped[Optional[date]] = mapped_column(Date)
    source_document_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("source_documents.id", ondelete="SET NULL")
    )
    verified: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")

    # ── filing facts (migration 053, partner workbook CME tab) ──────────
    # exposure_score stays the judgment column; these carry the auditable
    # facts behind it. See migration 053 docstring for full semantics.
    production_tonnage: Mapped[Optional[float]] = mapped_column(Float)
    production_unit: Mapped[Optional[str]] = mapped_column(String(64))
    # Free-form on purpose ('kt LCE', 't Au', 'Mt') — commodity bases must
    # not be silently normalised.
    production_year: Mapped[Optional[int]] = mapped_column(Integer)
    revenue_share_pct: Mapped[Optional[float]] = mapped_column(Float)  # 0-1
    revenue_year: Mapped[Optional[int]] = mapped_column(Integer)
    battery_grade_relevance: Mapped[Optional[float]] = mapped_column(Float)  # 0-1
    source_url: Mapped[Optional[str]] = mapped_column(Text)
    score_derivation: Mapped[Optional[str]] = mapped_column(String(32))
    # curated_seed | derived_revenue_share | default_unscored | NULL(pre-053)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    company: Mapped["Company"] = relationship(back_populates="exposures")
    material: Mapped["Material"] = relationship(back_populates="company_exposures")


class CompanySupplyRelationship(Base):
    """
    Confirmed or estimated buyer-supplier relationship between two companies for a
    given material. Enables Tier 2/3 risk propagation: distress at a Tier 2 cathode
    supplier flows upward to Tier 1 cell manufacturers. data_confidence is important
    because many relationships are estimated from filings rather than confirmed.
    """

    __tablename__ = "company_supply_relationships"
    __table_args__ = (
        UniqueConstraint(
            "buyer_id", "supplier_id", "material_id",
            name="uq_supply_relationship",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    buyer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    supplier_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    material_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("materials.id", ondelete="SET NULL")
    )
    relationship_type: Mapped[str] = mapped_column(
        String(64), nullable=False, default="direct"
    )  # direct | indirect | estimated
    data_confidence: Mapped[Optional[float]] = mapped_column(Float)  # 0.0–1.0
    volume_share_pct: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True
    )
    # Fraction of buyer's demand for this material supplied by this
    # supplier. 0.0–1.0. NULL = relationship confirmed but share unknown.
    source_document_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("source_documents.id", ondelete="SET NULL")
    )
    valid_from: Mapped[Optional[date]] = mapped_column(Date)
    valid_to: Mapped[Optional[date]] = mapped_column(Date)
    verified: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")

    # ── agreement detail (migration 053, partner workbook SR tab) ───────
    agreement_type: Mapped[Optional[str]] = mapped_column(String(32))
    # offtake | supply_agreement | joint_development | equity_offtake |
    # framework | spot | jv | unknown — complements relationship_type
    # (which stays evidential: direct | indirect | estimated | framework).
    contract_term_years: Mapped[Optional[float]] = mapped_column(Float)  # 0 = spot
    announced_date: Mapped[Optional[date]] = mapped_column(Date)
    source_url: Mapped[Optional[str]] = mapped_column(Text)
    notes: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class CompanyScore(Base):
    """
    Append-only score history per company. One row per scoring run. Renamed from
    supplier_scores to reflect the broader scope of the companies table.
    rationale_json stores the full SupplierScoreRationale structure from types.py.
    Never overwrite existing rows — historical rows are preserved for delta tracking.
    """

    __tablename__ = "company_scores"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    as_of_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    material_concentration_risk_score: Mapped[Optional[float]] = mapped_column(Float)
    geopolitical_trade_risk_score: Mapped[Optional[float]] = mapped_column(Float)
    regulatory_risk_score: Mapped[Optional[float]] = mapped_column(Float)
    operational_risk_score: Mapped[Optional[float]] = mapped_column(Float)
    financial_pressure_score: Mapped[Optional[float]] = mapped_column(Float)
    overall_risk_score: Mapped[Optional[float]] = mapped_column(Float)
    supply_chain_propagation_score: Mapped[Optional[float]] = mapped_column(Float)
    propagation_depth_used: Mapped[Optional[int]] = mapped_column(Integer)
    rationale_json: Mapped[Optional[Any]] = mapped_column(JSONB)
    scoring_version: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="2.0"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    company: Mapped["Company"] = relationship(back_populates="scores")
