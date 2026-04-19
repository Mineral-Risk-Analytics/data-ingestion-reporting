"""Tests for scope-regulation merging and propagation input shaping.

Covers:

* ``derive_regulatory_inputs`` UNION (max-weight) merge of structured
  exposures + scope-derived obligations.
* Regulation-tagged event widening + dedup.
* ``policy_proximity_adjustment`` triggers when an event has an
  ``effective_date`` within 90 days of ``as_of_date``.
* ``derive_propagation_inputs`` skips suppliers without a persisted score and
  substitutes the conservative volume default for unknown shares.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.services.scoring.evidence_aggregator import (
    _PROPAGATION_DEFAULT_VOLUME_SHARE,
    derive_propagation_inputs,
    derive_regulatory_inputs,
)
from app.services.scoring.evidence_query import EventWithRelevance, SupplierEdge


AS_OF = date(2025, 6, 1)


def _event(
    event_id: int,
    title: str = "evt",
    severity: float = 0.5,
    confidence: float = 0.9,
    days_ago: int = 7,
    effective_date: date | None = None,
) -> EventWithRelevance:
    metadata: dict = {}
    if effective_date is not None:
        metadata["effective_date"] = effective_date.isoformat()
    # Event date is relative to AS_OF (2025-06-01) so the recency multiplier
    # in compute_event_impact stays inside [0.60, 1.20].
    ev_dt = datetime.combine(AS_OF - timedelta(days=days_ago), datetime.min.time(), timezone.utc)
    ev = SimpleNamespace(
        id=event_id,
        title=title,
        severity_score=severity,
        confidence_score=confidence,
        metadata_json=metadata,
        event_date=ev_dt,
    )
    return EventWithRelevance(event=ev, relevance_score=1.0)


# ---------------------------------------------------------------------------
# derive_regulatory_inputs
# ---------------------------------------------------------------------------

class TestRegulatoryScopeMerge:
    def test_scope_only_obligation_passes_through(self):
        _, obligations, _ = derive_regulatory_inputs(
            regulatory_events=[],
            active_obligations=[],
            as_of_date=AS_OF,
            scope_obligations=[("LI-RULE", 0.5)],
        )
        assert obligations == [("LI-RULE", 0.5)]

    def test_max_weight_wins_on_collision(self):
        """Structured non_compliant (1.0) must beat scope-only (0.5)."""
        _, obligations, _ = derive_regulatory_inputs(
            regulatory_events=[],
            active_obligations=[("UFLPA", 1.0)],
            as_of_date=AS_OF,
            scope_obligations=[("UFLPA", 0.5)],
        )
        assert obligations == [("UFLPA", 1.0)]

    def test_disjoint_keys_are_unioned(self):
        _, obligations, _ = derive_regulatory_inputs(
            regulatory_events=[],
            active_obligations=[("UFLPA", 1.0)],
            as_of_date=AS_OF,
            scope_obligations=[("LI-RULE", 0.5), ("CN-RULE", 0.5)],
        )
        assert dict(obligations) == {"UFLPA": 1.0, "LI-RULE": 0.5, "CN-RULE": 0.5}

    def test_regulation_events_widen_pool(self):
        ev = _event(event_id=10, title="UFLPA enforcement notice")
        impacts_company, _, _ = derive_regulatory_inputs(
            regulatory_events=[ev],
            active_obligations=[],
            as_of_date=AS_OF,
        )
        impacts_reg, _, _ = derive_regulatory_inputs(
            regulatory_events=[],
            active_obligations=[],
            as_of_date=AS_OF,
            regulation_events=[ev],
        )
        # Same event, same number of impacts whether sourced via company or
        # regulation linkage.
        assert len(impacts_company) == len(impacts_reg) == 1

    def test_dedup_event_in_both_sources(self):
        ev = _event(event_id=10, title="UFLPA enforcement notice")
        impacts, _, _ = derive_regulatory_inputs(
            regulatory_events=[ev],
            active_obligations=[],
            as_of_date=AS_OF,
            regulation_events=[ev],
        )
        assert len(impacts) == 1

    def test_policy_proximity_triggers_within_90_days(self):
        ev = _event(
            event_id=11,
            title="EU Battery Regulation phase-in",
            effective_date=date(2025, 7, 1),  # +30 days from AS_OF
        )
        _, _, adj = derive_regulatory_inputs(
            regulatory_events=[ev],
            active_obligations=[],
            as_of_date=AS_OF,
        )
        assert adj == pytest.approx(1.15)

    def test_policy_proximity_not_triggered_far_future(self):
        ev = _event(
            event_id=12,
            title="Future rule",
            effective_date=date(2026, 6, 1),  # +1 year
        )
        _, _, adj = derive_regulatory_inputs(
            regulatory_events=[ev],
            active_obligations=[],
            as_of_date=AS_OF,
        )
        assert adj == pytest.approx(1.0)

    def test_policy_proximity_not_triggered_in_past(self):
        ev = _event(
            event_id=13,
            title="Past rule",
            effective_date=date(2024, 1, 1),
        )
        _, _, adj = derive_regulatory_inputs(
            regulatory_events=[ev],
            active_obligations=[],
            as_of_date=AS_OF,
        )
        assert adj == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# derive_propagation_inputs
# ---------------------------------------------------------------------------

def _supplier_score(score: float):
    return SimpleNamespace(overall_risk_score=score)


def _edge(supplier_id: uuid.UUID, depth: int, share: float | None) -> SupplierEdge:
    return SupplierEdge(
        supplier_id=supplier_id,
        depth=depth,
        cumulative_volume_share=share,
        path=[supplier_id],
    )


class TestDerivePropagationInputs:
    def test_empty_inputs_returns_empty(self):
        assert derive_propagation_inputs([], {}) == []

    def test_supplier_without_score_is_skipped(self):
        sup = uuid.uuid4()
        out = derive_propagation_inputs(
            [_edge(sup, depth=1, share=0.5)],
            latest_scores={},
        )
        assert out == []

    def test_supplier_with_none_score_is_skipped(self):
        sup = uuid.uuid4()
        out = derive_propagation_inputs(
            [_edge(sup, depth=1, share=0.5)],
            latest_scores={sup: _supplier_score(None)},
        )
        assert out == []

    def test_known_share_passes_through(self):
        sup = uuid.uuid4()
        out = derive_propagation_inputs(
            [_edge(sup, depth=1, share=0.4)],
            latest_scores={sup: _supplier_score(75.0)},
        )
        assert out == [(75.0, 0.4, 1)]

    def test_unknown_share_uses_default(self):
        sup = uuid.uuid4()
        out = derive_propagation_inputs(
            [_edge(sup, depth=2, share=None)],
            latest_scores={sup: _supplier_score(50.0)},
        )
        assert out == [(50.0, _PROPAGATION_DEFAULT_VOLUME_SHARE, 2)]

    def test_multi_supplier_chain(self):
        s1, s2, s3 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        chain = [
            _edge(s1, depth=1, share=0.5),
            _edge(s2, depth=2, share=None),
            _edge(s3, depth=2, share=0.1),
        ]
        scores = {
            s1: _supplier_score(80.0),
            s2: _supplier_score(40.0),
            s3: _supplier_score(60.0),
        }
        out = derive_propagation_inputs(chain, scores)
        assert (80.0, 0.5, 1) in out
        assert (40.0, _PROPAGATION_DEFAULT_VOLUME_SHARE, 2) in out
        assert (60.0, 0.1, 2) in out
        assert len(out) == 3
