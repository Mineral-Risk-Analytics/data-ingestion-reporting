"""Physical facilities operated by companies, with a many-to-many junction."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any, Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.company import Company


class Facility(Base):
    """
    A physical location where supply chain activity occurs.

    Linked to companies via the ``company_facilities`` junction table so that
    JV or co-owned facilities (BlueOvalSK, Ultium Cells, etc.) can appear under
    multiple company detail views without duplicating the facility row.

    The global ``verified`` flag means: "this facility physically exists / the
    data is trustworthy." Per-company verification lives on CompanyFacility.verified.

    Facility-level data is sparse and often requires manual research or
    commercial data sources (Benchmark Mineral Intelligence, Wood Mackenzie, USGS).
    """

    __tablename__ = "facilities"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    facility_type: Mapped[str] = mapped_column(String(64), nullable=False)
    # mine | refinery | cell_factory | pack_plant | recycling | r_and_d | hq
    country: Mapped[str] = mapped_column(String(2), nullable=False, index=True)  # ISO2
    region: Mapped[Optional[str]] = mapped_column(String(128))
    city: Mapped[Optional[str]] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(64), nullable=False, default="operating")
    # operating | planned | under_construction | mothballed | closed
    capacity_notes: Mapped[Optional[str]] = mapped_column(Text)
    # free text, e.g. "50 GWh/yr planned 2026"
    latitude: Mapped[Optional[float]] = mapped_column(Float)
    longitude: Mapped[Optional[float]] = mapped_column(Float)
    data_source: Mapped[Optional[str]] = mapped_column(String(128))
    metadata_json: Mapped[Optional[Any]] = mapped_column(JSONB)
    verified: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    company_links: Mapped[list["CompanyFacility"]] = relationship(
        back_populates="facility", cascade="all, delete-orphan"
    )


class CompanyFacility(Base):
    """
    Junction table linking companies to their facilities.

    ``ownership_type`` characterises the relationship:
        operator        — the company runs the facility outright
        jv_partner      — joint-venture co-owner (use ownership_pct for stake)
        lessee          — long-term lease / offtake arrangement
        minority_stake  — financial stake, not operational control
        other           — catch-all

    ``ownership_pct`` is a 0.0–1.0 fraction of ownership/stake where known.
    NULL means the relationship is confirmed but the exact share is unknown.

    ``verified`` = an analyst has confirmed this company–facility link is real.
    """

    __tablename__ = "company_facilities"
    __table_args__ = (
        UniqueConstraint("company_id", "facility_id", name="uq_company_facility"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    facility_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("facilities.id", ondelete="CASCADE"), nullable=False, index=True
    )
    ownership_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default="operator"
    )
    ownership_pct: Mapped[Optional[float]] = mapped_column(Float)
    verified: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    company: Mapped["Company"] = relationship(back_populates="facility_links")
    facility: Mapped["Facility"] = relationship(back_populates="company_links")
