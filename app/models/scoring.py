"""Material-level aggregate risk scores (v3.0 scoring pipeline).

Scoring hierarchy after the HS code redesign (migrations 022–027):

    hs_code_geography_risk_scores   — per (hs_mapping, geography) — Level 0 (NEW)
              ↓  stage-weighted rollup (STAGE_ROLLUP_WEIGHTS)
    material_geography_risk_scores  — per (material, geography), five pillars — Level 1
              ↓  trade-flow weighted avg
    material_global_risk_scores     — per material, global rollup — Level 2
              ↓  intensity-weighted avg across minerals
    chemistry_risk_scores           — per battery chemistry — Level 3

Legacy tables ``material_scores`` and ``geography_scores`` were dropped in
migration 021_drop_legacy_scores. See docs/deprecation-audit.md §A1, §A2.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING, Any, Optional

from sqlalchemy import (
    Date, DateTime, Float, ForeignKey, Integer,
    SmallInteger, String, UniqueConstraint, func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.supply import HsCodeMaterialMapping


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
        comment=(
            "Total events consumed in this scoring run — the UNION of "
            "RiskEventMaterial (this material, any country) and "
            "RiskEventGeography (this country, any material) consumed by "
            "the pillar sub-input derivation.  Preserves the audit trail "
            "of what was fed into the score.  For the UI 'this country has "
            "exposure to this material' read use ``event_count_geo_specific`` "
            "instead — see migration 042 docstring."
        ),
    )
    event_count_geo_specific: Mapped[Optional[int]] = mapped_column(
        Integer,
        nullable=True,
        default=None,
        comment=(
            "Count of events at the INTERSECTION of RiskEventMaterial "
            "and RiskEventGeography for this (material, country) pair, "
            "within each category's lookback window.  This is what the "
            "UI 'Events' column displays and what country-exposure "
            "filters key off.  NULL on rows produced before migration "
            "042 — re-run POST /market/rescore to populate."
        ),
    )
    rationale_json: Mapped[Optional[Any]] = mapped_column(
        JSONB, comment="Sub-inputs, top evidence IDs, criticality signal source, weights used"
    )
    scoring_version: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="3.0"
    )
    # ---- stage rollup annotations (added migration 025) ----------------------
    stage_rollup_count: Mapped[Optional[int]] = mapped_column(
        SmallInteger,
        nullable=True,
        comment=(
            "Number of HS stage nodes that contributed to the Material Concentration "
            "pillar rollup.  NULL for rows produced before the Phase-3 scoring engine."
        ),
    )
    stage_rollup_method: Mapped[Optional[str]] = mapped_column(
        String(32),
        nullable=True,
        comment=(
            "'stage_weighted' = computed from hs_code_geography_risk_scores; "
            "'material_fallback' = fewer than 2 stage nodes available, fell back "
            "to MaterialCriticalitySignal.hhi_score.  NULL for pre-redesign rows."
        ),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class HsCodeGeographyRiskScore(Base):
    """
    Risk score at the HS code mapping node × geography intersection.
    Level 0 of the scoring stack — the most granular persisted score.

    One row per (hs_mapping_id, country_code, as_of_date, market_scope).
    These roll up into material_geography_risk_scores (Level 1) via
    STAGE_ROLLUP_WEIGHTS in market_aggregator.py.

    Sub-scores:
      production_share   — this country's fraction of world production for this stage node
      hhi_at_stage       — Σ(share²) across all countries for this HS node and year
      tariff_exposure    — 0–1, from tariff events scoped to this HS code
      export_restriction — 0–1, from export restriction events scoped to this HS code
      composite_node_score — 0–100, weighted combination

    Append-only: historical rows preserved for trend analysis.
    methodology_version must be stable across all persisted rows — see
    docs/hs-code-redesign.md § Decision 7.
    """

    __tablename__ = "hs_code_geography_risk_scores"
    __table_args__ = (
        UniqueConstraint(
            "hs_mapping_id", "country_code", "as_of_date", "market_scope",
            name="uq_hs_geo_score",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    hs_mapping_id: Mapped[int] = mapped_column(
        ForeignKey("hs_code_material_mappings.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    country_code: Mapped[str] = mapped_column(
        String(2), nullable=False, index=True,
        comment="ISO 3166-1 alpha-2 country code",
    )
    as_of_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)

    # ---- sub-scores (raw 0–1 except composite which is 0–100) ----------------
    production_share: Mapped[Optional[float]] = mapped_column(
        Float,
        comment="This country's share of world production for this HS stage node",
    )
    hhi_at_stage: Mapped[Optional[float]] = mapped_column(
        Float,
        comment="Σ(production_share²) across all countries for this HS node and year",
    )
    tariff_exposure: Mapped[Optional[float]] = mapped_column(
        Float,
        comment="0–1, derived from tariff risk events scoped to this HS code",
    )
    export_restriction: Mapped[Optional[float]] = mapped_column(
        Float,
        comment="0–1, derived from export restriction events scoped to this HS code",
    )
    composite_node_score: Mapped[Optional[float]] = mapped_column(
        Float,
        comment="0–100, weighted combination of sub-scores for this node",
    )

    # ---- metadata ------------------------------------------------------------
    market_scope: Mapped[str] = mapped_column(
        String(8), nullable=False, server_default="global",
        comment="Inherited from hs_code_material_mappings.market_scope",
    )
    methodology_version: Mapped[str] = mapped_column(
        String(8), nullable=False, server_default="1.0",
        comment="Must be stable across all persisted rows — see hs-code-redesign.md § Decision 7",
    )
    metadata_json: Mapped[Optional[Any]] = mapped_column(
        JSONB,
        comment="Event IDs consumed, weight breakdown, data coverage notes",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # Relationship — lets the market aggregator's stage-weighted rollup
    # access ``n.hs_mapping.supply_chain_stage`` and ``stage_sequence``
    # without a manual lookup.  Added 2026-05-09 (was missing — the bug
    # was masked until G6 threshold lowered to 1 caused the rollup path
    # to fire in production for the first time).  ``lazy="joined"`` is
    # OK here because every consumer already needs the mapping; no risk
    # of accidentally fetching mappings we won't read.
    hs_mapping: Mapped["HsCodeMaterialMapping"] = relationship(
        "HsCodeMaterialMapping", lazy="joined",
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


