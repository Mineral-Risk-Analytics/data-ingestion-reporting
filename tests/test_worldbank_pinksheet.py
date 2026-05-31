"""Tests for the World Bank Pink Sheet ingestion module.

All tests are self-contained — no real HTTP calls and no live database.
``parse_pink_sheet`` is tested with in-memory openpyxl workbooks.
``ingest_pink_sheet`` is tested with a mocked ``download_pink_sheet`` and a
mocked SQLAlchemy session.
"""

from __future__ import annotations

import io
from datetime import date
from typing import Any
from unittest.mock import MagicMock, call, patch

import openpyxl
import pytest

from app.services.ingestion.material_resolver import ResolveResult
from app.services.ingestion.worldbank_pinksheet import (
    _HEADER_TO_HS_PREFIX,
    _normalise_unit,
    parse_pink_sheet,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_workbook(
    commodities: list[str],
    units: list[str],
    data_rows: list[tuple[str, list[Any]]],
    sheet_name: str = "Monthly Prices",
) -> bytes:
    """Return Pink Sheet-shaped Excel bytes using an in-memory openpyxl workbook.

    Args:
        commodities: Column headers for row 5 (excluding the date column A).
        units:       Unit strings for row 6 (same indexing as commodities).
        data_rows:   List of ``(date_str, [price_or_None, ...])`` for rows 7+.
        sheet_name:  Worksheet name (default matches production sheet name).
    """
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet_name

    # Rows 1–4: metadata (arbitrary content, should be ignored by the parser)
    for row_num in range(1, 5):
        ws.cell(row=row_num, column=1, value=f"metadata row {row_num}")

    # Row 5: commodity names (column A left blank — it is the date column)
    ws.cell(row=5, column=1, value=None)
    for col_offset, name in enumerate(commodities, start=2):
        ws.cell(row=5, column=col_offset, value=name)

    # Row 6: units
    ws.cell(row=6, column=1, value=None)
    for col_offset, unit in enumerate(units, start=2):
        ws.cell(row=6, column=col_offset, value=unit)

    # Rows 7+: data
    for row_offset, (date_str, prices) in enumerate(data_rows, start=7):
        ws.cell(row=row_offset, column=1, value=date_str)
        for col_offset, price in enumerate(prices, start=2):
            ws.cell(row=row_offset, column=col_offset, value=price)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Unit normalisation
# ---------------------------------------------------------------------------

class TestNormaliseUnit:
    def test_dollars_per_mt(self):
        assert _normalise_unit("$/mt") == "per_mt"

    def test_dollars_per_kg(self):
        assert _normalise_unit("$/kg") == "per_kg"

    def test_dollars_per_troy_oz(self):
        assert _normalise_unit("$/troy oz") == "per_troy_oz"

    def test_unknown_unit_kept_as_is(self):
        assert _normalise_unit("$/mmbtu") == "$/mmbtu"

    def test_strips_whitespace(self):
        assert _normalise_unit("  $/mt  ") == "per_mt"

    def test_empty_string(self):
        assert _normalise_unit("") == "unknown"


# ---------------------------------------------------------------------------
# parse_pink_sheet — happy path
# ---------------------------------------------------------------------------

class TestParsePinkSheet:
    def _simple_workbook(self) -> bytes:
        """Two commodities, two data rows — all values valid."""
        return _build_workbook(
            commodities=["Cobalt", "Copper"],
            units=["$/mt", "$/mt"],
            data_rows=[
                ("2024M01", [33_000.0, 8_500.0]),
                ("2024M02", [34_000.0, 8_600.0]),
            ],
        )

    def test_returns_list_of_dicts(self):
        results = parse_pink_sheet(self._simple_workbook())
        assert isinstance(results, list)
        assert all(isinstance(r, dict) for r in results)

    def test_correct_observation_count(self):
        # 2 commodities × 2 rows = 4 observations
        results = parse_pink_sheet(self._simple_workbook())
        assert len(results) == 4

    def test_date_parsing_january(self):
        results = parse_pink_sheet(self._simple_workbook())
        dates = {r["price_date"] for r in results}
        assert date(2024, 1, 1) in dates

    def test_date_parsing_february(self):
        results = parse_pink_sheet(self._simple_workbook())
        dates = {r["price_date"] for r in results}
        assert date(2024, 2, 1) in dates

    def test_date_day_is_always_one(self):
        results = parse_pink_sheet(self._simple_workbook())
        assert all(r["price_date"].day == 1 for r in results)

    def test_unit_normalised(self):
        results = parse_pink_sheet(self._simple_workbook())
        assert all(r["price_unit"] == "per_mt" for r in results)

    def test_raw_unit_preserved(self):
        results = parse_pink_sheet(self._simple_workbook())
        assert all(r["raw_unit"] == "$/mt" for r in results)

    def test_commodity_name_in_result(self):
        results = parse_pink_sheet(self._simple_workbook())
        commodities = {r["commodity"] for r in results}
        assert commodities == {"Cobalt", "Copper"}

    def test_price_values_correct(self):
        results = parse_pink_sheet(self._simple_workbook())
        cobalt_jan = next(
            r for r in results
            if r["commodity"] == "Cobalt" and r["price_date"] == date(2024, 1, 1)
        )
        assert cobalt_jan["price_usd"] == pytest.approx(33_000.0)

    def test_lithium_carbonate_full_header(self):
        """Full commodity header name as it appears in the Pink Sheet."""
        raw = _build_workbook(
            commodities=["Lithium carbonate, battery grade"],
            units=["$/mt"],
            data_rows=[("2023M06", [25_000.0])],
        )
        results = parse_pink_sheet(raw)
        assert len(results) == 1
        assert results[0]["commodity"] == "Lithium carbonate, battery grade"

    def test_aluminium_british_spelling(self):
        """Pink Sheet sometimes uses 'Aluminium'; must still be accepted."""
        raw = _build_workbook(
            commodities=["Aluminium"],
            units=["$/mt"],
            data_rows=[("2022M03", [2_700.0])],
        )
        results = parse_pink_sheet(raw)
        assert len(results) == 1
        assert results[0]["commodity"] == "Aluminium"

    def test_troy_oz_unit(self):
        raw = _build_workbook(
            commodities=["Cobalt"],
            units=["$/troy oz"],
            data_rows=[("2024M01", [15.5])],
        )
        results = parse_pink_sheet(raw)
        assert results[0]["price_unit"] == "per_troy_oz"


# ---------------------------------------------------------------------------
# parse_pink_sheet — skipping bad / irrelevant rows
# ---------------------------------------------------------------------------

class TestParsePinkSheetSkipping:
    def test_skips_unknown_commodity(self):
        """With ``accepted_headers`` excluding workbook columns, parse returns []."""
        raw = _build_workbook(
            commodities=["Natural Gas", "Coal"],
            units=["$/mmbtu", "$/mt"],
            data_rows=[("2024M01", [3.5, 120.0])],
        )
        results = parse_pink_sheet(raw, accepted_headers={"Cobalt", "Nickel"})
        assert results == []

    def test_skips_empty_price_cell(self):
        """A None price cell must produce no result for that observation."""
        raw = _build_workbook(
            commodities=["Cobalt"],
            units=["$/mt"],
            data_rows=[("2024M01", [None])],
        )
        assert parse_pink_sheet(raw) == []

    def test_skips_non_numeric_price(self):
        """A non-numeric price string must be skipped with a warning, not raise."""
        raw = _build_workbook(
            commodities=["Cobalt"],
            units=["$/mt"],
            data_rows=[("2024M01", ["n.a."])],
        )
        assert parse_pink_sheet(raw) == []

    def test_skips_zero_price(self):
        """Zero prices are not meaningful — skip them."""
        raw = _build_workbook(
            commodities=["Cobalt"],
            units=["$/mt"],
            data_rows=[("2024M01", [0.0])],
        )
        assert parse_pink_sheet(raw) == []

    def test_skips_negative_price(self):
        raw = _build_workbook(
            commodities=["Cobalt"],
            units=["$/mt"],
            data_rows=[("2024M01", [-1.0])],
        )
        assert parse_pink_sheet(raw) == []

    def test_skips_bad_date_row(self):
        """Rows where column A is not a parseable date string are skipped."""
        raw = _build_workbook(
            commodities=["Cobalt"],
            units=["$/mt"],
            data_rows=[
                ("NOT_A_DATE", [33_000.0]),
                ("2024M01", [34_000.0]),
            ],
        )
        results = parse_pink_sheet(raw)
        # Only the valid row is returned
        assert len(results) == 1
        assert results[0]["price_date"] == date(2024, 1, 1)

    def test_mixed_valid_and_invalid(self):
        """Valid and invalid cells: missing Cobalt price skipped; Gas kept if not filtered."""
        raw = _build_workbook(
            commodities=["Cobalt", "Natural Gas"],
            units=["$/mt", "$/mmbtu"],
            data_rows=[
                ("2024M01", [33_000.0, 3.5]),
                ("2024M02", [None, 3.6]),  # Cobalt missing → skip that cell
            ],
        )
        results = parse_pink_sheet(raw, accepted_headers={"Cobalt"})
        assert len(results) == 1
        assert results[0]["commodity"] == "Cobalt"
        assert results[0]["price_date"] == date(2024, 1, 1)

    def test_metadata_rows_ignored(self):
        """Rows 1–4 are metadata and must never appear in results."""
        raw = _build_workbook(
            commodities=["Cobalt"],
            units=["$/mt"],
            data_rows=[("2024M01", [33_000.0])],
        )
        results = parse_pink_sheet(raw)
        # Metadata would parse as bad date strings; none should appear
        assert all(isinstance(r["price_date"], date) for r in results)

    def test_empty_workbook_returns_empty_list(self):
        """A workbook with only header rows and no data rows returns []."""
        raw = _build_workbook(
            commodities=["Cobalt"],
            units=["$/mt"],
            data_rows=[],
        )
        assert parse_pink_sheet(raw) == []


# ---------------------------------------------------------------------------
# ingest_pink_sheet — integration via mocked session
# ---------------------------------------------------------------------------

def _make_bytes_fixture() -> bytes:
    """Minimal valid workbook: two Cobalt observations."""
    return _build_workbook(
        commodities=["Cobalt"],
        units=["$/mt"],
        data_rows=[
            ("2024M01", [33_000.0]),
            ("2024M02", [34_000.0]),
        ],
    )


def _mock_cobalt_material(material_id: int = 1) -> MagicMock:
    m = MagicMock()
    m.id = material_id
    m.canonical_name = "Cobalt"
    return m


def _alias_resolver_mock(material_id: int = 1) -> MagicMock:
    """MaterialAliasResolver-compatible mock: Cobalt header resolves; cache keys lowercased."""
    mat = _mock_cobalt_material(material_id)
    resolver = MagicMock()
    resolver._cache = {"worldbank_pinksheet": {"cobalt": (mat, False, True)}}

    def _resolve(_ss: str, header: str) -> ResolveResult:
        if header.strip().lower() == "cobalt":
            return ResolveResult(material=mat, status="ok", writes_material_signals=True)
        return ResolveResult(material=None, status="unknown")

    resolver.resolve.side_effect = _resolve
    return resolver


def _mock_session(existing: bool = False) -> MagicMock:
    """Session mock: ``scalar`` answers CommodityPrice existence checks only."""
    price_return = MagicMock() if existing else None
    session = MagicMock()
    session.scalar.return_value = price_return
    return session


class TestIngestPinkSheet:
    def test_inserted_count_matches_observations(self):
        from app.services.ingestion import worldbank_pinksheet as wbp
        from app.services.ingestion.worldbank_pinksheet import ingest_pink_sheet

        fixture_bytes = _make_bytes_fixture()
        session = _mock_session(existing=False)

        with (
            patch.object(wbp, "_days_since_last_run", return_value=None),
            patch.object(wbp, "MaterialAliasResolver", return_value=_alias_resolver_mock(1)),
            patch.object(wbp, "MaterialResolver") as mock_hs,
            patch(
                "app.services.ingestion.worldbank_pinksheet.download_pink_sheet",
                return_value=fixture_bytes,
            ),
        ):
            mock_hs.return_value.resolve_by_hs_code.return_value = (1, 99, 0.9)
            result = ingest_pink_sheet(session=session, since_year=None)

        assert result["inserted"] == 2
        assert result["skipped_existing"] == 0
        assert result["skipped_unknown_material"] == 0

    def test_idempotent_second_run(self):
        """Re-running when all rows already exist must insert 0 and skip all."""
        from app.services.ingestion import worldbank_pinksheet as wbp
        from app.services.ingestion.worldbank_pinksheet import ingest_pink_sheet

        fixture_bytes = _make_bytes_fixture()
        session = _mock_session(existing=True)

        with (
            patch.object(wbp, "_days_since_last_run", return_value=None),
            patch.object(wbp, "MaterialAliasResolver", return_value=_alias_resolver_mock(1)),
            patch.object(wbp, "MaterialResolver") as mock_hs,
            patch(
                "app.services.ingestion.worldbank_pinksheet.download_pink_sheet",
                return_value=fixture_bytes,
            ),
        ):
            mock_hs.return_value.resolve_by_hs_code.return_value = (1, 99, 0.9)
            result = ingest_pink_sheet(session=session, since_year=None)

        assert result["inserted"] == 0
        assert result["skipped_existing"] == 2

    def test_since_year_filters_observations(self):
        """since_year should drop rows before that year."""
        from app.services.ingestion import worldbank_pinksheet as wbp
        from app.services.ingestion.worldbank_pinksheet import ingest_pink_sheet

        # Workbook has 2023 and 2024 rows
        raw = _build_workbook(
            commodities=["Cobalt"],
            units=["$/mt"],
            data_rows=[
                ("2023M12", [30_000.0]),
                ("2024M01", [33_000.0]),
            ],
        )
        session = _mock_session(existing=False)

        with (
            patch.object(wbp, "_days_since_last_run", return_value=None),
            patch.object(wbp, "MaterialAliasResolver", return_value=_alias_resolver_mock(1)),
            patch.object(wbp, "MaterialResolver") as mock_hs,
            patch(
                "app.services.ingestion.worldbank_pinksheet.download_pink_sheet",
                return_value=raw,
            ),
        ):
            mock_hs.return_value.resolve_by_hs_code.return_value = (1, 99, 0.9)
            result = ingest_pink_sheet(session=session, since_year=2024)

        # Only the 2024 row should be inserted
        assert result["inserted"] == 1

    def test_unknown_material_increments_skip_counter(self):
        """When no Pink Sheet aliases are seeded, ingest inserts nothing."""
        from app.services.ingestion import worldbank_pinksheet as wbp
        from app.services.ingestion.worldbank_pinksheet import ingest_pink_sheet

        raw = _build_workbook(
            commodities=["Cobalt"],
            units=["$/mt"],
            data_rows=[("2024M01", [33_000.0])],
        )
        session = _mock_session(existing=False)
        empty_resolver = MagicMock()
        empty_resolver._cache = {"worldbank_pinksheet": {}}

        with (
            patch.object(wbp, "_days_since_last_run", return_value=None),
            patch.object(wbp, "MaterialAliasResolver", return_value=empty_resolver),
            patch(
                "app.services.ingestion.worldbank_pinksheet.download_pink_sheet",
                return_value=raw,
            ),
        ):
            result = ingest_pink_sheet(session=session, since_year=None)

        assert result["inserted"] == 0

    def test_session_add_called_for_each_inserted_row(self):
        """session.add must be called once per inserted CommodityPrice."""
        from app.services.ingestion import worldbank_pinksheet as wbp
        from app.services.ingestion.worldbank_pinksheet import ingest_pink_sheet

        fixture_bytes = _make_bytes_fixture()
        session = _mock_session(existing=False)

        with (
            patch.object(wbp, "_days_since_last_run", return_value=None),
            patch.object(wbp, "MaterialAliasResolver", return_value=_alias_resolver_mock(7)),
            patch.object(wbp, "MaterialResolver") as mock_hs,
            patch(
                "app.services.ingestion.worldbank_pinksheet.download_pink_sheet",
                return_value=fixture_bytes,
            ),
        ):
            mock_hs.return_value.resolve_by_hs_code.return_value = (7, 99, 0.9)
            ingest_pink_sheet(session=session, since_year=None)

        assert session.add.call_count == 2

    def test_session_commit_called(self):
        from app.services.ingestion import worldbank_pinksheet as wbp
        from app.services.ingestion.worldbank_pinksheet import ingest_pink_sheet

        fixture_bytes = _make_bytes_fixture()
        session = _mock_session(existing=False)

        with (
            patch.object(wbp, "_days_since_last_run", return_value=None),
            patch.object(wbp, "MaterialAliasResolver", return_value=_alias_resolver_mock(1)),
            patch.object(wbp, "MaterialResolver") as mock_hs,
            patch(
                "app.services.ingestion.worldbank_pinksheet.download_pink_sheet",
                return_value=fixture_bytes,
            ),
        ):
            mock_hs.return_value.resolve_by_hs_code.return_value = (1, 99, 0.9)
            ingest_pink_sheet(session=session, since_year=None)

        session.commit.assert_called_once()

    def test_download_called_with_url_override(self):
        """The url parameter must be forwarded to download_pink_sheet."""
        from app.services.ingestion import worldbank_pinksheet as wbp
        from app.services.ingestion.worldbank_pinksheet import ingest_pink_sheet

        fixture_bytes = _make_bytes_fixture()
        session = _mock_session(existing=False)
        custom_url = "https://example.com/custom-sheet.xlsx"

        with (
            patch.object(wbp, "_days_since_last_run", return_value=None),
            patch.object(wbp, "MaterialAliasResolver", return_value=_alias_resolver_mock(1)),
            patch.object(wbp, "MaterialResolver") as mock_hs,
            patch(
                "app.services.ingestion.worldbank_pinksheet.download_pink_sheet",
                return_value=fixture_bytes,
            ) as mock_dl,
        ):
            mock_hs.return_value.resolve_by_hs_code.return_value = (1, 99, 0.9)
            ingest_pink_sheet(session=session, url=custom_url, since_year=None)

        mock_dl.assert_called_once_with(custom_url)


# ---------------------------------------------------------------------------
# Stage attribution: Pink Sheet header → HS prefix (ingest-time)
# ---------------------------------------------------------------------------

class TestHeaderToHsPrefix:
    """Headers listed here get ``hs_mapping_id`` when the prefix resolves in the DB."""

    def test_cobalt_maps_to_refined_prefix(self):
        assert _HEADER_TO_HS_PREFIX["Cobalt"] == "810520"

    def test_aluminium_maps_to_same_prefix_as_aluminum(self):
        assert _HEADER_TO_HS_PREFIX["Aluminium"] == "760110"

    def test_lithium_carbonate_battery_grade(self):
        assert _HEADER_TO_HS_PREFIX["Lithium carbonate, battery grade"] == "283691"

    def test_bare_graphite_not_attributed(self):
        assert "Graphite" not in _HEADER_TO_HS_PREFIX
