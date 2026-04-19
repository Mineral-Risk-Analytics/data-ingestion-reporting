"""Unit tests for app.services.review.reviewers.regulation.

Tests focus on _score_document — the pure helper — using hand-built
SourceDocument-shaped objects. The DB integration path (_candidate_documents)
is not exercised here; it's a thin SQLAlchemy select that's covered by the
orchestrator-level pattern.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.services.review.reviewers.regulation import _score_document


_IRA_CONFIG = {
    "fr_query_names": ["critical_minerals", "lithium_battery"],
    "keywords": [
        "FEOC",
        "foreign entity of concern",
        "critical mineral",
        "domestic content",
    ],
    "agency_hints": ["Treasury", "IRS"],
}


def _doc(
    *,
    title: str = "",
    raw_text: str = "",
    metadata: dict | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        title=title,
        raw_text=raw_text,
        metadata_json=metadata or {},
    )


class TestScoreDocument:
    def test_query_name_match_contributes_two(self) -> None:
        doc = _doc(metadata={"query_name": "critical_minerals"})
        score, signals = _score_document(doc, _IRA_CONFIG)
        assert score == 2
        assert signals["query_name_match"]["hit"] is True
        assert signals["query_name_match"]["points"] == 2

    def test_unrelated_query_name_is_miss(self) -> None:
        doc = _doc(metadata={"query_name": "export_control"})
        score, signals = _score_document(doc, _IRA_CONFIG)
        assert score == 0
        assert signals["query_name_match"]["hit"] is False

    def test_keyword_hits_are_capped_at_two(self) -> None:
        doc = _doc(
            title="FEOC guidance",
            raw_text="The foreign entity of concern rule and critical mineral updates",
        )
        score, signals = _score_document(doc, _IRA_CONFIG)
        # 3 keyword hits but cap at +2.
        assert signals["keyword_match"]["points"] == 2
        assert len(signals["keyword_match"]["matched"]) >= 3
        assert score == 2

    def test_agency_match_from_string_list(self) -> None:
        doc = _doc(metadata={"agencies": ["Department of the Treasury"]})
        score, signals = _score_document(doc, _IRA_CONFIG)
        assert signals["agency_match"]["hit"] is True
        assert "Treasury" in signals["agency_match"]["agency"]
        assert score == 1

    def test_agency_match_from_dict_list(self) -> None:
        doc = _doc(metadata={"agencies": [{"name": "IRS (Treasury)"}]})
        score, signals = _score_document(doc, _IRA_CONFIG)
        assert signals["agency_match"]["hit"] is True
        assert score == 1

    def test_full_stack_scores_reach_high(self) -> None:
        doc = _doc(
            title="FEOC clarifications",
            raw_text="foreign entity of concern rule updates",
            metadata={
                "query_name": "critical_minerals",
                "agencies": ["Treasury"],
            },
        )
        score, signals = _score_document(doc, _IRA_CONFIG)
        # Query (2) + keywords capped (2) + agency (1) = 5.
        assert score == 5
        assert signals["query_name_match"]["points"] == 2
        assert signals["keyword_match"]["points"] == 2
        assert signals["agency_match"]["points"] == 1

    def test_no_hits_scores_zero(self) -> None:
        doc = _doc(title="Unrelated commerce rule")
        score, signals = _score_document(doc, _IRA_CONFIG)
        assert score == 0
        assert signals["total_score"] == 0

    def test_keyword_match_is_case_insensitive(self) -> None:
        doc = _doc(raw_text="feoc updates this week")
        score, signals = _score_document(doc, _IRA_CONFIG)
        assert signals["keyword_match"]["points"] >= 1
        assert "FEOC" in signals["keyword_match"]["matched"]
