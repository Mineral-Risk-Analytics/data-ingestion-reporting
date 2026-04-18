"""Unit tests for app.services.review.scoring.score_to_relevance."""

from __future__ import annotations

import pytest

from app.services.review.scoring import score_to_relevance


class TestScoreToRelevance:
    @pytest.mark.parametrize(
        "score,expected",
        [
            (0, "none"),
            (1, "low"),
            (2, "medium"),
            (3, "high"),
            (4, "high"),
            (99, "high"),
        ],
    )
    def test_default_bucketing(self, score: int, expected: str) -> None:
        assert score_to_relevance(score) == expected

    def test_max_bucket_caps_high_to_medium(self) -> None:
        assert score_to_relevance(3, max_bucket="medium") == "medium"
        assert score_to_relevance(99, max_bucket="medium") == "medium"

    def test_max_bucket_caps_high_to_low(self) -> None:
        assert score_to_relevance(5, max_bucket="low") == "low"

    def test_max_bucket_none_suppresses_all(self) -> None:
        assert score_to_relevance(5, max_bucket="none") == "none"
        assert score_to_relevance(0, max_bucket="none") == "none"

    def test_max_bucket_does_not_promote_low_scores(self) -> None:
        assert score_to_relevance(1, max_bucket="high") == "low"
        assert score_to_relevance(0, max_bucket="high") == "none"
