"""Unit tests for app.services.review.reviewers.company._score_link."""

from __future__ import annotations

from types import SimpleNamespace

from app.services.review.reviewers.company import _score_link


def _event(
    *,
    severity: float | None = None,
    subtype: str | None = None,
) -> SimpleNamespace:
    metadata = {"event_subtype": subtype} if subtype is not None else {}
    return SimpleNamespace(
        id=1,
        severity_score=severity,
        metadata_json=metadata,
        title="event",
        event_date=None,
        event_type="REGULATORY_COMPLIANCE",
        source_document_id=None,
    )


def _link(*, relevance_score: float | None = 1.0) -> SimpleNamespace:
    return SimpleNamespace(
        risk_event_id=1,
        company_id="c1",
        relevance_score=relevance_score,
        match_reason="named_company",
    )


class TestScoreLink:
    def test_high_severity_contributes_two(self) -> None:
        score, signals = _score_link(_link(), _event(severity=0.85))
        assert signals["severity"]["points"] == 2
        assert score == 3  # severity(2) + confidence(1) at default relevance=1.0

    def test_medium_severity_contributes_one(self) -> None:
        score, signals = _score_link(_link(), _event(severity=0.50))
        assert signals["severity"]["points"] == 1

    def test_low_severity_contributes_zero(self) -> None:
        score, signals = _score_link(_link(), _event(severity=0.20))
        assert signals["severity"]["points"] == 0

    def test_none_severity_contributes_zero(self) -> None:
        score, signals = _score_link(_link(), _event(severity=None))
        assert signals["severity"]["points"] == 0

    def test_high_impact_subtype_contributes_one(self) -> None:
        score, signals = _score_link(
            _link(), _event(severity=0.20, subtype="SANCTION_ADD")
        )
        assert signals["event_subtype"]["points"] == 1

    def test_nonhigh_subtype_contributes_zero(self) -> None:
        score, signals = _score_link(
            _link(), _event(severity=0.20, subtype="TARIFF")
        )
        assert signals["event_subtype"]["points"] == 0

    def test_confidence_gate_at_080(self) -> None:
        _s1, signals_hi = _score_link(
            _link(relevance_score=0.80), _event(severity=0.0)
        )
        _s2, signals_lo = _score_link(
            _link(relevance_score=0.79), _event(severity=0.0)
        )
        assert signals_hi["match_confidence"]["points"] == 1
        assert signals_lo["match_confidence"]["points"] == 0

    def test_full_stack_scores_high(self) -> None:
        score, _ = _score_link(
            _link(relevance_score=0.95),
            _event(severity=0.85, subtype="EXPORT_RESTRICTION"),
        )
        # severity(2) + subtype(1) + confidence(1) = 4
        assert score == 4
