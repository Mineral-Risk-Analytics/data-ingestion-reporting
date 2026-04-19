"""Tests for app/services/ingestion/gleif.py"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch

import pytest

from app.services.ingestion.gleif import (
    GLEIF_MATCH_THRESHOLD,
    _GLEIF_SKIP_COMPANIES,
    _best_candidate,
    _name_similarity,
    _normalize_name,
    _search_gleif,
    enrich_all_companies,
    enrich_company_from_gleif,
)


# ---------------------------------------------------------------------------
# _normalize_name
# ---------------------------------------------------------------------------

class TestNormalizeName:
    def test_lowercase(self):
        assert _normalize_name("CATL") == "catl"

    def test_strips_inc(self):
        assert "inc" not in _normalize_name("Tesla, Inc.")

    def test_strips_ltd(self):
        assert "ltd" not in _normalize_name("Glencore Ltd.")

    def test_strips_ag(self):
        result = _normalize_name("Volkswagen AG")
        assert "ag" not in result.split()

    def test_strips_plc(self):
        assert "plc" not in _normalize_name("Rio Tinto plc")

    def test_collapses_whitespace(self):
        assert "  " not in _normalize_name("BHP  Group  Limited")


# ---------------------------------------------------------------------------
# _name_similarity
# ---------------------------------------------------------------------------

class TestNameSimilarity:
    def test_identical_strings_return_1(self):
        assert _name_similarity("CATL", "CATL") == 1.0

    def test_identical_after_normalization(self):
        score = _name_similarity("Tesla, Inc.", "Tesla Inc")
        assert score == 1.0

    def test_completely_different_below_half(self):
        score = _name_similarity("CATL", "Volkswagen AG")
        assert score < 0.5

    def test_suffix_only_difference_above_threshold(self):
        score = _name_similarity("Albemarle Corporation", "Albemarle Corp.")
        assert score >= GLEIF_MATCH_THRESHOLD

    def test_ford_vs_ford_credit_below_threshold(self):
        # Ensure a common false-positive case is rejected
        score = _name_similarity("Ford Motor Company", "Ford Motor Credit Company")
        assert score < GLEIF_MATCH_THRESHOLD

    def test_glencore_plc_match(self):
        score = _name_similarity("Glencore", "Glencore plc")
        assert score >= GLEIF_MATCH_THRESHOLD

    def test_sqm_full_name(self):
        score = _name_similarity(
            "SQM",
            "Sociedad Química y Minera de Chile S.A.",
        )
        # Very different names — should not match
        assert score < GLEIF_MATCH_THRESHOLD


# ---------------------------------------------------------------------------
# _best_candidate
# ---------------------------------------------------------------------------

def _make_candidate(lei: str, legal_name: str, other_names: list[str] | None = None) -> dict:
    """Build a minimal GLEIF-shaped record dict."""
    other_names_data = [{"name": n, "language": "en", "type": "TRADING_OR_OPERATING_NAME"}
                        for n in (other_names or [])]
    return {
        "id": lei,
        "attributes": {
            "lei": lei,
            "entity": {
                "legalName": {"name": legal_name, "language": "en"},
                "otherNames": other_names_data,
                "headquartersAddress": {"country": "US"},
                "status": "ACTIVE",
            },
        },
    }


class TestBestCandidate:
    def test_returns_none_when_no_candidates(self):
        record, score = _best_candidate("CATL", [])
        assert record is None
        assert score == 0.0

    def test_returns_best_above_threshold(self):
        candidates = [
            _make_candidate("LEI001", "Albemarle Corporation"),
        ]
        record, score = _best_candidate("Albemarle Corporation", candidates)
        assert record is not None
        assert record["id"] == "LEI001"
        assert score >= GLEIF_MATCH_THRESHOLD

    def test_returns_none_below_threshold(self):
        candidates = [
            _make_candidate("LEI001", "Some Completely Different Company"),
        ]
        record, score = _best_candidate("CATL", candidates)
        assert record is None

    def test_scores_other_names(self):
        # Legal name doesn't match but an otherName does
        candidates = [
            _make_candidate(
                "LEI002",
                "Contemporary Amperex Technology Co., Limited",
                other_names=["CATL"],
            )
        ]
        record, score = _best_candidate("CATL", candidates)
        assert record is not None

    def test_picks_highest_score_candidate(self):
        candidates = [
            _make_candidate("LEI001", "Some Random Company"),
            _make_candidate("LEI002", "Albemarle Corporation"),
        ]
        record, score = _best_candidate("Albemarle Corporation", candidates)
        assert record is not None
        assert record["id"] == "LEI002"


# ---------------------------------------------------------------------------
# enrich_company_from_gleif
# ---------------------------------------------------------------------------

def _mock_company(canonical_name: str, lei: str | None = None) -> MagicMock:
    company = MagicMock()
    company.canonical_name = canonical_name
    company.id = uuid.uuid4()
    company.lei = lei
    company.legal_name = None
    company.headquarters_country = None
    company.aliases = []
    return company


def _mock_gleif_response(legal_name: str, lei: str, country: str = "US") -> list[dict]:
    return [_make_candidate(lei, legal_name)]


class TestGleifSkipList:
    def test_skip_list_contains_expected_companies(self):
        assert "Norilsk Nickel" in _GLEIF_SKIP_COMPANIES
        assert "BTR New Energy" in _GLEIF_SKIP_COMPANIES

    def test_skip_list_is_frozenset(self):
        assert isinstance(_GLEIF_SKIP_COMPANIES, frozenset)


class TestEnrichCompanyFromGleif:
    def test_sets_lei_when_null(self):
        session = MagicMock()
        company = _mock_company("Albemarle Corporation")

        with patch(
            "app.services.ingestion.gleif._search_gleif",
            return_value=_mock_gleif_response("Albemarle Corporation", "LEI_ALB"),
        ):
            result = enrich_company_from_gleif(session, company, rate_limit_delay=0)

        assert result["matched"] is True
        assert company.lei == "LEI_ALB"
        assert result["lei"] == "LEI_ALB"

    def test_does_not_overwrite_existing_lei(self):
        session = MagicMock()
        company = _mock_company("Tesla", lei="EXISTING_LEI")

        with patch(
            "app.services.ingestion.gleif._search_gleif",
            return_value=_mock_gleif_response("Tesla, Inc.", "NEW_LEI"),
        ):
            result = enrich_company_from_gleif(session, company, rate_limit_delay=0)

        # lei was already set — should not be overwritten
        assert company.lei == "EXISTING_LEI"
        assert result["lei"] is None  # result reflects what was written, not existing

    def test_below_threshold_returns_unmatched(self):
        session = MagicMock()
        company = _mock_company("CATL")

        with patch(
            "app.services.ingestion.gleif._search_gleif",
            return_value=_mock_gleif_response("Some Completely Different Company", "LEI_XYZ"),
        ):
            result = enrich_company_from_gleif(session, company, rate_limit_delay=0)

        assert result["matched"] is False
        assert company.lei is None

    def test_no_results_returns_unmatched(self):
        session = MagicMock()
        company = _mock_company("Unknown Corp")

        with patch(
            "app.services.ingestion.gleif._search_gleif",
            return_value=[],
        ):
            result = enrich_company_from_gleif(session, company, rate_limit_delay=0)

        assert result["matched"] is False
        assert company.lei is None

    def test_adds_other_names_as_aliases(self):
        session = MagicMock()
        company = _mock_company("CATL")

        gleif_data = [
            _make_candidate(
                "LEI_CATL",
                "Contemporary Amperex Technology Co., Limited",
                other_names=["CATL International"],
            )
        ]

        with patch(
            "app.services.ingestion.gleif._search_gleif",
            return_value=gleif_data,
        ):
            result = enrich_company_from_gleif(session, company, rate_limit_delay=0)

        assert result["matched"] is True
        assert result["aliases_added"] == 1
        session.add.assert_called_once()

    def test_does_not_add_duplicate_aliases(self):
        session = MagicMock()
        company = _mock_company("CATL")

        existing_alias = MagicMock()
        existing_alias.alias = "CATL International"
        company.aliases = [existing_alias]

        gleif_data = [
            _make_candidate(
                "LEI_CATL",
                "Contemporary Amperex Technology Co., Limited",
                other_names=["CATL International"],
            )
        ]

        with patch(
            "app.services.ingestion.gleif._search_gleif",
            return_value=gleif_data,
        ):
            result = enrich_company_from_gleif(session, company, rate_limit_delay=0)

        assert result["aliases_added"] == 0
        session.add.assert_not_called()

    def test_sets_legal_name_when_null(self):
        session = MagicMock()
        company = _mock_company("Glencore")

        with patch(
            "app.services.ingestion.gleif._search_gleif",
            return_value=_mock_gleif_response("Glencore plc", "LEI_GLEN"),
        ):
            enrich_company_from_gleif(session, company, rate_limit_delay=0)

        assert company.legal_name == "Glencore plc"

    def test_does_not_overwrite_legal_name(self):
        session = MagicMock()
        company = _mock_company("Glencore")
        company.legal_name = "Manually Set Legal Name"

        with patch(
            "app.services.ingestion.gleif._search_gleif",
            return_value=_mock_gleif_response("Glencore plc", "LEI_GLEN"),
        ):
            enrich_company_from_gleif(session, company, rate_limit_delay=0)

        assert company.legal_name == "Manually Set Legal Name"

    def test_gleif_search_name_overrides_canonical_name_in_query(self):
        """When gleif_search_name is supplied it is passed to _search_gleif,
        not the canonical_name."""
        session = MagicMock()
        company = _mock_company("CATL")

        with patch(
            "app.services.ingestion.gleif._search_gleif",
            return_value=[],
        ) as mock_search:
            enrich_company_from_gleif(
                session,
                company,
                rate_limit_delay=0,
                gleif_search_name="Contemporary Amperex Technology",
            )

        mock_search.assert_called_once_with(
            "Contemporary Amperex Technology",
            timeout=15,
        )

    def test_gleif_search_name_result_stored_in_return_dict(self):
        session = MagicMock()
        company = _mock_company("CATL")

        with patch(
            "app.services.ingestion.gleif._search_gleif",
            return_value=[],
        ):
            result = enrich_company_from_gleif(
                session,
                company,
                rate_limit_delay=0,
                gleif_search_name="Contemporary Amperex Technology",
            )

        assert result["search_name_used"] == "Contemporary Amperex Technology"

    def test_canonical_name_used_when_no_override(self):
        session = MagicMock()
        company = _mock_company("Glencore")

        with patch(
            "app.services.ingestion.gleif._search_gleif",
            return_value=[],
        ) as mock_search:
            result = enrich_company_from_gleif(session, company, rate_limit_delay=0)

        mock_search.assert_called_once_with("Glencore", timeout=15)
        assert result["search_name_used"] == "Glencore"


# ---------------------------------------------------------------------------
# enrich_all_companies
# ---------------------------------------------------------------------------

class TestEnrichAllCompanies:
    def test_only_queries_companies_without_lei(self):
        session = MagicMock()
        company_without_lei = _mock_company("CATL", lei=None)
        session.scalars.return_value.all.return_value = [company_without_lei]

        with patch(
            "app.services.ingestion.gleif._search_gleif",
            return_value=[],
        ):
            results = enrich_all_companies(session, rate_limit_delay=0)

        assert len(results) == 1
        assert results[0]["company"] == "CATL"

    def test_commits_after_each_match(self):
        session = MagicMock()
        company = _mock_company("Albemarle Corporation")
        session.scalars.return_value.all.return_value = [company]

        with patch(
            "app.services.ingestion.gleif._search_gleif",
            return_value=_mock_gleif_response("Albemarle Corporation", "LEI_ALB"),
        ):
            results = enrich_all_companies(session, rate_limit_delay=0)

        assert results[0]["matched"] is True
        session.commit.assert_called_once()

    def test_no_commit_when_no_match(self):
        session = MagicMock()
        company = _mock_company("Unknown Corp")
        session.scalars.return_value.all.return_value = [company]

        with patch(
            "app.services.ingestion.gleif._search_gleif",
            return_value=[],
        ):
            results = enrich_all_companies(session, rate_limit_delay=0)

        assert results[0]["matched"] is False
        session.commit.assert_not_called()

    def test_search_name_override_passed_to_enrich(self):
        """search_name_overrides are forwarded to enrich_company_from_gleif."""
        session = MagicMock()
        company = _mock_company("CATL")
        session.scalars.return_value.all.return_value = [company]

        with patch(
            "app.services.ingestion.gleif._search_gleif",
            return_value=[],
        ) as mock_search:
            enrich_all_companies(
                session,
                rate_limit_delay=0,
                search_name_overrides={"CATL": "Contemporary Amperex Technology"},
            )

        mock_search.assert_called_once_with(
            "Contemporary Amperex Technology",
            timeout=15,
        )

    def test_no_override_uses_canonical_name(self):
        """When no override exists the canonical_name is used for search."""
        session = MagicMock()
        company = _mock_company("Glencore")
        session.scalars.return_value.all.return_value = [company]

        with patch(
            "app.services.ingestion.gleif._search_gleif",
            return_value=[],
        ) as mock_search:
            enrich_all_companies(session, rate_limit_delay=0, search_name_overrides={})

        mock_search.assert_called_once_with("Glencore", timeout=15)
