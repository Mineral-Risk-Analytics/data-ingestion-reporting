"""Material-level and geography-level aggregate risk scores.

These tables enable material-anchored and geography-anchored market intelligence
briefs without requiring any supplier/company rows. The query path for a graphite
brief is:

    materials → risk_event_materials → risk_events → event_impact scores
                                                           ↓
                                                  material_scores (aggregated)
                                                           ↓
                                                  report_insights
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Optional

from sqlalchemy import Date, DateTime, Float, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class MaterialScore(Base):
    """
    Aggregate risk score at the material level. Computed from all events tagged to
    that material via risk_event_materials, weighted by relevance_score.

    When company data exists, can also weight by company_exposures to incorporate
    company-level signals. company_count = 0 means the score is event-only.

    Append-only: one row per (material_id, as_of_date) scoring run. Historical rows
    preserved for trend analysis.
    """

    __tablename__ = "material_scores"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    material_id: Mapped[int] = mapped_column(
        ForeignKey("materials.id", ondelete="CASCADE"), nullable=False, index=True
    )
    as_of_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    material_concentration_score: Mapped[Optional[float]] = mapped_column(Float)  # 0–100
    geopolitical_trade_score: Mapped[Optional[float]] = mapped_column(Float)
    regulatory_compliance_score: Mapped[Optional[float]] = mapped_column(Float)
    operational_score: Mapped[Optional[float]] = mapped_column(Float)
    financial_pressure_score: Mapped[Optional[float]] = mapped_column(Float)
    overall_risk_score: Mapped[Optional[float]] = mapped_column(Float)
    company_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )  # 0 = event-only score; >0 = includes company-level aggregation
    event_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rationale_json: Mapped[Optional[Any]] = mapped_column(JSONB)
    scoring_version: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="2.0"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


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


class GeographyScore(Base):
    """
    Aggregate risk score at the geography (country) level. Aggregates company scores
    for companies with operations in that geography and event signals tagged to that
    geography via risk_event_geographies.

    geography_code is ISO2 (e.g. "CN", "CD", "CL"). No FK enforced —
    geography is treated as a string code throughout the schema, not a FK target.
    """

    __tablename__ = "geography_scores"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    geography_code: Mapped[str] = mapped_column(
        String(2), nullable=False, index=True
    )  # ISO2
    as_of_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    geopolitical_trade_score: Mapped[Optional[float]] = mapped_column(Float)
    regulatory_compliance_score: Mapped[Optional[float]] = mapped_column(Float)
    operational_score: Mapped[Optional[float]] = mapped_column(Float)
    overall_risk_score: Mapped[Optional[float]] = mapped_column(Float)
    company_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    event_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rationale_json: Mapped[Optional[Any]] = mapped_column(JSONB)
    scoring_version: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="2.0"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
