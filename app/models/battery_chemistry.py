"""Battery chemistry ORM models.

Three tables:
  battery_chemistries          — one row per chemistry (NMC, LFP, …)
  battery_chemistry_materials  — versioned junction: which materials each chemistry uses
                                  and at what intensity
  chemistry_risk_scores        — pre-computed append-only risk scores per chemistry
"""

from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING, Any, Optional

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.supply import Material


class BatteryChemistry(Base):
    """
    A battery cell chemistry (NMC, LFP, NCA, LFMP, sodium_ion, solid_state).

    NMC variants (111/622/811) are collapsed into the single 'nmc' slug for v1.
    Intensity values in battery_chemistry_materials use NMC 622 as a midpoint.
    NMC 811 will be split as a separate slug once temporal junction data warrants it.

    current_market_share_pct requires market_share_as_of_date — without the date,
    the number is uninterpretable (LFP went from ~6% to 40%+ of Chinese EV market
    in three years).
    """

    __tablename__ = "battery_chemistries"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False,
        comment="commercial | emerging | research",
    )
    current_market_share_pct: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True,
        comment="Approximate global share 0.0–1.0. See market_share_as_of_date.",
    )
    market_share_as_of_date: Mapped[Optional[date]] = mapped_column(
        Date, nullable=True,
        comment="Required when current_market_share_pct is set.",
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    verified: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    materials: Mapped[list["BatteryChemistryMaterial"]] = relationship(
        back_populates="chemistry", cascade="all, delete-orphan"
    )
    risk_scores: Mapped[list["ChemistryRiskScore"]] = relationship(
        back_populates="chemistry", cascade="all, delete-orphan"
    )


class BatteryChemistryMaterial(Base):
    """
    Versioned junction between battery_chemistries and materials.

    valid_from / valid_to enable point-in-time queries so chemistry risk scores
    are auditable as compositions change over time. Point-in-time selection:
        valid_from <= as_of_date <= COALESCE(valid_to, 'infinity')

    Example: NMC nickel intensity migrating from 0.60 (NMC 622, 2020) to 0.80
    (NMC 811, 2025) is captured by inserting a new row with valid_from='2025-01-01'
    and setting valid_to='2024-12-31' on the prior row.
    """

    __tablename__ = "battery_chemistry_materials"
    __table_args__ = (
        UniqueConstraint(
            "battery_chemistry_id", "material_id", "role", "valid_from",
            name="uq_chem_material_role_date",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    battery_chemistry_id: Mapped[int] = mapped_column(
        ForeignKey("battery_chemistries.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    material_id: Mapped[int] = mapped_column(
        ForeignKey("materials.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    role: Mapped[str] = mapped_column(
        String(64), nullable=False,
        comment="cathode_active | anode | electrolyte | current_collector | other",
    )
    intensity: Mapped[float] = mapped_column(
        Float, nullable=False,
        comment="0–1: relative material intensity. Higher = more critical to cell performance.",
    )
    is_substitutable: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false",
    )
    valid_from: Mapped[date] = mapped_column(Date, nullable=False)
    valid_to: Mapped[Optional[date]] = mapped_column(
        Date, nullable=True,
        comment="NULL = currently active.",
    )
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    chemistry: Mapped["BatteryChemistry"] = relationship(back_populates="materials")
    material: Mapped["Material"] = relationship(back_populates="chemistry_uses")


class ChemistryRiskScore(Base):
    """
    Pre-computed risk score for a battery chemistry. Append-only (one row per
    scoring run). Historical rows are preserved for trend analysis.

    score_confidence reflects data quality: penalised when materials have
    data_availability='no_benchmark'. A score backed solely by no_benchmark
    materials floors at 0.3.

    metadata_json structure (required for auditability):
    {
      "signal_sources":               {"Gallium": "manual", "Lithium": "usgs_mcs"},
      "geo_coverage":                 {"Lithium": 0.85, "Gallium": null},
      "materials_missing_hs":         ["Gallium", "Germanium"],
      "patent_modifiers_applied":     {"Gallium": 1.15, "Cobalt": 0.85},
      "trade_flows_vintage":          "2023",
      "no_benchmark_materials":       ["Gallium"],
      "score_confidence":             0.72,
      "trade_volatility_by_material": {"Lithium": 0.41, "Cobalt": 0.30},
      "trade_event_counts":           {"Lithium": 7, "Cobalt": 0}
    }

    ``trade_volatility_by_material`` records the per-material trade-volatility
    sub-input fed into ``score_material_exposure()``. Derived from the average
    normalised impact of GEOPOLITICAL_TRADE events tagged to the material via
    ``risk_event_materials``. When no events exist the value falls back to
    ``_DEFAULT_TRADE_VOLATILITY`` (0.3) so the absence of news reads as
    "neutral", not zero risk. ``trade_event_counts`` mirrors the per-material
    event counts so analysts can see why a value is at the neutral default.
    """

    __tablename__ = "chemistry_risk_scores"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    battery_chemistry_id: Mapped[int] = mapped_column(
        ForeignKey("battery_chemistries.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    as_of_date: Mapped[date] = mapped_column(
        Date, nullable=False,
        comment="Point-in-time date used to select active battery_chemistry_materials rows.",
    )
    methodology_version: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="1.0",
    )
    material_concentration_score: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True,
        comment="Intensity-weighted material concentration sub-score 0–100.",
    )
    geopolitical_score: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True,
        comment="Intensity-weighted geopolitical concentration sub-score 0–100.",
    )
    composite_risk_score: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True,
        comment="Blended risk score 0–100.",
    )
    score_confidence: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True,
        comment="0–1. Product of DATA_AVAILABILITY_CONFIDENCE factors. Floored at 0.3.",
    )
    metadata_json: Mapped[Optional[Any]] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    chemistry: Mapped["BatteryChemistry"] = relationship(back_populates="risk_scores")
