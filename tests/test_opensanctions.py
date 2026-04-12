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
    _build_name_index,
    _content_hash,
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
    fieldnames = ["id", "schema", "name", "aliases", "birth_date", "countries", "datasets", "first_seen", "last_seen"]
    import csv as _csv
    buf = io.BytesIO()
    wrapper = io.TextIOWrapper(buf, encoding="utf-8", newline="")
    writer = _csv.DictWriter(wrapper, fieldnames=fieldnames)
    writer.writeheader()
    for row in rows:
        full_row = {f: row.get(f, "") for f in fieldnames}
        writer.writerow(full_row)
    wrapper.flush()
    # Detach before wrapper leaves scope — prevents it from closing buf.
    # Capture the bytes first so the returned BytesIO is always at position 0.
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
        "datasets": "us_ofac_sdn",
        "first_seen": "2023-01-01",
        "last_seen": "2024-01-01",
    },
]


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
        assert len(results[0]["aliases"]) == 1  # all three normalise to "acme"

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
                "countries": ["RU"],
                "datasets": ["us_ofac_sdn"],
                "first_seen": None,
                "last_seen": None,
            },
            {
                "opensanctions_id": "os-2",
                "name": "Glencore International AG",
                "aliases": ["glencore plc", "glen"],
                "countries": ["CH", "CD"],
                "datasets": ["ch_seco_sanctions"],
                "first_seen": None,
                "last_seen": None,
            },
        ]

    def test_primary_name_is_indexed(self):
        index = _build_name_index(self._entities())
        assert "rosneft oil company" in index

    def test_aliases_are_indexed(self):
        index = _build_name_index(self._entities())
        assert "glencore plc" in index
        assert "glen" in index
        assert "pjsc rosneft" in index

    def test_normalisation_lowercases(self):
        entities = [
            {
                "opensanctions_id": "os-upper",
                "name": "UPPER CASE COMPANY",
                "aliases": [],
                "countries": [],
                "datasets": [],
                "first_seen": None,
                "last_seen": None,
            }
        ]
        index = _build_name_index(entities)
        assert "upper case company" in index
        assert "UPPER CASE COMPANY" not in index

    def test_normalisation_collapses_whitespace(self):
        entities = [
            {
                "opensanctions_id": "os-ws",
                "name": "Extra  Spaces  Here",
                "aliases": [],
                "countries": [],
                "datasets": [],
                "first_seen": None,
                "last_seen": None,
            }
        ]
        index = _build_name_index(entities)
        assert "extra spaces here" in index

    def test_multiple_entities_share_same_alias_key(self):
        """Two entities with overlapping aliases should both be in the index list."""
        entities = [
            {
                "opensanctions_id": "os-a",
                "name": "Company A",
                "aliases": ["shared name"],
                "countries": [],
                "datasets": [],
                "first_seen": None,
                "last_seen": None,
            },
            {
                "opensanctions_id": "os-b",
                "name": "Company B",
                "aliases": ["shared name"],
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
# match_companies
# ---------------------------------------------------------------------------

def _mock_company(canonical_name: str) -> MagicMock:
    c = MagicMock()
    c.id = uuid.uuid4()
    c.canonical_name = canonical_name
    return c


def _mock_alias(company_id: uuid.UUID, alias: str) -> MagicMock:
    a = MagicMock()
    a.company_id = company_id
    a.alias = alias
    return a


def _mock_session_for_match(
    companies: list[MagicMock],
    aliases: list[MagicMock],
) -> MagicMock:
    session = MagicMock()

    def scalars_side_effect(stmt):
        # Distinguish Company from CompanyAlias query by inspecting the
        # whereclause entity — use a call-count approach instead.
        result = MagicMock()
        result.all.return_value = scalars_side_effect._calls.pop(0)
        return result

    scalars_side_effect._calls = [companies, aliases]
    session.scalars.side_effect = scalars_side_effect
    return session


class TestMatchCompanies:
    def _entities_glencore(self) -> list[dict]:
        return [
            {
                "opensanctions_id": "os-glen-1",
                "name": "GLENCORE INTERNATIONAL AG",
                "aliases": ["glencore plc"],
                "countries": ["CH", "CD"],
                "datasets": ["ch_seco_sanctions"],
                "first_seen": None,
                "last_seen": None,
            }
        ]

    def test_match_via_alias(self):
        company = _mock_company("Glencore International")
        alias = _mock_alias(company.id, "Glencore PLC")
        session = _mock_session_for_match([company], [alias])

        result = match_companies(session, self._entities_glencore())

        # "Glencore PLC" alias normalises to "glencore plc" which matches the
        # entity alias "glencore plc" → should find a match
        assert len(result) == 1
        matched_company, matched_entities = result[0]
        assert matched_company.canonical_name == "Glencore International"
        assert len(matched_entities) >= 1

    def test_match_via_canonical_name(self):
        company = _mock_company("Glencore International AG")
        session = _mock_session_for_match([company], [])

        result = match_companies(session, self._entities_glencore())

        # canonical name "Glencore International AG" normalises and matches
        # the entity name "GLENCORE INTERNATIONAL AG" (case-insensitive)
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
        alias = _mock_alias(company.id, "Glencore International AG")  # same as canonical
        session = _mock_session_for_match([company], [alias])

        result = match_companies(session, self._entities_glencore())
        assert len(result) == 1
        _, matched_entities = result[0]
        ids = [e["opensanctions_id"] for e in matched_entities]
        assert len(ids) == len(set(ids))


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
    countries: list[str] | None = None,
    datasets: list[str] | None = None,
    opensanctions_id: str = "os-1",
) -> dict:
    return {
        "opensanctions_id": opensanctions_id,
        "name": name,
        "aliases": aliases or [],
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

    # Give the mock event an id attribute so flush() → event.id works
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
        existing_event = MagicMock()  # truthy → already exists
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

        entities = [_make_entity(countries=["RU"])]  # No entities for CN
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

        # One entity per default geo
        entities = [_make_entity(countries=[geo]) for geo in _DEFAULT_HIGH_CONCENTRATION_GEOS]
        session = _make_ingest_session(existing_event=None)

        with (
            patch("app.services.ingestion.opensanctions.download_sanctions_csv"),
            patch("app.services.ingestion.opensanctions.parse_sanctions_csv", return_value=entities),
            patch("app.services.ingestion.opensanctions.match_companies", return_value=[]),
        ):
            result = ingest_opensanctions(session=session, high_concentration_geos=None)

        assert result["geography_events_inserted"] == len(_DEFAULT_HIGH_CONCENTRATION_GEOS)
