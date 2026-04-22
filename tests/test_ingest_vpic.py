"""Tests for app/services/ingestion/ingest_vpic.py"""

from __future__ import annotations

import uuid
from datetime import date
from unittest.mock import MagicMock, patch

import pytest

from app.services.ingestion.ingest_vpic import (
    CURRENT_YEAR,
    DATA_SOURCE,
    OEM_MAKE_MAP,
    derive_year_windows,
    get_models_for_make,
    get_models_for_make_year,
    upsert_models,
)


# ---------------------------------------------------------------------------
# OEM_MAKE_MAP validation
# ---------------------------------------------------------------------------


class TestOemMakeMap:
    def test_all_values_are_lists(self):
        for company, makes in OEM_MAKE_MAP.items():
            assert isinstance(makes, list), f"{company} has non-list value"

    def test_known_oems_present(self):
        required = {
            "Tesla",
            "Volkswagen Group",
            "BMW Group",
            "General Motors",
            "Ford Motor Company",
            "Rivian Automotive",
        }
        assert required.issubset(OEM_MAKE_MAP.keys())

    def test_chinese_brands_skipped(self):
        """Geely and Zeekr have no vPIC presence — they must map to empty lists."""
        assert OEM_MAKE_MAP["Geely Auto Group"] == []
        assert OEM_MAKE_MAP["Zeekr"] == []

    def test_make_names_are_uppercase_strings(self):
        for company, makes in OEM_MAKE_MAP.items():
            for make in makes:
                assert isinstance(make, str), f"Non-string make in {company}"
                assert make == make.upper(), f"Make '{make}' for {company} is not uppercase"


# ---------------------------------------------------------------------------
# derive_year_windows
# ---------------------------------------------------------------------------


class TestDeriveYearWindows:
    def _make_presence(self, model_in_years: dict[str, list[int]], year_range: range) -> dict:
        """Helper: returns a fake year_presence dict for patching."""
        return {
            year: {m for m, years in model_in_years.items() if year in years}
            for year in year_range
        }

    def test_model_present_all_years_is_active(self):
        """Model present every year → year_end is None (still active)."""
        year_range = range(2020, 2024)  # 2020-2023

        fake_results: dict[int, set[str]] = {y: {"Model 3"} for y in year_range}

        with patch(
            "app.services.ingestion.ingest_vpic.get_models_for_make_year",
            side_effect=lambda make, year, **kw: fake_results[year],
        ):
            windows = derive_year_windows("TESLA", ["Model 3"], 2020, 2023)

        assert "Model 3" in windows
        start, end = windows["Model 3"]
        assert start == 2020
        assert end is None  # present in latest scanned year → still active

    def test_model_ends_before_last_year(self):
        """Model disappears before the scan end → year_end is set."""
        fake_results: dict[int, set[str]] = {
            2020: {"Roadster"},
            2021: {"Roadster"},
            2022: set(),
            2023: set(),
        }

        with patch(
            "app.services.ingestion.ingest_vpic.get_models_for_make_year",
            side_effect=lambda make, year, **kw: fake_results[year],
        ):
            windows = derive_year_windows("TESLA", ["Roadster"], 2020, 2023)

        start, end = windows["Roadster"]
        assert start == 2020
        assert end == 2021

    def test_model_never_seen_returns_none_none(self):
        """Model in make list but never found in year scan → (None, None)."""
        fake_results: dict[int, set[str]] = {y: set() for y in range(2020, 2024)}

        with patch(
            "app.services.ingestion.ingest_vpic.get_models_for_make_year",
            side_effect=lambda make, year, **kw: fake_results[year],
        ):
            windows = derive_year_windows("TESLA", ["Cybertruck"], 2020, 2023)

        assert windows["Cybertruck"] == (None, None)

    def test_model_starts_mid_range(self):
        """Model appears partway through the scan → correct start year."""
        fake_results: dict[int, set[str]] = {
            2020: set(),
            2021: set(),
            2022: {"Model Y"},
            2023: {"Model Y"},
        }

        with patch(
            "app.services.ingestion.ingest_vpic.get_models_for_make_year",
            side_effect=lambda make, year, **kw: fake_results[year],
        ):
            windows = derive_year_windows("TESLA", ["Model Y"], 2020, 2023)

        start, end = windows["Model Y"]
        assert start == 2022
        assert end is None  # present in 2023 (last year) → still active

    def test_multiple_models_independent(self):
        """Multiple models are processed independently."""
        fake_results: dict[int, set[str]] = {
            2020: {"Model S", "Model 3"},
            2021: {"Model S", "Model 3"},
            2022: {"Model 3"},  # Model S discontinued
            2023: {"Model 3"},
        }

        with patch(
            "app.services.ingestion.ingest_vpic.get_models_for_make_year",
            side_effect=lambda make, year, **kw: fake_results[year],
        ):
            windows = derive_year_windows("TESLA", ["Model S", "Model 3"], 2020, 2023)

        s_start, s_end = windows["Model S"]
        assert s_start == 2020
        assert s_end == 2021

        y_start, y_end = windows["Model 3"]
        assert y_start == 2020
        assert y_end is None


# ---------------------------------------------------------------------------
# get_models_for_make (mocked HTTP)
# ---------------------------------------------------------------------------


class TestGetModelsForMake:
    def test_returns_model_list(self):
        fake_results = [
            {"Make_ID": 441, "Make_Name": "TESLA", "Model_ID": 1685, "Model_Name": "Model S"},
            {"Make_ID": 441, "Make_Name": "TESLA", "Model_ID": 17834, "Model_Name": "Model 3"},
        ]
        with patch(
            "app.services.ingestion.ingest_vpic._fetch_json",
            return_value=fake_results,
        ):
            result = get_models_for_make("TESLA")

        assert len(result) == 2
        assert result[0]["Model_Name"] == "Model S"

    def test_empty_make_returns_empty_list(self):
        with patch("app.services.ingestion.ingest_vpic._fetch_json", return_value=[]):
            result = get_models_for_make("UNKNOWNMAKE")
        assert result == []


class TestGetModelsForMakeYear:
    def test_returns_set_of_model_names(self):
        fake_results = [
            {"Model_Name": "Model 3"},
            {"Model_Name": "Model Y"},
        ]
        with patch(
            "app.services.ingestion.ingest_vpic._fetch_json",
            return_value=fake_results,
        ):
            result = get_models_for_make_year("TESLA", 2023)

        assert result == {"Model 3", "Model Y"}

    def test_missing_model_name_is_filtered(self):
        fake_results = [{"Model_Name": "Model S"}, {"Model_ID": 999}]
        with patch("app.services.ingestion.ingest_vpic._fetch_json", return_value=fake_results):
            result = get_models_for_make_year("TESLA", 2023)
        assert result == {"Model S"}


# ---------------------------------------------------------------------------
# upsert_models
# ---------------------------------------------------------------------------


class TestUpsertModels:
    def _make_company(self) -> MagicMock:
        company = MagicMock()
        company.id = uuid.uuid4()
        company.canonical_name = "Tesla"
        return company

    def test_inserts_new_row(self):
        session = MagicMock()
        session.no_autoflush = MagicMock(__enter__=MagicMock(return_value=None), __exit__=MagicMock(return_value=False))
        session.scalar.return_value = None  # no existing row

        company = self._make_company()
        windows = {"Model 3": (2017, None)}
        model_id_map = {"Model 3": 17834}

        ins, upd, skp = upsert_models(
            session, company, "TESLA", 441, windows, model_id_map, dry_run=False
        )

        assert ins == 1
        assert upd == 0
        assert skp == 0
        session.add.assert_called_once()

        added = session.add.call_args[0][0]
        assert added.model_name == "Model 3"
        assert added.model_year_start == 2017
        assert added.model_year_end is None
        assert added.is_active is True
        assert added.data_source == DATA_SOURCE
        assert added.metadata_json["vpic_make_name"] == "TESLA"
        assert added.metadata_json["vpic_model_id"] == 17834

    def test_skips_none_year_start(self):
        """Models where year_start is None (never seen in year scan) are skipped."""
        session = MagicMock()
        session.no_autoflush = MagicMock(__enter__=MagicMock(return_value=None), __exit__=MagicMock(return_value=False))

        company = self._make_company()
        windows = {"Ghost Model": (None, None)}
        ins, upd, skp = upsert_models(
            session, company, "TESLA", 441, windows, {}, dry_run=False
        )

        assert ins == 0
        assert skp == 1
        session.add.assert_not_called()
        session.scalar.assert_not_called()

    def test_dry_run_does_not_call_add(self):
        session = MagicMock()
        session.no_autoflush = MagicMock(__enter__=MagicMock(return_value=None), __exit__=MagicMock(return_value=False))
        session.scalar.return_value = None

        company = self._make_company()
        ins, upd, skp = upsert_models(
            session, company, "TESLA", 441, {"Model S": (2012, None)}, {"Model S": 1685},
            dry_run=True,
        )

        assert ins == 1
        session.add.assert_not_called()

    def test_updates_existing_row_when_year_end_changes(self):
        existing = MagicMock()
        existing.model_year_end = None
        existing.is_active = True
        existing.metadata_json = {}
        existing.data_source = DATA_SOURCE

        session = MagicMock()
        session.no_autoflush = MagicMock(__enter__=MagicMock(return_value=None), __exit__=MagicMock(return_value=False))
        session.scalar.return_value = existing

        company = self._make_company()
        # Simulate model now showing year_end = 2023 (discontinued)
        ins, upd, skp = upsert_models(
            session, company, "TESLA", 441,
            {"Model S": (2012, 2023)}, {"Model S": 1685},
            dry_run=False,
        )

        assert ins == 0
        assert upd == 1
        assert skp == 0
        assert existing.model_year_end == 2023
        assert existing.is_active is False

    def test_skips_unchanged_row(self):
        existing = MagicMock()
        existing.model_year_end = None
        existing.is_active = True
        existing.metadata_json = {
            "vpic_make_id": 441,
            "vpic_model_id": 1685,
            "vpic_make_name": "TESLA",
        }
        existing.data_source = DATA_SOURCE

        session = MagicMock()
        session.no_autoflush = MagicMock(__enter__=MagicMock(return_value=None), __exit__=MagicMock(return_value=False))
        session.scalar.return_value = existing

        company = self._make_company()
        ins, upd, skp = upsert_models(
            session, company, "TESLA", 441,
            {"Model S": (2012, None)}, {"Model S": 1685},
            dry_run=False,
        )

        assert ins == 0
        assert upd == 0
        assert skp == 1

    def test_preserves_existing_ev_database_metadata(self):
        """vPIC upsert must not clobber chemistry keys set by the ev-database scraper."""
        existing = MagicMock()
        existing.model_year_end = None
        existing.is_active = True
        existing.metadata_json = {
            "ev_database_id": "tesla-model3-lr",
            "useable_battery_kwh": 75.0,
            "chemistry_raw": "NMC",
        }
        existing.data_source = "ev-database.org"

        session = MagicMock()
        session.no_autoflush = MagicMock(__enter__=MagicMock(return_value=None), __exit__=MagicMock(return_value=False))
        session.scalar.return_value = existing

        company = self._make_company()
        upsert_models(
            session, company, "TESLA", 441,
            {"Model 3": (2017, None)}, {"Model 3": 17834},
            dry_run=False,
        )

        merged = existing.metadata_json
        # ev-database keys must still be present
        assert merged["ev_database_id"] == "tesla-model3-lr"
        assert merged["useable_battery_kwh"] == 75.0
        # vPIC keys must also be present
        assert merged["vpic_make_name"] == "TESLA"
