"""MaterialCriticalitySignal — authoritative timeseries for per-material criticality.

This is the source of truth for criticality scores, replacing the single static
``Material.criticality_score`` float for multi-source, multi-year tracking.

``Material.criticality_score`` is retained as a convenience column (updated by
``ingest-usgs`` to reflect the latest USGS-derived value) but ``material_criticality_signals``
is what the chemistry risk scorer reads.

``Material.patent_occurrence_trend`` is a denormalized cache of the most recent
``trend_direction`` for that material. It is kept in sync by
``_sync_patent_trend()`` in ``app/services/scoring/chemistry_risk.py``.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any, Optional

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.supply import Material


class MaterialCriticalitySignal(Base):
    """
    One row per (material, source, reference_year). Captures criticality scores
    from multiple independent sources so the chemistry risk scorer can prefer
    more authoritative signals (eu_crma, iea_report) over baseline USGS data.

    Source hierarchy used by chemistry_risk.score_chemistry_from_rollup():
        1. eu_crma      — EU Critical Raw Materials Act assessments
        2. iea_report   — IEA Critical Minerals annual reports
        3. usgs_mcs     — USGS Mineral Commodity Summaries (default baseline)
        4. manual       — hand-entered data for materials without other sources
        5. patstat      — EPO PATSTAT (future phase)

    hhi_score stores the raw Herfindahl-Hirschman Index (Σ share_i²) in 0–1
    range (this codebase uses fractional shares, not percentages). The traditional
    0–10 000 scale uses percentage shares; multiply by 10 000 to convert.
    """

    __tablename__ = "material_criticality_signals"
    __table_args__ = (
        UniqueConstraint(
            "material_id", "source", "reference_year",
            name="uq_criticality_signal",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    material_id: Mapped[int] = mapped_column(
        ForeignKey("materials.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    source: Mapped[str] = mapped_column(
        String(32), nullable=False,
        comment="usgs_mcs | eu_crma | iea_report | patstat | manual",
    )
    reference_year: Mapped[int] = mapped_column(
        Integer, nullable=False,
        comment="Calendar year this signal reflects.",
    )
    criticality_score: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True,
        comment="Normalised criticality 0.0–1.0. For usgs_mcs equals normalised HHI.",
    )
    trend_direction: Mapped[Optional[str]] = mapped_column(
        String(16), nullable=True,
        comment="rising | declining | stable. Written to materials.patent_occurrence_trend "
                "by _sync_patent_trend() after any write to this table.",
    )
    hhi_score: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True,
        comment="Raw HHI 0.0–1.0 (Σ share_i²) on mine production. Stored for methodological transparency.",
    )
    # ── Supply metrics promoted from USGS MCS CSV (migration 019) ─────────────
    reserve_hhi_score: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True,
        comment=(
            "HHI of reserve distribution by country (0–1). Forward-looking "
            "concentration signal independent of production HHI."
        ),
    )
    reserve_life_index: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True,
        comment=(
            "World reserves / world annual production (years). "
            "Lower = nearer-term scarcity risk."
        ),
    )
    production_yoy_pct: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True,
        comment=(
            "YoY change in world mine production as a signed fraction "
            "(e.g. -0.05 = -5%). Negative = contracting supply."
        ),
    )
    capacity_utilization: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True,
        comment=(
            "World mine production / world mine capacity (0–1). "
            "High = tight market with little buffer."
        ),
    )
    # ── Price-trend metrics from USGS MCS 2026 Fig 10 (migration 036) ─────────
    price_yoy_pct: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True,
        comment=(
            "YoY price change as signed fraction (e.g. 1.44 = +144%). "
            "Positive = supply-side stress; negative = market easing. "
            "Sourced from USGS MCS Fig 10 Price Growth Rates."
        ),
    )
    price_cagr_5yr_pct: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True,
        comment=(
            "5-year compound annual growth rate of price as signed fraction "
            "(e.g. 0.47 = +47% CAGR). Smoother trend signal than yoy."
        ),
    )
    # ── US-dependency metrics promoted from metadata_json (migration 038) ─────
    us_net_import_reliance_pct: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True,
        comment=(
            "USGS Net Import Reliance percentage (0–100). Bounded "
            "estimates ('<50', '>50') stored as midpoint values."
        ),
    )
    us_apparent_consumption: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True,
        comment=(
            "US apparent consumption volume (latest year, source unit). "
            "Sourced from MCS Salient Statistics."
        ),
    )
    metadata_json: Mapped[Optional[Any]] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    material: Mapped["Material"] = relationship(back_populates="criticality_signals")
