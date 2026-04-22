"""Materials, trade flows, commodity prices, and HS code mappings."""

from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING, Any, Optional

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, String, Text, UniqueConstraint, func
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
    A battery supply chain material tracked by the platform. Materials are the
    primary anchor for market intelligence briefs. criticality_score captures
    inherent supply-chain risk (substitutability, concentration, essentialness).
    hs_codes is a JSONB array of HS code strings for trade data classification.
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
    criticality_score: Mapped[Optional[float]] = mapped_column(Float)  # 0.0–1.0
    primary_producing_countries: Mapped[Optional[Any]] = mapped_column(JSONB)  # ["CN","CD","AU"]
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
        String(16), nullable=True,
        comment="rising | declining | stable. DENORMALIZED CACHE — authoritative source "
                "is material_criticality_signals. Refreshed by _sync_patent_trend().",
    )
    data_availability: Mapped[Optional[str]] = mapped_column(
        String(32), nullable=True,
        comment="commercial | limited | no_benchmark. Used by chemistry_risk.py "
                "to compute score_confidence.",
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


class TradeFlow(Base):
    """
    A single import or export observation from Census/Comtrade trade data.
    period is YYYY-MM or YYYY depending on granularity of the source.
    """

    __tablename__ = "trade_flows"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    source_document_id: Mapped[int] = mapped_column(
        ForeignKey("source_documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    period: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    reporter_country: Mapped[str] = mapped_column(String(8), nullable=False)
    partner_country: Mapped[str] = mapped_column(String(8), nullable=False)
    hs_code: Mapped[Optional[str]] = mapped_column(String(32), index=True)
    hs_description: Mapped[Optional[str]] = mapped_column(String(512))
    material_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("materials.id", ondelete="SET NULL")
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


class HsCodeMaterialMapping(Base):
    """
    Maps HS code prefixes to materials for automated trade data classification.
    confidence = 1.0 for exact single-material codes; lower for codes that map
    to multiple materials (e.g. 8507 covers all battery types).
    """

    __tablename__ = "hs_code_material_mappings"
    __table_args__ = (
        UniqueConstraint("hs_code_prefix", "material_id", name="uq_hs_material"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    hs_code_prefix: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    material_id: Mapped[int] = mapped_column(
        ForeignKey("materials.id", ondelete="CASCADE"), nullable=False, index=True
    )
    description: Mapped[Optional[str]] = mapped_column(String(512))
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    material: Mapped["Material"] = relationship(back_populates="hs_mappings")


class CommodityPrice(Base):
    """
    Commodity price time series for key battery materials.
    Free sources: USGS mineral commodity summaries (annual).
    Paid sources: LME, Fastmarkets (monthly/daily — require subscription).
    """

    __tablename__ = "commodity_prices"
    __table_args__ = (
        UniqueConstraint("material_id", "price_date", "source", name="uq_commodity_price"),
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
    metadata_json: Mapped[Optional[Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    material: Mapped["Material"] = relationship(back_populates="commodity_prices")
