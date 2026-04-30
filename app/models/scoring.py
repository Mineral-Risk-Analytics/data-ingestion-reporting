"""Material-level aggregate risk scores (v3.0 scoring pipeline).

Three active tables in the scoring hierarchy:

    material_geography_risk_scores  — per (material, geography), five pillars
              ↓  trade-flow weighted avg
    material_global_risk_scores     — per material, global rollup
              ↓  intensity-weighted avg across minerals
    chemistry_risk_scores           — per battery chemistry

Legacy tables ``material_scores`` and ``geography_scores`` were dropped in
migration 021_drop_legacy_scores. See docs/deprecation-audit.md §A1, §A2.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Optional

from sqlalchemy import Date, DateTime, Float, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class MaterialGeographyRiskScore(Base):
    """
    Risk score at the material × geography intersection.

    This is the PRIMARY output table of the market intelligence layer.
    Scores are computed without any company data — purely from risk events
    tagged via risk_event_materials and risk_event_geographies, combined
    with MaterialCriticalitySignal for the criticality sub-input.

    Financial pressure is KEPT but reframed with market-level inputs:
    commodity price volatility, directional price trend, and producer stress
    events — instead of company filing signals. Supply-chain propagation is
    excluded (requires a company graph). The five active pillar weights are
    renormalised to sum to 1.0; see market_aggregator.MARKET_PILLAR_WEIGHTS.

    Append-only: one row per (material_id, geography_code, as_of_date).
    Historical rows preserved for trend analysis and intelligence hub display.

    Use cases:
      - "What is the current geopolitical risk for lithium from Chile?"
      - "How has cobalt-DRC regulatory risk moved over 12 months?"
      - Company overlay: blends this market score with customer exposure config.
    """

    __tablename__ = "material_geography_risk_scores"
    __table_args__ = (
        UniqueConstraint(
            "material_id", "geography_code", "as_of_date",
            name="uq_mat_geo_risk_score",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    material_id: Mapped[int] = mapped_column(
        ForeignKey("materials.id", ondelete="CASCADE"), nullable=False, index=True
    )
    geography_code: Mapped[str] = mapped_column(
        String(2), nullable=False, index=True,
        comment="ISO 3166-1 alpha-2, e.g. CN, CD, CL",
    )
    as_of_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)

    # ---- five active pillars (propagation excluded — requires company graph) ----
    material_concentration_score: Mapped[Optional[float]] = mapped_column(
        Float, comment="0–100, material-level criticality × concentration sub-inputs"
    )
    geopolitical_trade_score: Mapped[Optional[float]] = mapped_column(
        Float, comment="0–100, events + geography HCG status"
    )
    regulatory_compliance_score: Mapped[Optional[float]] = mapped_column(
        Float, comment="0–100, regulations scoped to this material + geography"
    )
    operational_score: Mapped[Optional[float]] = mapped_column(
        Float, comment="0–100, operational events for this material/geography pair"
    )
    financial_pressure_score: Mapped[Optional[float]] = mapped_column(
        Float,
        comment=(
            "0–100, reframed for market level: commodity price volatility "
            "(base signal) + price spike events (leverage proxy) + producer "
            "stress events / price crash (liquidity proxy). "
            "No company filing data used."
        ),
    )

    # ---- overall ----
    overall_risk_score: Mapped[Optional[float]] = mapped_column(
        Float, comment="0–100, renormalised weighted average of five active pillars"
    )

    # ---- metadata ----
    event_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0,
        comment="Total events consumed in this scoring run"
    )
    rationale_json: Mapped[Optional[Any]] = mapped_column(
        JSONB, comment="Sub-inputs, top evidence IDs, criticality signal source, weights used"
    )
    scoring_version: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="3.0"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class MaterialGlobalRiskScore(Base):
    """
    Trade-flow-weighted rollup of MaterialGeographyRiskScore rows into a single
    global risk view per material.

    Sits one level above the geo scores and one level below ChemistryRiskScore:

        material_geography_risk_scores  (per-geo, five pillars)
                  ↓  trade-flow weighted avg across geographies
        material_global_risk_scores     (this table — one row per material per date)
                  ↓  intensity-weighted avg across minerals
        chemistry_risk_scores           (per chemistry, five pillars)

    Unique on (material_id, as_of_date) — one row per scoring run.
    Append-only: historical rows preserved for trend analysis.

    trade_weighted_geo_count:
        How many geographies contributed to the weighted average. Shown in
        the UI as a data coverage indicator — a global score built from only
        two geographies is less reliable than one built from seven.

    total_trade_value_usd:
        The sum of trade_value_usd values used as the denominator. NULL when
        the weights came from MaterialProductionShare or equal weighting
        (i.e. TradeFlow had no coverage for this material).
    """

    __tablename__ = "material_global_risk_scores"
    __table_args__ = (
        UniqueConstraint(
            "material_id", "as_of_date",
            name="uq_material_global_risk_score",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    material_id: Mapped[int] = mapped_column(
        ForeignKey("materials.id", ondelete="CASCADE"), nullable=False, index=True
    )
    as_of_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)

    # ---- five active pillars ------------------------------------------------
    material_concentration_score: Mapped[Optional[float]] = mapped_column(
        Float, comment="0–100, trade-flow-weighted avg across geographies"
    )
    geopolitical_trade_score: Mapped[Optional[float]] = mapped_column(
        Float, comment="0–100, trade-flow-weighted avg across geographies"
    )
    regulatory_compliance_score: Mapped[Optional[float]] = mapped_column(
        Float, comment="0–100, trade-flow-weighted avg across geographies"
    )
    operational_score: Mapped[Optional[float]] = mapped_column(
        Float, comment="0–100, trade-flow-weighted avg across geographies"
    )
    financial_pressure_score: Mapped[Optional[float]] = mapped_column(
        Float, comment="0–100, trade-flow-weighted avg across geographies"
    )

    # ---- overall ------------------------------------------------------------
    overall_risk_score: Mapped[Optional[float]] = mapped_column(
        Float, comment="0–100, weighted average of five pillars using MARKET_PILLAR_WEIGHTS"
    )

    # ---- weight auditability ------------------------------------------------
    trade_weighted_geo_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0,
        comment="Number of geographies that contributed a non-zero weight"
    )
    total_trade_value_usd: Mapped[Optional[float]] = mapped_column(
        Float,
        comment=(
            "Sum of trade_value_usd used as the weighting denominator. "
            "NULL when production shares or equal weights were used instead."
        ),
    )

    # ---- metadata -----------------------------------------------------------
    rationale_json: Mapped[Optional[Any]] = mapped_column(
        JSONB,
        comment="Per-geography weights, weight source (trade/production/equal), pillar breakdown"
    )
    scoring_version: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="1.0"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


