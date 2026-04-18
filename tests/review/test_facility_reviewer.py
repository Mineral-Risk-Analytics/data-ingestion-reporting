"""Unit tests for app.services.review.reviewers.facility helpers."""

from __future__ import annotations

from app.services.review.reviewers.facility import _score_document
from app.services.review.scoring import score_to_relevance


class TestScoreDocument:
    def test_company_city_base_one(self) -> None:
        score, signals, matched = _score_document("catl ningde plant news")
        assert signals["company_and_city_match"]["points"] == 1
        assert signals["status_change_keyword"]["points"] == 0
        assert matched == []
        assert score == 1

    def test_status_keyword_adds_one(self) -> None:
        score, signals, matched = _score_document(
            "catl ningde plant shutdown announced"
        )
        assert signals["status_change_keyword"]["points"] == 1
        assert "shutdown" in matched
        assert score == 2

    def test_bucketing_respects_medium_cap(self) -> None:
        # Even at the max internal score (2), cap at medium.
        assert score_to_relevance(2, max_bucket="medium") == "medium"
        # A hypothetical higher score would still cap at medium.
        assert score_to_relevance(99, max_bucket="medium") == "medium"
