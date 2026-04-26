"""Pydantic schemas for the market intelligence layer (material × geography).

Read models for ``MaterialGeographyRiskScore`` rows produced by
``app.services.scoring.market_aggregator``. These scores are company-agnostic
and feed both the Mineral Risk Analytics public hub (Phase 4) and the
internal admin browser.

The list view (``MaterialGeographyScoreRead``) deliberately omits
``rationale_json`` because it can be large (sub-input dicts, top-evidence IDs,
weight blocks). The detail view (``MaterialGeographyScoreDetail``) includes
the full rationale so the UI can render the evidence trail.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict


class MaterialGeographyScoreRead(BaseModel):
    """List/browse view — rationale omitted to keep payloads small."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    material_id: int
    geography_code: str
    as_of_date: date

    material_concentration_score: Optional[float] = None
    geopolitical_trade_score: Optional[float] = None
    regulatory_compliance_score: Optional[float] = None
    operational_score: Optional[float] = None
    financial_pressure_score: Optional[float] = None
    overall_risk_score: Optional[float] = None

    event_count: int = 0
    scoring_version: str
    created_at: datetime


class MaterialGeographyScoreDetail(MaterialGeographyScoreRead):
    """Single-pair view — full rationale included for evidence display."""

    rationale_json: Optional[dict[str, Any]] = None


class RescoredResult(BaseModel):
    """Response payload for ``POST /market/rescore``."""

    scored: int
    as_of_date: date
    run_id: str
