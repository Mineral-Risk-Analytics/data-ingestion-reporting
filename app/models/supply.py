"""Materials, trade flows, commodity prices, and HS code mappings."""

from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING, Any, Optional

from sqlalchemy import (
    Boolean, Date, DateTime, Float, ForeignKey, Integer,
    SmallInteger, String, Text, UniqueConstraint, func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.battery_chemistry import BatteryChemistryMaterial
    from app.models.company import CompanyMaterialExposure
    from app.models.criticality_signal import MaterialCriticalitySignal
    from app.models.documents import SourceDocument


class Material(Base):
    """
    A battery supply chain material tracked by the platform.  Materials are
    the primary anchor for market intelligence briefs.  After migration 023,
    this table is an identity anchor only — country share data lives in
    material_production_shares (material level) and hs_code_production_shares
    (stage level).

    criticality_score and patent_occurrence_trend are DENORMALIZED CACHES
    refreshed by the ingest pipeline; do not use them as authoritative sources.
    """

    __tablename__ = "materials"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    canonical_name: Mapped[str] = mapped_column(
        String(255), unique=True, nullable=False, index=True
    )
    category: Mapped[Optional[str]] = mapped_column(String(128), index=True)
    # cathode_active | anode | electrolyte | separator | structural | packaging
    symbol_or_code: Mapped[Optional[str]] = mapped_column(String(64))  # e.g. Li, Co
    hs_codes: Mapped[Optional[Any]] = mapped_column(JSONB)  # ["2825.20", "2836.91"]
    criticality_score: Mapped[Optional[float]] = mapped_column(
        Float,
        comment=(
            "DENORMALIZED CACHE — authoritative source is material_criticality_signals. "
            "Refreshed by ingest-usgs."
        ),
    )
    # primary_producing_countries removed in migration 023 — data moved to
    # material_production_shares (material level) and hs_code_production_shares (stage level).
    price_unit: Mapped[Optional[str]] = mapped_column(String(20))  # per_mt | per_kg
    # Regulatory criticality flags — used for compliance exposure detection and scoring uplift
    is_ira_critical_mineral: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    is_eu_crma_critical: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    # Added by migration 002_battery_chemistry
    patent_occurrence_trend: Mapped[Optional[str]] = mapped_column(
        String(16),
        nullable=True,
        comment=(
            "rising | declining | stable. DENORMALIZED CACHE — authoritative source "
            "is material_criticality_signals. Refreshed by _sync_patent_trend()."
        ),
    )
    data_availability: Mapped[Optional[str]] = mapped_column(
        String(32),
        nullable=True,
        comment=(
            "commercial | limited | no_benchmark. Used by chemistry_risk.py "
            "to compute score_confidence."
        ),
    )
    notes: Mapped[Optional[str]] = mapped_column(Text)
    verified: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    company_exposures: Mapped[list["CompanyMaterialExposure"]] = relationship(
        back_populates="material"
    )
    trade_flows: Mapped[list["TradeFlow"]] = relationship(back_populates="material")
    hs_mappings: Mapped[list["HsCodeMaterialMapping"]] = relationship(
        back_populates="material", cascade="all, delete-orphan"
    )
    commodity_prices: Mapped[list["CommodityPrice"]] = relationship(
        back_populates="material", cascade="all, delete-orphan"
    )
    criticality_signals: Mapped[list["MaterialCriticalitySignal"]] = relationship(
        back_populates="material", cascade="all, delete-orphan"
    )
    chemistry_uses: Mapped[list["BatteryChemistryMaterial"]] = relationship(
        back_populates="material"
    )
    production_shares: Mapped[list["MaterialProductionShare"]] = relationship(
        back_populates="material", cascade="all, delete-orphan"
    )
    source_aliases: Mapped[list["MaterialSourceAlias"]] = relationship(
        back_populates="canonical_material", cascade="all, delete-orphan"
    )


class MaterialSourceAlias(Base):
    """
    Reference table mapping external-source commodity names to canonical materials.

    Each row says: when source_system X uses source_name Y, it means
    canonical_material_id Z (or, if is_skipped=True, the row was deliberately
    not ingested — preserving the audit trail of considered-and-rejected names).

    Replaces four Python dicts that previously lived next to each parser
    (_CHAPTER_TO_MATERIAL, _PRICE_NAME_TO_MATERIAL, _MCS_COMMODITY_MAP,
    _COMMODITY_CONFIG).  Future ingestion sources (T4 export controls, T6
    end-use, Comtrade, etc.) add rows instead of new dicts.

    Lookup pattern: (source_system, lower(btrim(source_name))) is unique
    per the partial expression index in migration 036.  The
    `material_resolver` helper handles the normalization.

    See migration 036 for column comments.
    """

    __tablename__ = "material_source_aliases"
    __table_args__ = (
        # Unique constraint enforced by the expression index in the migration;
        # we don't redeclare it here because SQLAlchemy can't model the
        # lower(btrim(...)) expression cleanly.  Insertions go through
        # PG ON CONFLICT DO UPDATE on that index.
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_system: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    source_name: Mapped[str] = mapped_column(String(255), nullable=False)
    canonical_material_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("materials.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    is_skipped: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false",
    )
    skip_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Default true preserves existing single-chapter aliases.  When false,
    # the CLI writes only per-HS-prefix shares for this alias and skips
    # material-level signal upserts.  Used for upstream-stage chapters
    # that share a canonical with a sibling chapter (e.g. BAUXITE AND
    # ALUMINA → Aluminum, where ALUMINUM is the primary).
    writes_material_signals: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true",
    )
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(),
    )

    canonical_material: Mapped[Optional["Material"]] = relationship(
        back_populates="source_aliases"
    )


class MaterialProductionShare(Base):
    """
    Country-level production volume and share for a material in a given year.
    Granularity: material × country × year (not stage-specific).

    Populated by ``bdi-ingest ingest-usgs`` from the USGS Mineral Commodity
    Summaries world data CSV.  Used by global_rollup.py as a fallback weighting
    source when stage-level data (hs_code_production_shares) is unavailable.

    NOT replaced by hs_code_production_shares — both tables coexist.  See
    docs/hs-code-redesign.md § "Relationship to Existing material_production_shares".
    """

    __tablename__ = "material_production_shares"
    __table_args__ = (
        UniqueConstraint(
            "material_id", "country_code", "reference_year",
            name="uq_material_production_share",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    material_id: Mapped[int] = mapped_column(
        ForeignKey("materials.id", ondelete="CASCADE"), nullable=False, index=True
    )
    country_code: Mapped[str] = mapped_column(String(2), nullable=False, index=True)
    reference_year: Mapped[int] = mapped_column(Integer, nullable=False)
    production_volume: Mapped[Optional[float]] = mapped_column(
        Float,
        nullable=True,
        comment="Raw production volume from MCS (unit in unit_of_measure)",
    )
    production_share: Mapped[float] = mapped_column(
        Float,
        nullable=False,
        comment="Fraction of world total production (0.0–1.0)",
    )
    unit_of_measure: Mapped[Optional[str]] = mapped_column(
        String(64),
        nullable=True,
        comment="Unit from MCS UNIT_MEAS column, e.g. 'metric tons', 'kilograms'",
    )
    data_source: Mapped[str] = mapped_column(
        String(64), nullable=False, default="usgs_mcs",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    material: Mapped["Material"] = relationship(back_populates="production_shares")


class MaterialCapacityShare(Base):
    """
    Country-level installed capacity volume and share for a material in a given year.
    Distinct from ``MaterialProductionShare``: capacity = theoretical maximum
    output; production = actual delivered tonnes.  Together they enable
    spare-capacity / utilization-overhang signals in scoring.

    Granularity: material × country × year × detail_type.  ``detail_type``
    is the verbatim USGS ``Statistics_detail`` string (e.g. "Smelter capacity",
    "Refinery capacity", "Titanium sponge metal Capacity", "TiO2 Pigment
    Capacity").  Required because some chapters publish multiple capacity
    streams under one canonical material (TITANIUM has both sponge metal
    AND TiO2 pigment capacities — different products, same material).

    ``capacity_share`` is computed per (material × year × detail_type)
    bucket so multi-stream chapters don't produce shares > 1.0.

    Populated by ``bdi-ingest ingest-usgs`` from MCS 2026 Capacity rows.
    Added by migration 045 (2026-05-31).
    """

    __tablename__ = "material_capacity_shares"
    __table_args__ = (
        UniqueConstraint(
            "material_id", "country_code", "reference_year", "detail_type",
            name="uq_material_capacity_share",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    material_id: Mapped[int] = mapped_column(
        ForeignKey("materials.id", ondelete="CASCADE"), nullable=False, index=True
    )
    country_code: Mapped[str] = mapped_column(String(2), nullable=False, index=True)
    reference_year: Mapped[int] = mapped_column(Integer, nullable=False)
    detail_type: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment=(
            "Verbatim MCS Statistics_detail. Distinguishes capacity streams "
            "within a chapter (TITANIUM: 'Titanium sponge metal Capacity' vs "
            "'TiO2 Pigment Capacity')."
        ),
    )
    capacity_volume: Mapped[Optional[float]] = mapped_column(
        Float,
        nullable=True,
        comment="Raw capacity volume from MCS (unit in unit_of_measure)",
    )
    capacity_share: Mapped[float] = mapped_column(
        Float,
        nullable=False,
        comment=(
            "Fraction of world capacity within this (material, year, detail_type) "
            "bucket (0.0–1.0)."
        ),
    )
    unit_of_measure: Mapped[Optional[str]] = mapped_column(
        String(64),
        nullable=True,
        comment="Unit from MCS Unit column, e.g. 'metric tons', 'kilograms'",
    )
    data_source: Mapped[str] = mapped_column(
        String(64), nullable=False, default="usgs_mcs",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class CountryMaterialRelevance(Base):
    """
    Per-(country, material, role) relevance flag.  Captures the fact that
    each material has a distinct set of relevant producing and consuming
    countries — Australia is a top lithium producer but a minor cobalt
    producer; DRC dominates cobalt but not nickel; China is a major
    refiner of cathode chemistry but only a moderate raw-ore producer.

    Replaces (eventually) the per-country binary ``countries.is_major_producer``
    and ``is_major_consumer`` flags, and the global ``{CN, CD, RU}`` HCG
    set in the scoring engine.  In Phase 1 (the migration introducing
    this table) the data is purely informational — the scoring engine
    still reads the global HCG set.  Phase 2 swaps the scoring engine
    to read ``is_hcg`` from this table on a per-(material, country)
    basis.

    Producer rows are auto-seeded from ``material_production_shares``
    using share-based thresholds (top ≥ 30%, mid 10-30%, minor 1-10%,
    HCG ≥ 40%).  Consumer rows start as placeholders for each
    ``is_major_consumer`` country and are refined by partner curation
    or by future Comtrade-import-share calculations.

    Granularity: one row per (material, country, role).  A single
    country can have BOTH a producer row and a consumer row for the
    same material (e.g. Germany imports lithium AND has minor lithium
    production).
    """

    __tablename__ = "country_material_relevance"
    __table_args__ = (
        UniqueConstraint(
            "material_id", "country_code", "role",
            name="uq_country_material_relevance",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    material_id: Mapped[int] = mapped_column(
        ForeignKey("materials.id", ondelete="CASCADE"), nullable=False, index=True
    )
    country_code: Mapped[str] = mapped_column(
        String(2), nullable=False, index=True,
        comment="ISO-3166-1 alpha-2 country code.  Joins to countries.iso2.",
    )
    role: Mapped[str] = mapped_column(
        String(16), nullable=False,
        comment="'producer' or 'consumer'.",
    )
    tier: Mapped[Optional[str]] = mapped_column(
        String(16), nullable=True,
        comment=(
            "Relevance tier: 'top' (≥30% share for producers), "
            "'mid' (10-30%), 'minor' (1-10%), 'emerging' "
            "(announced but not yet producing)."
        ),
    )
    is_hcg: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false",
        comment=(
            "High-Concentration Geography flag, per-(material, country). "
            "Default threshold: share ≥ 40%.  Phase 2: scoring engine "
            "reads this column instead of the global HCG set."
        ),
    )
    source: Mapped[str] = mapped_column(
        String(32), nullable=False,
        comment=(
            "'mcs_share' (auto-seeded), 'partner_curated' (manual), "
            "or 'derived' (computed from another source)."
        ),
    )
    derived_share: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True,
        comment="Production share snapshot at seed time (0.0–1.0).",
    )
    reference_year: Mapped[Optional[int]] = mapped_column(
        SmallInteger, nullable=True,
        comment="Year of the derived_share snapshot.",
    )
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(),
    )

    material: Mapped["Material"] = relationship()


class TradeFlow(Base):
    """
    A single import or export observation from Comtrade trade data.
    period is YYYY-MM or YYYY depending on source granularity.

    hs_code stores the 6-digit subheading returned by the Comtrade API
    (e.g. '260500' for cobalt ores), NOT the 4-digit prefix used to query.

    material_id and hs_mapping_id are both resolved at ingest time from
    hs_code_material_mappings.  Historically-ingested rows (before migration 027)
    have hs_mapping_id=NULL; the backfill query in the migration docstring can
    populate them without re-hitting the API.
    """

    __tablename__ = "trade_flows"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    source_document_id: Mapped[int] = mapped_column(
        ForeignKey("source_documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    period: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    reporter_country: Mapped[str] = mapped_column(String(8), nullable=False)
    partner_country: Mapped[str] = mapped_column(String(8), nullable=False)
    hs_code: Mapped[Optional[str]] = mapped_column(
        String(32),
        index=True,
        comment="6-digit HS subheading from Comtrade API response cmdCode field",
    )
    hs_description: Mapped[Optional[str]] = mapped_column(String(512))
    material_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("materials.id", ondelete="SET NULL")
    )
    hs_mapping_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("hs_code_material_mappings.id", ondelete="SET NULL"),
        nullable=True,
        comment=(
            "FK to hs_code_material_mappings.  Populated at Comtrade ingest time "
            "alongside material_id.  NULL for rows ingested before migration 027."
        ),
    )
    import_export_flag: Mapped[str] = mapped_column(String(16), nullable=False)
    # import | export
    quantity: Mapped[Optional[float]] = mapped_column(Float)
    quantity_unit: Mapped[Optional[str]] = mapped_column(String(64))
    trade_value_usd: Mapped[Optional[float]] = mapped_column(Float)
    metadata_json: Mapped[Optional[Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    source_document: Mapped["SourceDocument"] = relationship(back_populates="trade_flows")
    material: Mapped[Optional["Material"]] = relationship(back_populates="trade_flows")
    hs_mapping: Mapped[Optional["HsCodeMaterialMapping"]] = relationship(
        back_populates="trade_flows"
    )


class HsCodeMaterialMapping(Base):
    """
    Maps HS code prefixes to materials, with supply chain stage context.

    Each row represents one node in the supply chain graph for a material:
      (hs_code_prefix, material_id, market_scope) is unique.

    digit_count reflects the precision of the prefix:
      4 = HS heading (broad, e.g. all nickel ores under 2604)
      6 = HS subheading (international, e.g. 260400 for nickel ores specifically)
      8 = EU Combined Nomenclature extension
      10 = US HTS extension (market_scope='us' only)

    market_scope separates international and country-specific codes:
      global = 4/6-digit WCO codes, compatible with UN Comtrade queries
      us     = 10-digit US HTS codes from USGS MCS tariff tables
      eu     = 8-digit EU CN codes

    SCORING RULE: Comtrade trade flow queries match market_scope='global' ONLY.
    US HTS codes are used for tariff exposure scoring within the US market scope.

    keywords (added migration 026, Phase 1.5) will store compound/trade-name
    aliases for event attribution lookups.  Do not maintain a parallel hard-coded
    list anywhere in application code — this column is the single source of truth.
    """

    __tablename__ = "hs_code_material_mappings"
    __table_args__ = (
        UniqueConstraint(
            "hs_code_prefix", "material_id", "market_scope",
            name="uq_hs_material_scope",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    hs_code_prefix: Mapped[str] = mapped_column(
        String(16), nullable=False, index=True,
        comment="Prefix without dots, e.g. '2604', '260400', '2604000000'",
    )
    material_id: Mapped[int] = mapped_column(
        ForeignKey("materials.id", ondelete="CASCADE"), nullable=False, index=True
    )
    description: Mapped[Optional[str]] = mapped_column(String(512))
    confidence: Mapped[float] = mapped_column(
        Float,
        nullable=False,
        default=1.0,
        comment=(
            "Prefix specificity score (0–1).  1.0 = unambiguous single-material code. "
            "Lower values indicate the prefix covers multiple materials."
        ),
    )
    digit_count: Mapped[int] = mapped_column(
        SmallInteger,
        nullable=False,
        default=4,
        comment="4 | 6 | 8 | 10",
    )
    market_scope: Mapped[str] = mapped_column(
        String(8),
        nullable=False,
        default="global",
        comment="global | us | eu",
    )
    supply_chain_stage: Mapped[Optional[str]] = mapped_column(
        String(16),
        nullable=True,
        comment=(
            "ore | concentrate | intermediate | refined | "
            "battery_grade | fabricated | scrap"
        ),
    )
    stage_sequence: Mapped[Optional[int]] = mapped_column(
        SmallInteger,
        nullable=True,
        comment="1=ore, 2=concentrate, 3=intermediate, 4=refined, 5=battery_grade, "
                "6=fabricated, 7=scrap.  Derived from supply_chain_stage.",
    )
    # NOTE: HHI cache columns (hhi_score, hhi_reference_year, hhi_source) were
    # dropped in migration 035.  HHI is computed at runtime by hs_node_scorer
    # directly from hs_code_production_shares and persisted on
    # hs_code_geography_risk_scores.hhi_at_stage per (node × country × date).
    # The cache on this table was written-only and never read.

    # keywords column added in migration 026 (Phase 1.5).
    keywords: Mapped[Optional[Any]] = mapped_column(
        JSONB, nullable=True,
        comment="Array of compound/trade-name aliases used by material_resolver.py "
                "for event attribution at confidence 0.90.  Populated by "
                "seed-hs-mappings.  None means not yet seeded — callers treat as empty.",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    material: Mapped["Material"] = relationship(back_populates="hs_mappings")
    production_shares: Mapped[list["HsCodeProductionShare"]] = relationship(
        back_populates="hs_mapping", cascade="all, delete-orphan"
    )
    trade_flows: Mapped[list["TradeFlow"]] = relationship(back_populates="hs_mapping")


class HsCodeProductionShare(Base):
    """
    Country-level production share per HS code mapping node per year.
    Granularity: (hs_mapping_id, country_code, reference_year, market_scope, source).

    Two types of rows coexist, distinguished by market_scope:
      global — world mine production shares from USGS MCS world production table.
               Drives HHI computation in hs_node_scorer.py.
      us     — US import source shares from MCS "Import Sources (YYYY–YY):" section.
               Measures US trade dependency, NOT global concentration.

    SCORING RULE: Global risk scores (HHI, material_geography_risk_scores) use
    market_scope='global' rows ONLY.  US import source rows feed US-specific
    tariff exposure and IRA domestic content analysis.  Never mix in one HHI.

    Seeded by: MCS PDF parser (Phase 2) and manual seed entries.
    NOT a replacement for material_production_shares — that table remains as the
    material-level fallback in global_rollup.py.
    """

    __tablename__ = "hs_code_production_shares"
    __table_args__ = (
        UniqueConstraint(
            "hs_mapping_id",
            "country_code",
            "reference_year",
            "market_scope",
            "source",
            name="uq_hs_production_share",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    hs_mapping_id: Mapped[int] = mapped_column(
        ForeignKey("hs_code_material_mappings.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    country_code: Mapped[str] = mapped_column(
        String(2),
        nullable=False,
        index=True,
        comment="ISO 3166-1 alpha-2 country code",
    )
    production_share: Mapped[float] = mapped_column(
        Float,
        nullable=False,
        comment="Fraction of world total production for this HS node (0.0–1.0)",
    )
    production_volume: Mapped[Optional[float]] = mapped_column(
        Float,
        nullable=True,
        comment="Raw production tonnage where available",
    )
    unit_of_measure: Mapped[Optional[str]] = mapped_column(
        String(64),
        nullable=True,
        comment="e.g. 'metric tons', 'kilograms'",
    )
    reference_year: Mapped[int] = mapped_column(
        SmallInteger,
        nullable=False,
        comment="Data reference year (e.g. 2025 for MCS 2025)",
    )
    market_scope: Mapped[str] = mapped_column(
        String(8),
        nullable=False,
        default="global",
        comment="global = world mine production; us = US import source",
    )
    source: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="usgs_mcs",
        comment="usgs_mcs | comtrade | manual",
    )
    notes: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    hs_mapping: Mapped["HsCodeMaterialMapping"] = relationship(
        back_populates="production_shares"
    )


class CommodityPrice(Base):
    """
    Commodity price time series for key battery materials.
    Free sources: USGS mineral commodity summaries (annual).
    Paid sources: LME, Fastmarkets (monthly/daily — require subscription).

    ``hs_mapping_id`` (migration 030) scopes a price record to a specific supply
    chain stage node.  NULL for USGS annual averages where the traded form is not
    distinguished.  Examples of stage-scoped records: LME cobalt metal price
    vs. cobalt hydroxide 20.5% Co price — same material_id, different hs_mapping_id.

    ``price_form`` (migration 030) is a free-text benchmark descriptor for display
    and UI filtering.  Examples: "LiOH·H2O 56.5% min", "spodumene 6% Li2O SC",
    "LME cobalt", "cobalt hydroxide 20.5% Co".  Not a controlled vocabulary.

    The unique constraint ``uq_commodity_price`` includes hs_mapping_id and
    price_form so that multiple benchmark prices for the same material on the
    same date can coexist.  PostgreSQL treats NULL as distinct in unique
    constraints, so historical rows without stage attribution do not conflict.
    """

    __tablename__ = "commodity_prices"
    __table_args__ = (
        UniqueConstraint(
            "material_id",
            "price_date",
            "source",
            "hs_mapping_id",
            "price_form",
            name="uq_commodity_price",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    material_id: Mapped[int] = mapped_column(
        ForeignKey("materials.id", ondelete="CASCADE"), nullable=False, index=True
    )
    price_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    price_usd: Mapped[float] = mapped_column(Float, nullable=False)
    price_unit: Mapped[str] = mapped_column(String(20), nullable=False)  # per_mt | per_kg
    source: Mapped[str] = mapped_column(String(128), nullable=False)
    # usgs | lme | fastmarkets | manual
    hs_mapping_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("hs_code_material_mappings.id", ondelete="SET NULL"),
        nullable=True,
        comment=(
            "FK to hs_code_material_mappings. NULL for USGS annual averages "
            "and any price record where the specific traded form is not known. "
            "When set, scopes this price to a specific supply chain stage node."
        ),
    )
    price_form: Mapped[Optional[str]] = mapped_column(
        String(128),
        nullable=True,
        comment=(
            "Free-text benchmark descriptor, e.g. 'LiOH·H2O 56.5% min', "
            "'spodumene 6% Li2O', 'LME cobalt', 'cobalt hydroxide 20.5% Co'. "
            "For display and filtering; not a controlled vocabulary."
        ),
    )
    metadata_json: Mapped[Optional[Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    material: Mapped["Material"] = relationship(back_populates="commodity_prices")
