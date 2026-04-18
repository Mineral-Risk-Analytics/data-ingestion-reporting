"""Tests for the OpenSanctions ingestion module.

All tests are self-contained — no real HTTP calls and no live database.
``parse_sanctions_csv`` and ``_build_name_index`` are tested with in-memory
CSV/dict fixtures. ``match_companies`` and ``ingest_opensanctions`` are tested
with mocked SQLAlchemy sessions.
"""

from __future__ import annotations

import io
import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from app.services.ingestion.opensanctions import (
    _build_lei_index,
    _build_name_index,
    _content_hash,
    _extract_leis,
    _normalise,
    match_companies,
    parse_sanctions_csv,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_csv_bytes(rows: list[dict]) -> io.BytesIO:
    """Build an in-memory CSV buffer from a list of row dicts.

    Uses detach() so the TextIOWrapper does not close the underlying BytesIO
    when it goes out of scope (Python 3.12+ GC behaviour).
    """
    fieldnames = [
        "id", "schema", "name", "aliases", "birth_date",
        "countries", "identifiers", "datasets", "first_seen", "last_seen",
    ]
    import csv as _csv
    buf = io.BytesIO()
    wrapper = io.TextIOWrapper(buf, encoding="utf-8", newline="")
    writer = _csv.DictWriter(wrapper, fieldnames=fieldnames)
    writer.writeheader()
    for row in rows:
        full_row = {f: row.get(f, "") for f in fieldnames}
        writer.writerow(full_row)
    wrapper.flush()
    data = buf.getvalue()
    wrapper.detach()
    return io.BytesIO(data)


_SAMPLE_ROWS = [
    {
        "id": "os-person-1",
        "schema": "Person",
        "name": "John Doe",
        "aliases": "",
        "countries": "RU",
        "identifiers": "",
        "datasets": "us_ofac_sdn",
        "first_seen": "2020-01-01",
        "last_seen": "2024-01-01",
    },
    {
        "id": "os-company-1",
        "schema": "Company",
        "name": "Rosneft Oil Company",
        "aliases": "Rosneft|PJSC Rosneft",
        "countries": "RU",
        "identifiers": "",
        "datasets": "us_ofac_sdn|eu_fsf",
        "first_seen": "2014-03-20",
        "last_seen": "2024-06-01",
    },
    {
        "id": "os-org-1",
        "schema": "Organization",
        "name": "Glencore International AG",
        "aliases": "Glencore PLC|GLEN",
        "countries": "CH|CD",
        "identifiers": "lei-2138008O0QXTNYGBCT15|isin-CH0002647786",
        "datasets": "ch_seco_sanctions",
        "first_seen": "2021-04-01",
        "last_seen": "2024-05-01",
    },
    {
        "id": "os-legal-1",
        "schema": "LegalEntity",
        "name": "Shell Trading (US) Company",
        "aliases": "",
        "countries": "US",
        "identifiers": "",
        "datasets": "us_ofac_sdn",
        "first_seen": "2022-01-01",
        "last_seen": "2024-01-01",
    },
    {
        "id": "os-vessel-1",
        "schema": "Vessel",
        "name": "MV Sanctioned Ship",
        "aliases": "",
        "countries": "KP",
        "identifiers": "",
        "datasets": "us_ofac_sdn",
        "first_seen": "2023-01-01",
        "last_seen": "2024-01-01",
    },
]


# ---------------------------------------------------------------------------
# _normalise
# ---------------------------------------------------------------------------

class TestNormalise:
    def test_lowercases(self):
        assert _normalise("ROSNEFT") == "rosneft"

    def test_collapses_whitespace(self):
        assert _normalise("Extra  Spaces  Here") == "extra spaces here"

    def test_strips_inc(self):
        assert _normalise("Tesla Inc.") == "tesla"

    def test_strips_ltd(self):
        assert _normalise("Albemarle Ltd.") == "albemarle"

    def test_strips_plc(self):
        assert _normalise("Glencore plc") == "glencore"

    def test_strips_ag(self):
        assert _normalise("Volkswagen AG") == "volkswagen"

    def test_strips_pjsc(self):
        assert _normalise("Nornickel PJSC") == "nornickel"

    def test_strips_jsc(self):
        assert _normalise("Nornickel JSC") == "nornickel"

    def test_strips_oao(self):
        assert _normalise("Lukoil OAO") == "lukoil"

    def test_strips_pao(self):
        assert _normalise("Gazprom PAO") == "gazprom"

    def test_strips_company(self):
        assert _normalise("Rosneft Oil Company") == "rosneft oil"

    def test_strips_corporation(self):
        assert _normalise("Albemarle Corporation") == "albemarle"

    def test_strips_punctuation(self):
        assert _normalise("Tesla, Inc.") == "tesla"

    def test_pjsc_prefix_stripped(self):
        assert _normalise("PJSC Norilsk Nickel") == "norilsk nickel"

    def test_identical_after_suffix_removal(self):
        """PJSC prefix and nothing both reduce to the same core."""
        assert _normalise("PJSC Norilsk Nickel") == _normalise("Norilsk Nickel")


# ---------------------------------------------------------------------------
# _extract_leis
# ---------------------------------------------------------------------------

class TestExtractLeis:
    def test_extracts_single_lei(self):
        leis = _extract_leis("lei-2138008O0QXTNYGBCT15")
        assert leis == ["2138008O0QXTNYGBCT15"]

    def test_extracts_lei_from_mixed_identifiers(self):
        leis = _extract_leis("lei-2138008O0QXTNYGBCT15|isin-CH0002647786|bic-GLCNCHGG")
        assert leis == ["2138008O0QXTNYGBCT15"]

    def test_extracts_multiple_leis(self):
        leis = _extract_leis("lei-AAAA|lei-BBBB")
        assert set(leis) == {"AAAA", "BBBB"}

    def test_empty_string_returns_empty(self):
        assert _extract_leis("") == []

    def test_no_lei_prefix_returns_empty(self):
        assert _extract_leis("isin-US0378331005|bic-CITIUS33") == []

    def test_lei_uppercased(self):
        leis = _extract_leis("lei-aaaa1111bbbb2222")
        assert leis == ["AAAA1111BBBB2222"]

    def test_case_insensitive_prefix(self):
        leis = _extract_leis("LEI-AAAA")
        assert leis == ["AAAA"]


# ---------------------------------------------------------------------------
# parse_sanctions_csv
# ---------------------------------------------------------------------------

class TestParseSanctionsCsv:
    def test_filters_out_person_rows(self):
        buf = _make_csv_bytes(_SAMPLE_ROWS)
        results = parse_sanctions_csv(buf)
        names = [r["name"] for r in results]
        assert "John Doe" not in names

    def test_filters_out_vessel_rows(self):
        buf = _make_csv_bytes(_SAMPLE_ROWS)
        results = parse_sanctions_csv(buf)
        names = [r["name"] for r in results]
        assert "MV Sanctioned Ship" not in names

    def test_keeps_company_rows(self):
        buf = _make_csv_bytes(_SAMPLE_ROWS)
        results = parse_sanctions_csv(buf)
        names = [r["name"] for r in results]
        assert "Rosneft Oil Company" in names

    def test_keeps_organization_rows(self):
        buf = _make_csv_bytes(_SAMPLE_ROWS)
        results = parse_sanctions_csv(buf)
        names = [r["name"] for r in results]
        assert "Glencore International AG" in names

    def test_keeps_legal_entity_rows(self):
        buf = _make_csv_bytes(_SAMPLE_ROWS)
        results = parse_sanctions_csv(buf)
        names = [r["name"] for r in results]
        assert "Shell Trading (US) Company" in names

    def test_total_count_excludes_non_company_schemas(self):
        buf = _make_csv_bytes(_SAMPLE_ROWS)
        results = parse_sanctions_csv(buf)
        # Person + Vessel excluded → 3 remain
        assert len(results) == 3

    def test_aliases_split_on_pipe(self):
        buf = _make_csv_bytes(_SAMPLE_ROWS)
        results = parse_sanctions_csv(buf)
        rosneft = next(r for r in results if r["name"] == "Rosneft Oil Company")
        assert "rosneft" in rosneft["aliases"]
        assert "pjsc rosneft" in rosneft["aliases"]

    def test_aliases_are_lowercased(self):
        buf = _make_csv_bytes(_SAMPLE_ROWS)
        results = parse_sanctions_csv(buf)
        glencore = next(r for r in results if r["name"] == "Glencore International AG")
        assert all(a == a.lower() for a in glencore["aliases"])

    def test_aliases_deduped(self):
        row = {
            "id": "os-dup-1",
            "schema": "Company",
            "name": "Acme Corp",
            "aliases": "Acme|ACME|acme",
            "countries": "US",
            "datasets": "us_ofac_sdn",
        }
        buf = _make_csv_bytes([row])
        results = parse_sanctions_csv(buf)
        assert len(results[0]["aliases"]) == 1  # all three lowercase to "acme"

    def test_countries_split_on_pipe(self):
        buf = _make_csv_bytes(_SAMPLE_ROWS)
        results = parse_sanctions_csv(buf)
        glencore = next(r for r in results if r["name"] == "Glencore International AG")
        assert "CH" in glencore["countries"]
        assert "CD" in glencore["countries"]

    def test_countries_uppercased(self):
        row = {
            "id": "os-lower-1",
            "schema": "Company",
            "name": "Some Company",
            "aliases": "",
            "countries": "cn|ru",
            "datasets": "test_dataset",
        }
        buf = _make_csv_bytes([row])
        results = parse_sanctions_csv(buf)
        assert all(c == c.upper() for c in results[0]["countries"])

    def test_datasets_split_on_pipe(self):
        buf = _make_csv_bytes(_SAMPLE_ROWS)
        results = parse_sanctions_csv(buf)
        rosneft = next(r for r in results if r["name"] == "Rosneft Oil Company")
        assert "us_ofac_sdn" in rosneft["datasets"]
        assert "eu_fsf" in rosneft["datasets"]

    def test_empty_aliases_returns_empty_list(self):
        buf = _make_csv_bytes(_SAMPLE_ROWS)
        results = parse_sanctions_csv(buf)
        shell = next(r for r in results if "Shell Trading" in r["name"])
        assert shell["aliases"] == []

    def test_opensanctions_id_preserved(self):
        buf = _make_csv_bytes(_SAMPLE_ROWS)
        results = parse_sanctions_csv(buf)
        rosneft = next(r for r in results if r["name"] == "Rosneft Oil Company")
        assert rosneft["opensanctions_id"] == "os-company-1"

    def test_empty_csv_returns_empty_list(self):
        empty_buf = _make_csv_bytes([])
        assert parse_sanctions_csv(empty_buf) == []

    def test_leis_extracted_from_identifiers(self):
        buf = _make_csv_bytes(_SAMPLE_ROWS)
        results = parse_sanctions_csv(buf)
        glencore = next(r for r in results if r["name"] == "Glencore International AG")
        assert "2138008O0QXTNYGBCT15" in glencore["leis"]

    def test_leis_empty_when_no_identifiers(self):
        buf = _make_csv_bytes(_SAMPLE_ROWS)
        results = parse_sanctions_csv(buf)
        rosneft = next(r for r in results if r["name"] == "Rosneft Oil Company")
        assert rosneft["leis"] == []

    def test_leis_field_present_on_all_results(self):
        buf = _make_csv_bytes(_SAMPLE_ROWS)
        results = parse_sanctions_csv(buf)
        assert all("leis" in r for r in results)


# ---------------------------------------------------------------------------
# _build_name_index
# ---------------------------------------------------------------------------

class TestBuildNameIndex:
    def _entities(self) -> list[dict]:
        return [
            {
                "opensanctions_id": "os-1",
                "name": "Rosneft Oil Company",
                "aliases": ["rosneft", "pjsc rosneft"],
                "leis": [],
                "countries": ["RU"],
                "datasets": ["us_ofac_sdn"],
                "first_seen": None,
                "last_seen": None,
            },
            {
                "opensanctions_id": "os-2",
                "name": "Glencore International AG",
                "aliases": ["glencore plc", "glen"],
                "leis": [],
                "countries": ["CH", "CD"],
                "datasets": ["ch_seco_sanctions"],
                "first_seen": None,
                "last_seen": None,
            },
        ]

    def test_primary_name_indexed_with_suffix_stripped(self):
        index = _build_name_index(self._entities())
        # "Rosneft Oil Company" → strip "company" → "rosneft oil"
        assert "rosneft oil" in index
        # Original unsimplified form is NOT a key
        assert "rosneft oil company" not in index

    def test_alias_pjsc_stripped(self):
        index = _build_name_index(self._entities())
        # alias "pjsc rosneft" → strip "pjsc" → "rosneft"
        assert "rosneft" in index

    def test_alias_plc_stripped(self):
        index = _build_name_index(self._entities())
        # alias "glencore plc" → strip "plc" → "glencore"
        assert "glencore" in index

    def test_non_suffix_alias_preserved(self):
        index = _build_name_index(self._entities())
        # "glen" has no suffix to strip
        assert "glen" in index

    def test_normalisation_lowercases(self):
        entities = [
            {
                "opensanctions_id": "os-upper",
                "name": "UPPER CASE NAME",
                "aliases": [],
                "leis": [],
                "countries": [],
                "datasets": [],
                "first_seen": None,
                "last_seen": None,
            }
        ]
        index = _build_name_index(entities)
        assert "upper case name" in index
        assert "UPPER CASE NAME" not in index

    def test_normalisation_collapses_whitespace(self):
        entities = [
            {
                "opensanctions_id": "os-ws",
                "name": "Extra  Spaces  Here",
                "aliases": [],
                "leis": [],
                "countries": [],
                "datasets": [],
                "first_seen": None,
                "last_seen": None,
            }
        ]
        index = _build_name_index(entities)
        assert "extra spaces here" in index

    def test_multiple_entities_share_same_alias_key(self):
        """Two entities with overlapping normalised aliases → both in index list."""
        entities = [
            {
                "opensanctions_id": "os-a",
                "name": "Company A",
                "aliases": ["shared name"],
                "leis": [],
                "countries": [],
                "datasets": [],
                "first_seen": None,
                "last_seen": None,
            },
            {
                "opensanctions_id": "os-b",
                "name": "Company B",
                "aliases": ["shared name"],
                "leis": [],
                "countries": [],
                "datasets": [],
                "first_seen": None,
                "last_seen": None,
            },
        ]
        index = _build_name_index(entities)
        assert len(index["shared name"]) == 2

    def test_empty_entity_list_returns_empty_index(self):
        assert _build_name_index([]) == {}


# ---------------------------------------------------------------------------
# _build_lei_index
# ---------------------------------------------------------------------------

class TestBuildLeiIndex:
    def _entities_with_leis(self) -> list[dict]:
        return [
            {
                "opensanctions_id": "os-lei-1",
                "name": "Glencore International AG",
                "aliases": [],
                "leis": ["2138008O0QXTNYGBCT15"],
                "countries": ["CH"],
                "datasets": ["ch_seco_sanctions"],
                "first_seen": None,
                "last_seen": None,
            },
            {
                "opensanctions_id": "os-lei-2",
                "name": "Nornickel PJSC",
                "aliases": [],
                "leis": [],
                "countries": ["RU"],
                "datasets": ["us_ofac_sdn"],
                "first_seen": None,
                "last_seen": None,
            },
        ]

    def test_entity_indexed_by_lei(self):
        index = _build_lei_index(self._entities_with_leis())
        assert "2138008O0QXTNYGBCT15" in index

    def test_entity_without_lei_not_in_index(self):
        index = _build_lei_index(self._entities_with_leis())
        assert len(index) == 1  # only the Glencore entity

    def test_lei_maps_to_entity_list(self):
        index = _build_lei_index(self._entities_with_leis())
        assert index["2138008O0QXTNYGBCT15"][0]["opensanctions_id"] == "os-lei-1"

    def test_multiple_leis_per_entity(self):
        entities = [
            {
                "opensanctions_id": "os-multi",
                "name": "Some Corp",
                "aliases": [],
                "leis": ["AAAA", "BBBB"],
                "countries": [],
                "datasets": [],
                "first_seen": None,
                "last_seen": None,
            }
        ]
        index = _build_lei_index(entities)
        assert "AAAA" in index
        assert "BBBB" in index

    def test_empty_entities_returns_empty_index(self):
        assert _build_lei_index([]) == {}


# ---------------------------------------------------------------------------
# match_companies
# ---------------------------------------------------------------------------

def _mock_company(canonical_name: str, lei: str | None = None) -> MagicMock:
    c = MagicMock()
    c.id = uuid.uuid4()
    c.canonical_name = canonical_name
    c.lei = lei
    return c


def _mock_alias(
    company_id: uuid.UUID,
    alias: str,
    alias_type: str = "aka",
) -> MagicMock:
    a = MagicMock()
    a.company_id = company_id
    a.alias = alias
    a.alias_type = alias_type
    return a


def _mock_session_for_match(
    companies: list[MagicMock],
    aliases: list[MagicMock],
) -> MagicMock:
    session = MagicMock()

    def scalars_side_effect(stmt):
        result = MagicMock()
        result.all.return_value = scalars_side_effect._calls.pop(0)
        return result

    scalars_side_effect._calls = [companies, aliases]
    session.scalars.side_effect = scalars_side_effect
    return session


def _entity(
    name: str,
    aliases: list[str] | None = None,
    leis: list[str] | None = None,
    countries: list[str] | None = None,
    datasets: list[str] | None = None,
    opensanctions_id: str = "os-1",
) -> dict:
    return {
        "opensanctions_id": opensanctions_id,
        "name": name,
        "aliases": aliases or [],
        "leis": leis or [],
        "countries": countries or [],
        "datasets": datasets or [],
        "first_seen": None,
        "last_seen": None,
    }


class TestMatchCompanies:
    def _entities_glencore(self) -> list[dict]:
        return [
            _entity(
                name="GLENCORE INTERNATIONAL AG",
                aliases=["glencore plc"],
                leis=[],
                countries=["CH", "CD"],
                datasets=["ch_seco_sanctions"],
            )
        ]

    def test_match_via_alias(self):
        company = _mock_company("Glencore International")
        alias = _mock_alias(company.id, "Glencore PLC")
        session = _mock_session_for_match([company], [alias])

        result = match_companies(session, self._entities_glencore())

        # "Glencore PLC" → strip "plc" → "glencore"
        # entity "GLENCORE INTERNATIONAL AG" → strip "international"/"ag" → "glencore"
        # canonical "Glencore International" → strip "international" → "glencore"
        assert len(result) == 1
        matched_company, matched_entities = result[0]
        assert matched_company.canonical_name == "Glencore International"
        assert len(matched_entities) >= 1

    def test_match_via_canonical_name(self):
        company = _mock_company("Glencore International AG")
        session = _mock_session_for_match([company], [])

        result = match_companies(session, self._entities_glencore())

        assert len(result) == 1

    def test_match_via_lei(self):
        lei = "2138008O0QXTNYGBCT15"
        company = _mock_company("Glencore", lei=lei)
        session = _mock_session_for_match([company], [])
        entities = [_entity(name="Glencore International AG", leis=[lei])]

        result = match_companies(session, entities)

        assert len(result) == 1
        matched_company, _ = result[0]
        assert matched_company.canonical_name == "Glencore"

    def test_lei_match_takes_priority_over_name(self):
        """Company matched via LEI should appear in results even if its name differs."""
        lei = "UNIQUE-LEI-123"
        company = _mock_company("Completely Different Name", lei=lei)
        session = _mock_session_for_match([company], [])
        # Entity has a different name but the same LEI
        entities = [_entity(name="GLENCORE INTERNATIONAL AG", leis=[lei])]

        result = match_companies(session, entities)

        assert len(result) == 1

    def test_no_match_returns_empty(self):
        company = _mock_company("Tesla Inc.")
        session = _mock_session_for_match([company], [])

        result = match_companies(session, self._entities_glencore())
        assert result == []

    def test_empty_companies_returns_empty(self):
        session = _mock_session_for_match([], [])
        result = match_companies(session, self._entities_glencore())
        assert result == []

    def test_empty_entities_returns_empty(self):
        company = _mock_company("Glencore International AG")
        session = _mock_session_for_match([company], [])
        result = match_companies(session, [])
        assert result == []

    def test_matched_entities_deduplicated(self):
        """Same entity matched via both canonical name and alias should appear once."""
        company = _mock_company("Glencore International AG")
        alias = _mock_alias(company.id, "Glencore International AG")
        session = _mock_session_for_match([company], [alias])

        result = match_companies(session, self._entities_glencore())
        assert len(result) == 1
        _, matched_entities = result[0]
        ids = [e["opensanctions_id"] for e in matched_entities]
        assert len(ids) == len(set(ids))

    def test_ticker_aliases_skipped(self):
        """Aliases with alias_type='ticker' must not be used for name matching."""
        company = _mock_company("Tesla Inc.")
        # Create a ticker alias that happens to match the Glencore entity name
        ticker_alias = _mock_alias(company.id, "Glencore International AG", alias_type="ticker")
        session = _mock_session_for_match([company], [ticker_alias])

        result = match_companies(session, self._entities_glencore())
        # Ticker alias should be skipped; no match
        assert result == []

    def test_lei_aliases_skipped_from_name_matching(self):
        """Aliases with alias_type='lei' are raw LEI strings, not human-readable names."""
        company = _mock_company("Tesla Inc.")
        lei_alias = _mock_alias(company.id, "GLENCORE INTERNATIONAL AG", alias_type="lei")
        session = _mock_session_for_match([company], [lei_alias])

        result = match_companies(session, self._entities_glencore())
        assert result == []

    def test_pjsc_suffix_stripped_for_match(self):
        """'Nornickel PJSC' in OpenSanctions should match our alias 'Nornickel'."""
        company = _mock_company("Norilsk Nickel")
        alias = _mock_alias(company.id, "Nornickel")
        session = _mock_session_for_match([company], [alias])
        entities = [_entity(name="Nornickel PJSC", aliases=[])]

        result = match_companies(session, entities)

        assert len(result) == 1

    def test_company_null_lei_skips_lei_matching(self):
        """A company with lei=None must not be considered for LEI matching."""
        company = _mock_company("Rosneft", lei=None)
        session = _mock_session_for_match([company], [])
        entities = [_entity(name="Unrelated Entity", leis=["SOME-LEI"])]

        result = match_companies(session, entities)
        assert result == []


# ---------------------------------------------------------------------------
# _content_hash
# ---------------------------------------------------------------------------

class TestContentHash:
    def test_returns_64_char_hex_string(self):
        h = _content_hash("title", "summary", datetime(2024, 1, 15, tzinfo=timezone.utc))
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)

    def test_same_inputs_same_hash(self):
        dt = datetime(2024, 1, 15, tzinfo=timezone.utc)
        h1 = _content_hash("title", "summary", dt)
        h2 = _content_hash("title", "summary", dt)
        assert h1 == h2

    def test_different_title_different_hash(self):
        dt = datetime(2024, 1, 15, tzinfo=timezone.utc)
        h1 = _content_hash("title A", "summary", dt)
        h2 = _content_hash("title B", "summary", dt)
        assert h1 != h2

    def test_uses_only_date_portion(self):
        """Two datetimes on the same date but different times → same hash."""
        dt1 = datetime(2024, 1, 15, 8, 0, 0, tzinfo=timezone.utc)
        dt2 = datetime(2024, 1, 15, 23, 59, 59, tzinfo=timezone.utc)
        h1 = _content_hash("title", "summary", dt1)
        h2 = _content_hash("title", "summary", dt2)
        assert h1 == h2

    def test_none_event_date(self):
        h = _content_hash("title", "summary", None)
        assert len(h) == 64


# ---------------------------------------------------------------------------
# ingest_opensanctions — integration-style with mocked session
# ---------------------------------------------------------------------------

def _make_entity(
    name: str = "Rosneft Oil Company",
    aliases: list[str] | None = None,
    leis: list[str] | None = None,
    countries: list[str] | None = None,
    datasets: list[str] | None = None,
    opensanctions_id: str = "os-1",
) -> dict:
    return {
        "opensanctions_id": opensanctions_id,
        "name": name,
        "aliases": aliases or [],
        "leis": leis or [],
        "countries": countries or ["RU"],
        "datasets": datasets or ["us_ofac_sdn"],
        "first_seen": None,
        "last_seen": None,
    }


def _make_ingest_session(existing_event=None) -> MagicMock:
    """Mock session for ingest_opensanctions tests.

    All scalar() calls return ``existing_event`` (used for content_hash checks).
    scalars() calls (used inside match_companies) return empty lists so company
    matching produces zero matches — we patch match_companies separately.
    """
    session = MagicMock()
    session.scalar.return_value = existing_event

    scalars_result = MagicMock()
    scalars_result.all.return_value = []
    session.scalars.return_value = scalars_result

    def add_side_effect(obj):
        if not hasattr(obj, "id") or obj.id is None:
            obj.id = 999

    session.add.side_effect = add_side_effect
    return session


class TestIngestOpensanctions:
    def test_company_events_inserted(self):
        from app.services.ingestion.opensanctions import ingest_opensanctions

        company = _mock_company("Rosneft Oil Company")
        entities = [_make_entity()]
        matches = [(company, entities)]
        session = _make_ingest_session(existing_event=None)

        with (
            patch("app.services.ingestion.opensanctions.download_sanctions_csv"),
            patch("app.services.ingestion.opensanctions.parse_sanctions_csv", return_value=entities),
            patch("app.services.ingestion.opensanctions.match_companies", return_value=matches),
        ):
            result = ingest_opensanctions(session=session, high_concentration_geos=[])

        assert result["company_events_inserted"] == 1
        assert result["company_events_skipped_existing"] == 0
        assert result["companies_matched"] == 1

    def test_company_event_skipped_when_existing(self):
        """When content_hash already exists, company event must be skipped."""
        from app.services.ingestion.opensanctions import ingest_opensanctions

        company = _mock_company("Rosneft Oil Company")
        entities = [_make_entity()]
        matches = [(company, entities)]
        existing_event = MagicMock()
        session = _make_ingest_session(existing_event=existing_event)

        with (
            patch("app.services.ingestion.opensanctions.download_sanctions_csv"),
            patch("app.services.ingestion.opensanctions.parse_sanctions_csv", return_value=entities),
            patch("app.services.ingestion.opensanctions.match_companies", return_value=matches),
        ):
            result = ingest_opensanctions(session=session, high_concentration_geos=[])

        assert result["company_events_inserted"] == 0
        assert result["company_events_skipped_existing"] == 1

    def test_geography_events_inserted(self):
        """When entities are present for a high-concentration geo, an event is created."""
        from app.services.ingestion.opensanctions import ingest_opensanctions

        entities = [_make_entity(countries=["CN"])]
        session = _make_ingest_session(existing_event=None)

        with (
            patch("app.services.ingestion.opensanctions.download_sanctions_csv"),
            patch("app.services.ingestion.opensanctions.parse_sanctions_csv", return_value=entities),
            patch("app.services.ingestion.opensanctions.match_companies", return_value=[]),
        ):
            result = ingest_opensanctions(session=session, high_concentration_geos=["CN"])

        assert result["geography_events_inserted"] == 1

    def test_geography_event_skipped_for_empty_country(self):
        """A high-concentration geo with zero matching entities produces no event."""
        from app.services.ingestion.opensanctions import ingest_opensanctions

        entities = [_make_entity(countries=["RU"])]
        session = _make_ingest_session(existing_event=None)

        with (
            patch("app.services.ingestion.opensanctions.download_sanctions_csv"),
            patch("app.services.ingestion.opensanctions.parse_sanctions_csv", return_value=entities),
            patch("app.services.ingestion.opensanctions.match_companies", return_value=[]),
        ):
            result = ingest_opensanctions(session=session, high_concentration_geos=["CN"])

        assert result["geography_events_inserted"] == 0

    def test_total_entities_parsed_in_result(self):
        from app.services.ingestion.opensanctions import ingest_opensanctions

        entities = [_make_entity(), _make_entity(name="Acme", opensanctions_id="os-2")]
        session = _make_ingest_session(existing_event=None)

        with (
            patch("app.services.ingestion.opensanctions.download_sanctions_csv"),
            patch("app.services.ingestion.opensanctions.parse_sanctions_csv", return_value=entities),
            patch("app.services.ingestion.opensanctions.match_companies", return_value=[]),
        ):
            result = ingest_opensanctions(session=session, high_concentration_geos=[])

        assert result["total_entities_parsed"] == 2

    def test_session_commit_called(self):
        from app.services.ingestion.opensanctions import ingest_opensanctions

        session = _make_ingest_session()

        with (
            patch("app.services.ingestion.opensanctions.download_sanctions_csv"),
            patch("app.services.ingestion.opensanctions.parse_sanctions_csv", return_value=[]),
            patch("app.services.ingestion.opensanctions.match_companies", return_value=[]),
        ):
            ingest_opensanctions(session=session, high_concentration_geos=[])

        session.commit.assert_called_once()

    def test_default_high_concentration_geos_used_when_none(self):
        """When high_concentration_geos=None, the default list should be applied."""
        from app.services.ingestion.opensanctions import (
            _DEFAULT_HIGH_CONCENTRATION_GEOS,
            ingest_opensanctions,
        )

        entities = [_make_entity(countries=[geo]) for geo in _DEFAULT_HIGH_CONCENTRATION_GEOS]
        session = _make_ingest_session(existing_event=None)

        with (
            patch("app.services.ingestion.opensanctions.download_sanctions_csv"),
            patch("app.services.ingestion.opensanctions.parse_sanctions_csv", return_value=entities),
            patch("app.services.ingestion.opensanctions.match_companies", return_value=[]),
        ):
            result = ingest_opensanctions(session=session, high_concentration_geos=None)

        assert result["geography_events_inserted"] == len(_DEFAULT_HIGH_CONCENTRATION_GEOS)
