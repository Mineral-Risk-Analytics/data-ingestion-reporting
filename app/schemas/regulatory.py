from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Union

from pydantic import BaseModel, ConfigDict, computed_field


class RegulationMaterialScopeRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    material_id: int
    scope_type: str
    notes: Optional[str] = None


class RegulationGeographyScopeRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    country_code: str
    scope_type: str


class RegulationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    source_document_id: Optional[int] = None
    regulation_key: str
    title: Optional[str] = None
    issuing_body: Optional[str] = None
    geography: Optional[str] = None
    policy_theme: Optional[str] = None
    status: Optional[str] = None
    publication_date: Optional[date] = None
    effective_date: Optional[date] = None
    summary: Optional[str] = None
    metadata_json: Optional[Dict[str, Any]] = None
    verified: bool = False
    created_at: datetime
    updated_at: datetime

    # ── Scoring-impact fields (computed) ────────────────────────────────
    # Surfaced so the regulation detail page can display the exact scoring
    # contribution this regulation produces, without the frontend having
    # to mirror Python constants.  Source-of-truth lookups:
    #   compliance_uplift_points → app.services.scoring.regulatory_risk
    #                              .COMPLIANCE_OBLIGATIONS
    #   status_severity_weight   → app.services.ingestion.eurlex
    #                              .SEVERITY_BY_STATUS
    #   proximity_window_active  → date math against effective_date
    #                              (decay.compute_recency_multiplier
    #                              applies a 1.10–1.20 step-up when True)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def compliance_uplift_points(self) -> int:
        """Per non-compliant company uplift points this regulation adds to
        the regulatory pillar at Level 4 (company scoring).  Multiplied by
        the company's compliance_status weight (non_compliant=1.00,
        unknown=0.50, partial=0.40, compliant=0.0) before being summed
        with other obligations and capped at 40.
        """
        # Late import to avoid circular: services.scoring imports schemas.
        from app.services.scoring.regulatory_risk import COMPLIANCE_OBLIGATIONS
        return COMPLIANCE_OBLIGATIONS.get(self.regulation_key, 0)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def status_severity_weight(self) -> float:
        """Per-event severity weight applied to every RiskEvent created
        for this regulation.  Drives the Level-1 regulatory pillar
        (event_score = avg(top-3 impacts) * proximity * 60).
        """
        from app.services.ingestion.eurlex import status_severity_weight
        return status_severity_weight(self.status)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def proximity_window_active(self) -> bool:
        """True when today is within ±90 days of effective_date.  When
        True, decay.compute_recency_multiplier returns 1.10–1.20 instead
        of 1.00, boosting the regulatory pillar score for this regulation.
        Recomputed at request time — flips automatically as time passes.
        """
        if self.effective_date is None:
            return False
        delta = abs((datetime.now().date() - self.effective_date).days)
        return delta <= 90


class RegulationDetail(RegulationRead):
    """Extends RegulationRead with joined scope tables."""

    material_scopes: List[RegulationMaterialScopeRead] = []
    geography_scopes: List[RegulationGeographyScopeRead] = []


class RiskEventRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    source_document_id: Optional[int] = None
    event_type: str
    event_date: Optional[datetime] = None
    title: str
    summary: Optional[str] = None
    severity_score: Optional[float] = None
    confidence_score: Optional[float] = None
    risk_categories_json: Optional[Union[List[Any], Dict[str, Any]]] = None
    geography_json: Optional[Union[Dict[str, Any], List[Any]]] = None
    metadata_json: Optional[Dict[str, Any]] = None
    verified: bool = False
    created_at: datetime
