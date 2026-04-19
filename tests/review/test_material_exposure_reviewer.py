"""Unit tests for app.services.review.reviewers.material_exposure._score_event."""

from __future__ import annotations

from types import SimpleNamespace

from app.services.review.reviewers.material_exposure import _score_event


def _exposure(source_geography: str | None = "CN") -> SimpleNamespace:
    return SimpleNamespace(
        id=1,
        source_geography=source_geography,
        supply_chain_stage="mining",
        updated_at=None,
        as_of_date=None,
    )


def _event(*, primary: str | None = "CN", severity: float | None = 0.70) -> SimpleNamespace:
    geography = {"primary": primary} if primary is not None else {}
    return SimpleNamespace(
        id=1,
        severity_score=severity,
        geography_json=geography,
        event_date=None,
        title="t",
        event_type="GEOPOLITICAL_TRADE",
        source_document_id=None,
    )


class TestScoreEvent:
    def test_material_match_always_contributes_two(self) -> None:
        score, signals = _score_event(
            _exposure(source_geography=None),
            _event(primary=None, severity=0.0),
        )
        assert signals["material_match"]["points"] == 2
        assert score == 2  # material(2) only

    def test_geography_overlap_case_insensitive(self) -> None:
        score, signals = _score_event(
            _exposure(source_geography="cn"),
            _event(primary="CN", severity=0.0),
        )
        assert signals["geography_overlap"]["hit"] is True
        assert score == 3  # material(2) + geo(1)

    def test_geography_mismatch_no_points(self) -> None:
        score, signals = _score_event(
            _exposure(source_geography="CD"),
            _event(primary="CN", severity=0.0),
        )
        assert signals["geography_overlap"]["hit"] is False

    def test_severity_gate_at_060(self) -> None:
        _s1, signals_hi = _score_event(
            _exposure(source_geography=None),
            _event(primary=None, severity=0.60),
        )
        _s2, signals_lo = _score_event(
            _exposure(source_geography=None),
            _event(primary=None, severity=0.59),
        )
        assert signals_hi["severity"]["points"] == 1
        assert signals_lo["severity"]["points"] == 0

    def test_full_stack_scores_high(self) -> None:
        score, _ = _score_event(
            _exposure(source_geography="CN"),
            _event(primary="CN", severity=0.70),
        )
        # material(2) + geo(1) + severity(1) = 4
        assert score == 4
