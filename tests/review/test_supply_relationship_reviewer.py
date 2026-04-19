"""Unit tests for app.services.review.reviewers.supply_relationship helpers."""

from __future__ import annotations

from types import SimpleNamespace

from app.models.enums import SourceType
from app.services.review.reviewers.supply_relationship import (
    _find_hits,
    _score_document,
)


class TestFindHits:
    def test_substring_match(self) -> None:
        assert _find_hits("abc catl ltd", ["catl"]) == ["catl"]

    def test_no_match(self) -> None:
        assert _find_hits("nothing here", ["catl"]) == []

    def test_deduplicates_repeated_aliases(self) -> None:
        aliases = ["catl", "catl"]
        hits = _find_hits("mentions catl twice catl", aliases)
        assert hits == ["catl"]


def _doc(*, document_type: str = "article") -> SimpleNamespace:
    return SimpleNamespace(
        id=1,
        document_type=document_type,
        title="t",
        url=None,
        external_id="e",
        published_at=None,
        raw_text=None,
    )


def _source(source_type: str) -> SimpleNamespace:
    return SimpleNamespace(id=1, source_type=source_type)


class TestScoreDocument:
    def test_base_match_is_two(self) -> None:
        score, signals = _score_document(
            _doc(),
            _source(SourceType.NEWS.value),
            buyer_hits=["ford"],
            supplier_hits=["catl"],
            haystack_lower="ford signs with catl",
        )
        assert signals["both_parties_match"]["points"] == 2
        assert signals["sec_filing"]["points"] == 0
        assert signals["contract_keyword"]["points"] == 0
        assert score == 2

    def test_sec_filing_adds_one(self) -> None:
        score, signals = _score_document(
            _doc(document_type="filing"),
            _source(SourceType.SEC_EDGAR.value),
            buyer_hits=["ford"],
            supplier_hits=["catl"],
            haystack_lower="ford signs with catl",
        )
        assert signals["sec_filing"]["hit"] is True
        assert signals["sec_filing"]["points"] == 1
        assert score == 3

    def test_sec_edgar_source_adds_one_even_without_filing_type(self) -> None:
        score, signals = _score_document(
            _doc(document_type="article"),
            _source(SourceType.SEC_EDGAR.value),
            buyer_hits=["a"],
            supplier_hits=["b"],
            haystack_lower="a b",
        )
        assert signals["sec_filing"]["points"] == 1
        assert score == 3

    def test_contract_keyword_adds_one(self) -> None:
        score, signals = _score_document(
            _doc(),
            _source(SourceType.NEWS.value),
            buyer_hits=["ford"],
            supplier_hits=["catl"],
            haystack_lower="ford and catl sign an offtake agreement",
        )
        assert signals["contract_keyword"]["points"] == 1
        assert "offtake" in signals["contract_keyword"]["matched"]
        assert score == 3

    def test_full_stack_scores_high(self) -> None:
        score, signals = _score_document(
            _doc(document_type="filing"),
            _source(SourceType.SEC_EDGAR.value),
            buyer_hits=["ford"],
            supplier_hits=["catl"],
            haystack_lower="ford terminated supply agreement with catl",
        )
        assert score == 4
        assert signals["contract_keyword"]["matched"]
