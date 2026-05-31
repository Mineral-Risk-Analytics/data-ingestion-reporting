"""Per-company vehicle model + per-model battery chemistry mix.

These two tables give the scoring pipeline product-level grain on chemistry
exposure. ``CompanyVehicleModel`` rows describe each model an OEM ships
(``Tesla.Model 3 Long Range``, ``Tesla.Model 3 Standard``); the
``VehicleModelChemistry`` rows describe what battery chemistry (and at what
share, in what time window) sits in that model.

Production volume is intentionally nullable. When present, the company-level
chemistry mix is volume-weighted; when absent, models contribute equally
(uniform default). Industry-average market shares from
``BatteryChemistry.current_market_share_pct`` are NEVER substituted — those
would silently inject signal that isn't about this company.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import TYPE_CHECKING, Any, Optional

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.battery_chemistry import BatteryChemistry
    from app.models.company import Company


class CompanyVehicleModel(Base):
    """A vehicle model produced by a company in a given model-year window."""

    __tablename__ = "company_vehicle_models"
    __table_args__ = (
        UniqueConstraint(
            "company_id",
            "model_name",
            "model_year_start",
            name="uq_company_model_year",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    model_name: Mapped[str] = mapped_column(Text, nullable=False)
    model_year_start: Mapped[Optional[int]] = mapped_column(Integer)
    model_year_end: Mapped[Optional[int]] = mapped_column(Integer)
    production_volume_units: Mapped[Optional[int]] = mapped_column(Integer)
    production_volume_year: Mapped[Optional[int]] = mapped_column(Integer)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="true"
    )
    data_source: Mapped[Optional[str]] = mapped_column(String(128))
    metadata_json: Mapped[Optional[Any]] = mapped_column(JSONB)
    verified: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    company: Mapped["Company"] = relationship()
    chemistries: Mapped[list["VehicleModelChemistry"]] = relationship(
        back_populates="vehicle_model", cascade="all, delete-orphan"
    )


class VehicleModelChemistry(Base):
    """
    Time-windowed chemistry share for a single vehicle model.

    ``share_pct`` is in the open-closed range (0, 1]; within a model, shares
    must sum to 1.0 over the active validity window. ``valid_to`` NULL means
    "still in effect". Multiple rows per ``(model, chemistry)`` are how a
    chemistry refresh (e.g. NMC 622 -> NMC 811) is recorded — append a new row
    with ``valid_from`` = effective date and back-fill ``valid_to`` on the
    superseded row.
    """

    __tablename__ = "vehicle_model_chemistries"
    __table_args__ = (
        CheckConstraint(
            "share_pct > 0 AND share_pct <= 1",
            name="ck_vehicle_model_share_range",
        ),
        UniqueConstraint(
            "vehicle_model_id",
            "battery_chemistry_id",
            "valid_from",
            name="uq_vehicle_model_chem_window",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    vehicle_model_id: Mapped[int] = mapped_column(
        ForeignKey("company_vehicle_models.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    battery_chemistry_id: Mapped[int] = mapped_column(
        ForeignKey("battery_chemistries.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    share_pct: Mapped[float] = mapped_column(Float, nullable=False)
    valid_from: Mapped[date] = mapped_column(Date, nullable=False)
    valid_to: Mapped[Optional[date]] = mapped_column(Date)
    notes: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    vehicle_model: Mapped["CompanyVehicleModel"] = relationship(
        back_populates="chemistries"
    )
    chemistry: Mapped["BatteryChemistry"] = relationship()
