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

Also defines the canonical ``SupplyChainStage`` reference table (migration
044) — distinct from ``SupplyChainContext`` despite the name overlap.
``SupplyChainContext`` is per-domain config; ``SupplyChainStage`` is the
normalised reference vocabulary that ``Company`` and (eventually)
``CompanyMaterialExposure`` FK into.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

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


class SupplyChainStage(Base):
    """Canonical ACTIVITY-stage taxonomy (Axis B, migration 044).

    The codebase carries TWO orthogonal "stage" axes:

      * Axis A — product form (ore | concentrate | intermediate | refined
        | battery_grade | fabricated | scrap).  Lives on
        ``hs_code_material_mappings.supply_chain_stage`` and
        ``facilities.supply_chain_stage``.  NOT this table.

      * Axis B — activity (mining | refining | cell_making | …).  THIS
        TABLE.

    ``typical_output_forms`` captures the many-to-many relationship from
    this table's stage_code to Axis A product forms — advisory only, used
    by partner-curation UI for cross-checks.

    Referenced by ``Company.primary_activity_stage_fk``,
    ``CompanyActivityStage.stage_code``, and (deferred migration)
    ``CompanyMaterialExposure.supply_chain_stage_fk``.  Seeded by
    ``app.services.ingestion.seed_supply_chain_stages``.
    """

    __tablename__ = "supply_chain_stages"

    stage_code: Mapped[str] = mapped_column(String(64), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    typical_facility_types: Mapped[Optional[Any]] = mapped_column(JSONB, nullable=True)
    typical_output_forms: Mapped[Optional[Any]] = mapped_column(JSONB, nullable=True)
    bottleneck_weight: Mapped[Decimal] = mapped_column(
        Numeric(precision=4, scale=2), nullable=False, server_default="1.0"
    )
    hs_chapter_hint: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class CompanyActivityStage(Base):
    """Join table: companies × supply_chain_stages (many-to-many, Axis B).

    Captures the multi-activity-stage case (Albemarle = mining + refining;
    Ganfeng = mining + refining + precursor_production).  The single
    ``Company`` column ``primary_activity_stage_fk`` carries the dominant
    activity stage; this table carries the full activity-stage list.
    """

    __tablename__ = "company_activity_stages"

    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("companies.id", ondelete="CASCADE"),
        primary_key=True,
    )
    stage_code: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("supply_chain_stages.stage_code", ondelete="RESTRICT"),
        primary_key=True,
        index=True,
    )
    is_primary: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class CompanyJvParent(Base):
    """Join table: jv_vehicle companies × parent companies (many-to-many).

    For JV vehicles like TLEA (Tianqi Lithium Energy Australia) that have
    multiple corporate parents, this table records each parent edge.
    Single-parent subsidiaries continue to use ``Company.parent_company_id``;
    this table is *additive* for the multi-parent case.
    """

    __tablename__ = "company_jv_parents"

    jv_vehicle_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("companies.id", ondelete="CASCADE"),
        primary_key=True,
    )
    parent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("companies.id", ondelete="CASCADE"),
        primary_key=True,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
