"""Tests for app.services.review.seed_staleness.run_seed_review.

Strategy: monkeypatch each reviewer module's ``review`` function so we don't
touch the DB. Use a MagicMock session that returns a scripted integer for
``_subject_count``. Assert persist vs dry-run behavior and the shape of
ReviewRunResult.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from app.services.review import seed_staleness
from app.services.review.seed_staleness import (
    SEED_TYPES,
    ReviewRunResult,
    run_seed_review,
)
from app.services.review.types import Finding


def _finding(seed_type: str, seed_key: str, relevance: str = "high") -> Finding:
    return Finding(
        seed_type=seed_type,
        seed_key=seed_key,
        seed_identifier_json={"key": seed_key},
        evidence_type="test_evidence",
        evidence_json={"title": "test"},
        relevance=relevance,
        match_signals_json={"total_score": 3},
    )


def _patch_reviewers(
    monkeypatch: pytest.MonkeyPatch,
    findings_map: dict[str, list[Finding]] | None = None,
) -> None:
    """Replace each reviewer's ``review`` with a stub returning ``findings_map``."""
    findings_map = findings_map or {}
    # Mapping from seed_type -> reviewer module attribute path.
    from app.services.review.reviewers import (
        company,
        facility,
        material_exposure,
        regulation,
        supply_relationship,
    )

    mapping = {
        "regulation": regulation,
        "company": company,
        "material_exposure": material_exposure,
        "supply_relationship": supply_relationship,
        "facility": facility,
    }
    def _make_stub(findings: list[Finding]):
        def _stub(session, **kwargs):
            return list(findings)
        return _stub

    for seed_type, mod in mapping.items():
        monkeypatch.setattr(
            mod,
            "review",
            _make_stub(findings_map.get(seed_type, [])),
        )

    # Subject counts: every seed_type reports 7 subjects.
    monkeypatch.setattr(
        seed_staleness,
        "_subject_count",
        lambda session, seed_type, seed_key: 7,
    )


def _mock_session() -> MagicMock:
    session = MagicMock()
    session.add = MagicMock()
    session.flush = MagicMock()
    added: list[Any] = []
    session.add.side_effect = lambda obj: added.append(obj)
    session._added = added
    return session


class TestRunSeedReview:
    def test_all_types_dispatched_when_seed_types_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_reviewers(
            monkeypatch,
            {
                "regulation": [_finding("regulation", "IRA_DOMESTIC")],
                "company": [_finding("company", "CATL")],
            },
        )
        session = _mock_session()
        # Orchestrator's _complete_run is a SELECT scalar_one call;
        # stub it out so we don't hit the DB.
        monkeypatch.setattr(
            seed_staleness,
            "_complete_run",
            lambda session, **kwargs: None,
        )

        result = run_seed_review(session, persist=True)

        assert isinstance(result, ReviewRunResult)
        assert result.seed_types == list(SEED_TYPES)
        assert result.total_findings == 2
        assert result.total_seeds_checked == 7 * len(SEED_TYPES)
        assert result.persisted is True

    def test_dry_run_does_not_add_to_session(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_reviewers(
            monkeypatch,
            {
                "regulation": [_finding("regulation", "IRA_DOMESTIC")],
            },
        )
        session = _mock_session()

        result = run_seed_review(session, persist=False)

        assert result.persisted is False
        # No SeedReviewRun or SeedReviewFinding objects should have been added.
        assert session.add.call_count == 0
        assert result.total_findings == 1

    def test_persist_adds_run_and_findings(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_reviewers(
            monkeypatch,
            {
                "regulation": [_finding("regulation", "IRA_DOMESTIC")],
                "company": [_finding("company", "CATL")],
            },
        )
        session = _mock_session()
        monkeypatch.setattr(
            seed_staleness,
            "_complete_run",
            lambda session, **kwargs: None,
        )

        run_seed_review(session, persist=True)

        # 1 SeedReviewRun + 2 SeedReviewFinding rows added.
        added_types = [type(o).__name__ for o in session._added]
        assert added_types.count("SeedReviewRun") == 1
        assert added_types.count("SeedReviewFinding") == 2

    def test_unknown_seed_type_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session = _mock_session()
        with pytest.raises(ValueError):
            run_seed_review(
                session, seed_types=["regulation", "bogus"], persist=False
            )

    def test_single_seed_type_subset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_reviewers(
            monkeypatch,
            {
                "regulation": [_finding("regulation", "IRA_DOMESTIC")],
                "company": [_finding("company", "CATL")],
            },
        )
        session = _mock_session()

        result = run_seed_review(
            session, seed_types=["regulation"], persist=False
        )

        assert result.seed_types == ["regulation"]
        # Only the regulation reviewer's findings are returned.
        assert list(result.findings_by_type.keys()) == ["regulation"]
        assert result.total_findings == 1

    def test_empty_reviewer_results_yield_zero_findings(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_reviewers(monkeypatch, {})
        session = _mock_session()

        result = run_seed_review(session, persist=False)

        assert result.total_findings == 0
        assert all(not lst for lst in result.findings_by_type.values())
