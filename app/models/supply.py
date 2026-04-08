"""Suppliers, materials, and trade flows."""

from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING, Any, Optional

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.documents import SourceDocument


class Supplier(Base):
    __tablename__ = "suppliers"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    canonical_name: Mapped[str] = mapped_column(String(512), unique=True, nullable=False, index=True)
    supplier_type: Mapped[str] = mapped_column(String(64), nullable=False, default="other")
    headquarters_country: Mapped[Optional[str]] = mapped_column(String(2), index=True)
    headquarters_region: Mapped[Optional[str]] = mapped_column(String(128))
    public_ticker: Mapped[Optional[str]] = mapped_column(String(32), index=True)
    is_public: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    aliases: Mapped[list["SupplierAlias"]] = relationship(
        back_populates="supplier", cascade="all, delete-orphan"
    )
    exposures: Mapped[list["SupplierMaterialExposure"]] = relationship(back_populates="supplier")
    scores: Mapped[list["SupplierScore"]] = relationship(back_populates="supplier")


class SupplierAlias(Base):
    __tablename__ = "supplier_aliases"
    __table_args__ = (UniqueConstraint("supplier_id", "alias", name="uq_supplier_alias"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id", ondelete="CASCADE"), index=True)
    alias: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    alias_type: Mapped[str] = mapped_column(String(64), default="aka")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    supplier: Mapped["Supplier"] = relationship(back_populates="aliases")


class Material(Base):
    __tablename__ = "materials"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    canonical_name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    category: Mapped[Optional[str]] = mapped_column(String(128), index=True)
    symbol_or_code: Mapped[Optional[str]] = mapped_column(String(64))
    notes: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    exposures: Mapped[list["SupplierMaterialExposure"]] = relationship(back_populates="material")
    trade_flows: Mapped[list["TradeFlow"]] = relationship(back_populates="material")


class TradeFlow(Base):
    __tablename__ = "trade_flows"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    source_document_id: Mapped[int] = mapped_column(
        ForeignKey("source_documents.id", ondelete="CASCADE"), index=True
    )
    period: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    reporter_country: Mapped[str] = mapped_column(String(8), nullable=False)
    partner_country: Mapped[str] = mapped_column(String(8), nullable=False)
    hs_code: Mapped[Optional[str]] = mapped_column(String(32), index=True)
    hs_description: Mapped[Optional[str]] = mapped_column(String(512))
    material_id: Mapped[Optional[int]] = mapped_column(ForeignKey("materials.id", ondelete="SET NULL"))
    import_export_flag: Mapped[str] = mapped_column(String(16), nullable=False)
    quantity: Mapped[Optional[float]] = mapped_column()
    quantity_unit: Mapped[Optional[str]] = mapped_column(String(64))
    trade_value_usd: Mapped[Optional[float]] = mapped_column()
    metadata_json: Mapped[Optional[Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    source_document: Mapped["SourceDocument"] = relationship(back_populates="trade_flows")
    material: Mapped[Optional["Material"]] = relationship(back_populates="trade_flows")


class SupplierMaterialExposure(Base):
    __tablename__ = "supplier_material_exposure"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id", ondelete="CASCADE"), index=True)
    material_id: Mapped[int] = mapped_column(ForeignKey("materials.id", ondelete="CASCADE"), index=True)
    exposure_type: Mapped[str] = mapped_column(String(64), nullable=False)
    exposure_score: Mapped[float] = mapped_column(nullable=False)
    rationale: Mapped[Optional[str]] = mapped_column(Text)
    source_document_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("source_documents.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    supplier: Mapped["Supplier"] = relationship(back_populates="exposures")
    material: Mapped["Material"] = relationship(back_populates="exposures")


class SupplierScore(Base):
    __tablename__ = "supplier_scores"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id", ondelete="CASCADE"), index=True)
    as_of_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    material_risk_score: Mapped[Optional[float]] = mapped_column()
    regulatory_risk_score: Mapped[Optional[float]] = mapped_column()
    financial_pressure_score: Mapped[Optional[float]] = mapped_column()
    operational_risk_score: Mapped[Optional[float]] = mapped_column()
    overall_risk_score: Mapped[Optional[float]] = mapped_column()
    rationale_json: Mapped[Optional[Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    supplier: Mapped["Supplier"] = relationship(back_populates="scores")
