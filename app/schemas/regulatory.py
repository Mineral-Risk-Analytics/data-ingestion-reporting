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
    event_subtype: Optional[str] = None
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


# ---------------------------------------------------------------------------
# Per-material risk events tab — table rows + drawer + summary stats
# ---------------------------------------------------------------------------
#
# Powers the Risk events tab on the material detail page.  Three pieces:
#   1. ``MaterialRiskEventRow`` — one row in the events table.  Includes
#      derived fields the analyst wants without a follow-up fetch:
#      pillars_affected (decoded from risk_categories_json), source_system
#      (Source.name), geography_codes (joined RiskEventGeography ISO2s).
#   2. ``MaterialRiskEventDrawer`` — extends the row with source_url + the
#      raw summary so the row is enough for the table cells but the drawer
#      can show a fuller view without N+1.
#   3. ``MaterialRiskEventsSummary`` — pillar bar chart counts + signal
#      summary (total, high severity, verified).  All counts respect the
#      timeframe filter the caller passes through so the cards animate
#      cleanly when the window changes.


class MaterialRiskEventRow(BaseModel):
    """Single event row in the per-material Risk events tab."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    summary: Optional[str] = None
    event_type: str
    event_subtype: Optional[str] = None
    severity_score: Optional[float] = None
    confidence_score: Optional[float] = None
    event_date: Optional[datetime] = None
    verified: bool = False

    pillars_affected: list[str]
    """Decoded from RiskEvent.risk_categories_json — pillar slugs the UI
    can map to colour + label.  Multiple entries when the event affects
    more than one pillar (e.g. a sanctions designation that hits both
    regulatory_compliance and geopolitical_trade)."""

    source_system: Optional[str] = None
    """SourceDocument.source.name — federal_register | global_trade_alert
    | eurlex | iea | opensanctions | sec_edgar | etc."""

    source_url: Optional[str] = None
    """Direct URL on the originating source, for the drawer's View source
    button.  Null when the source_document predates URL capture."""

    geography_codes: list[str]
    """ISO2 country codes tagged to this event via RiskEventGeography.
    Empty when the event has no geography attribution (rare — material-
    only events from MCS or financial filings)."""


class MaterialRiskEventsSummary(BaseModel):
    """Top-of-tab signal summary cards.  All counts respect the current
    filter set so the cards update as the analyst narrows the view."""

    window_days: int
    """Window the counts cover, mirrored from the request for clear
    labelling in the UI (e.g. 'Trailing 365 days')."""

    total_events: int
    high_severity_count: int
    """Events with severity_score >= 0.75 within the window."""
    verified_count: int

    events_by_pillar: dict[str, int]
    """Pillar slug → event count for the bar-chart card.  An event tagged
    to multiple pillars contributes 1 to each (so sums can exceed
    total_events — that's the analyst-honest answer for 'how often does
    each pillar show up in this material's recent feed')."""


class MaterialRiskEventsResponse(BaseModel):
    """Aggregated response for GET /materials/{id}/risk-events."""

    summary: MaterialRiskEventsSummary
    events: list[MaterialRiskEventRow]
    total: int
    """Pre-pagination count — matches summary.total_events."""
    page: int
    limit: int
