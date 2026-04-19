"""Domain configuration table for supply chain contexts.

One row per domain (e.g. EV battery, industrial metals). Externalises constants
that were previously hardcoded in Python scoring code so the platform is not
locked to battery-specific logic at the schema level.

Columns
-------
slug                    Short identifier used in API paths and code references
                        (e.g. "ev_battery"). Unique.
name                    Human-readable display name.
description             Optional prose description of the domain.
default_pillar_weights  JSONB dict of five pillar weights that sum to 1.0:
                            {"material_concentration": 0.30,
                             "geopolitical_trade":     0.20,
                             "regulatory":             0.20,
                             "operational":            0.15,
                             "financial":              0.15}
high_concentration_geos JSONB list of ISO2 country codes treated as
                        high-concentration geographies for the material
                        concentration pillar (e.g. ["CN", "CD", "RU"]).
relevant_hs_code_prefixes
                        JSONB list of HS code prefix strings used by the
                        ingestion pipeline to identify relevant trade flows
                        (e.g. ["8507", "2825", "2836"]).
supply_chain_stages     JSONB list of valid supply_chain_stage enum values
                        for companies in this domain
                        (e.g. ["miner", "refiner", "cell_maker", ...]).
is_active               When false the context is archived and excluded from
                        active scoring runs.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import Boolean, DateTime, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class SupplyChainContext(Base):
    __tablename__ = "supply_chain_contexts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    slug: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Scoring configuration
    default_pillar_weights: Mapped[Any] = mapped_column(JSONB, nullable=False)
    high_concentration_geos: Mapped[Any] = mapped_column(JSONB, nullable=False)
    relevant_hs_code_prefixes: Mapped[Any] = mapped_column(JSONB, nullable=False)
    supply_chain_stages: Mapped[Any] = mapped_column(JSONB, nullable=False)

    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="true"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
