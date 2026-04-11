"""Physical facilities operated by companies."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any, Optional

from sqlalchemy import DateTime, Float, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.company import Company


class Facility(Base):
    """
    A physical location where supply chain activity occurs. Linking companies to
    facilities enables geographic risk analysis beyond headquarters country — a
    company HQ'd in South Korea may have 80% of production capacity in China.
    Facility-level data is sparse and often requires manual research or commercial
    data sources (Benchmark Mineral Intelligence, Wood Mackenzie, USGS).
    """

    __tablename__ = "facilities"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
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
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    company: Mapped["Company"] = relationship(back_populates="facilities")
