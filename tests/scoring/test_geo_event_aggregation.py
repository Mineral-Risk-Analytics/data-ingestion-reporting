"""4.2: max-based export/tariff/subsidy sub-input aggregation.

Regression for the weak-event dilution bug: under avg(), a low-severity
corroborating event (e.g. a sanctions-count stat) DRAGGED DOWN the
export-restriction exposure that a high-severity export ban had
established.  Under max(), the strongest active restriction defines the
sub-input and weaker events can never lower it.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from app.constants import RiskCategory
from app.models.regulatory import RiskEvent
from app.services.scoring.evidence_query import EventWithRelevance
from app.services.scoring.market_aggregator import (
    _avg_impact_normalised,
    _max_impact_normalised,
)

AS_OF = date(2026, 7, 23)


def _ew(severity: float, confidence: float, days_old: int = 30,
        relevance: float = 1.0) -> EventWithRelevance:
    from datetime import timedelta
    ev = RiskEvent(
        event_type="MANUAL",
        title="Test export restriction",
        event_date=datetime(2026, 7, 23, tzinfo=timezone.utc) - timedelta(days=days_old),
        severity_score=severity,
        confidence_score=confidence,
        risk_categories_json=["geopolitical_trade"],
    )
    return EventWithRelevance(event=ev, relevance_score=relevance)


class TestMaxImpactNormalised:
    def test_empty_returns_zero(self):
        assert _max_impact_normalised([], RiskCategory.GEOPOLITICAL_TRADE, AS_OF) == 0.0

    def test_weak_event_does_not_dilute(self):
        """The cobalt/CD regression: ban + weak stat must score the same
        as the ban alone."""
        ban = _ew(0.9, 0.85, days_old=300)
        stat = _ew(0.072, 0.85, days_old=10)
        alone = _max_impact_normalised([ban], RiskCategory.GEOPOLITICAL_TRADE, AS_OF)
        together = _max_impact_normalised([ban, stat], RiskCategory.GEOPOLITICAL_TRADE, AS_OF)
        assert together == alone
        assert alone > 0.3

    def test_avg_did_dilute(self):
        """Documents the old behaviour max() replaces: avg is strictly
        lower once a weak event joins a strong one."""
        ban = _ew(0.9, 0.85, days_old=300)
        stat = _ew(0.072, 0.85, days_old=10)
        avg_alone = _avg_impact_normalised([ban], RiskCategory.GEOPOLITICAL_TRADE, AS_OF)
        avg_together = _avg_impact_normalised([ban, stat], RiskCategory.GEOPOLITICAL_TRADE, AS_OF)
        assert avg_together < avg_alone

    def test_capped_at_one(self):
        events = [_ew(1.0, 1.0, days_old=1) for _ in range(3)]
        assert _max_impact_normalised(events, RiskCategory.GEOPOLITICAL_TRADE, AS_OF) <= 1.0

    def test_stronger_event_raises_score(self):
        weak = _ew(0.3, 0.9, days_old=30)
        strong = _ew(0.9, 0.9, days_old=30)
        lo = _max_impact_normalised([weak], RiskCategory.GEOPOLITICAL_TRADE, AS_OF)
        hi = _max_impact_normalised([weak, strong], RiskCategory.GEOPOLITICAL_TRADE, AS_OF)
        assert hi > lo
