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

from sqlalchemy import Date, DateTime, Float, ForeignKey, Integer, String, func
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
