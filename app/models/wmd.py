"""World Mining Data (Austrian BMF, annual) — raw production + group tables.

Source docs: ADS/wmd_2026_extraction.md and
ADS/wmd_usgs_comparison_and_ingest_plan.md.  WMD is the independent
cross-check layer for ore-stage production; USGS stays canonical for
scoring inputs (single-source-per-stage rule).  Shares/HHI/stability
mixes are ALWAYS computed on read over the full country universe —
never stored renormalized (that's the bug the USGS table has).
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Float, ForeignKey, Integer, String, Text, UniqueConstraint, func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class WmdCommodity(Base):
    """One WMD sheet (65 per edition), optionally mapped to a material."""

    __tablename__ = "wmd_commodities"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    wmd_name: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    commodity_group: Mapped[Optional[str]] = mapped_column(String(32))
    # 'iron_ferroalloy' | 'non_ferrous' | 'precious' | 'industrial' | 'fuel'
    unit: Mapped[Optional[str]] = mapped_column(String(32))       # 'metr. t', 'kg', 'ct', 'Mio m3'
    content_basis: Mapped[Optional[str]] = mapped_column(String(32))
    # e.g. 'Co', 'Li2O', 'REO', 'Cr2O3', 'TiO2', 'P2O5', 'K2O', 'smelter_metal'
    material_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("materials.id", ondelete="SET NULL"), index=True
    )
    notes: Mapped[Optional[str]] = mapped_column(Text)

    production_rows = relationship("WmdProduction", back_populates="commodity")


class WmdProduction(Base):
    """Country × commodity × year production (WMD 6.4)."""

    __tablename__ = "wmd_production"
    __table_args__ = (
        UniqueConstraint(
            "commodity_id", "country_code", "year", name="uq_wmd_prod_row"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    commodity_id: Mapped[int] = mapped_column(
        ForeignKey("wmd_commodities.id", ondelete="CASCADE"), nullable=False, index=True
    )
    country_code: Mapped[str] = mapped_column(String(2), nullable=False, index=True)
    year: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    volume: Mapped[float] = mapped_column(Float, nullable=False)
    source_code: Mapped[Optional[str]] = mapped_column(String(2))
    # 'r' reported | 'e' estimated | 'p' provisional (per WMD Rem column)
    edition: Mapped[int] = mapped_column(Integer, nullable=False)   # e.g. 2026
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    commodity = relationship("WmdCommodity", back_populates="production_rows")


class WmdGroupProduction(Base):
    """Pre-aggregated group series (WMD 6.3c stability, 6.3d blocs)."""

    __tablename__ = "wmd_group_production"
    __table_args__ = (
        UniqueConstraint(
            "commodity_id", "group_type", "group_key", "year",
            name="uq_wmd_group_row",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    commodity_id: Mapped[int] = mapped_column(
        ForeignKey("wmd_commodities.id", ondelete="CASCADE"), nullable=False, index=True
    )
    group_type: Mapped[str] = mapped_column(String(16), nullable=False)
    # 'stability' (Stable/Fair/Unstable/Extreme Unstable) | 'bloc' (ACP..SCO)
    group_key: Mapped[str] = mapped_column(String(32), nullable=False)
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    volume: Mapped[float] = mapped_column(Float, nullable=False)
    edition: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
