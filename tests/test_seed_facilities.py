"""Tests for app/services/ingestion/seed_facilities.py"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, call

from app.services.ingestion import seed_facilities as seed_facilities_module


def test_inserts_new_facility(monkeypatch):
    entry = {
        "company_canonical_name": "TestCo",
        "facility_type": "cell_factory",
        "country": "US",
        "city": "Austin",
        "status": "operating",
        "capacity_notes": "10 GWh",
        "latitude": 30.2672,
        "longitude": -97.7431,
        "data_source": "manual",
    }
    monkeypatch.setattr(seed_facilities_module, "_FACILITIES", [entry])

    company = MagicMock()
    company.id = "company-1"

    session = MagicMock()
    session.scalar.side_effect = [company, None, None]

    result = seed_facilities_module.seed_facilities(session)

    assert result == {
        "inserted": 1,
        "updated": 0,
        "skipped": 0,
        "companies_not_found": 0,
    }
    assert session.add.call_count == 2
    session.commit.assert_called_once()


def test_skips_existing_facility_when_no_mutable_changes(monkeypatch):
    entry = {
        "company_canonical_name": "TestCo",
        "facility_type": "cell_factory",
        "country": "US",
        "city": "Austin",
        "status": "operating",
        "capacity_notes": "10 GWh",
        "latitude": 30.2672,
        "longitude": -97.7431,
        "data_source": "manual",
    }
    monkeypatch.setattr(seed_facilities_module, "_FACILITIES", [entry])

    company = MagicMock()
    company.id = "company-1"
    existing = MagicMock()
    existing.status = "operating"
    existing.capacity_notes = "10 GWh"
    existing.latitude = 30.2672
    existing.longitude = -97.7431
    existing.data_source = "manual"
    existing.created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    existing_link = MagicMock()

    session = MagicMock()
    session.scalar.side_effect = [company, existing, existing_link]

    result = seed_facilities_module.seed_facilities(session)

    assert result == {
        "inserted": 0,
        "updated": 0,
        "skipped": 1,
        "companies_not_found": 0,
    }
    assert existing.created_at == datetime(2026, 1, 1, tzinfo=timezone.utc)
    session.add.assert_not_called()


def test_partially_updates_existing_facility_and_logs_changed_fields(monkeypatch):
    entry = {
        "company_canonical_name": "TestCo",
        "facility_type": "cell_factory",
        "country": "US",
        "city": "Austin",
        "status": "under_construction",
        "capacity_notes": "20 GWh",
        "latitude": 30.2000,
        "longitude": -97.7000,
        "data_source": "manual",
    }
    monkeypatch.setattr(seed_facilities_module, "_FACILITIES", [entry])

    company = MagicMock()
    company.id = "company-1"
    existing = MagicMock()
    existing.status = "operating"
    existing.capacity_notes = "10 GWh"
    existing.latitude = 30.2672
    existing.longitude = -97.7431
    existing.data_source = "legacy"
    existing.created_at = datetime(2025, 1, 1, tzinfo=timezone.utc)

    session = MagicMock()
    session.scalar.side_effect = [company, existing, None]

    mock_log = MagicMock()
    monkeypatch.setattr(seed_facilities_module, "log", mock_log)

    result = seed_facilities_module.seed_facilities(session)

    assert result == {
        "inserted": 1,
        "updated": 0,
        "skipped": 0,
        "companies_not_found": 0,
    }
    assert existing.status == "under_construction"
    assert existing.capacity_notes == "20 GWh"
    assert existing.latitude == 30.2000
    assert existing.longitude == -97.7000
    assert existing.data_source == "manual"
    assert existing.created_at == datetime(2025, 1, 1, tzinfo=timezone.utc)

    assert call(
        "seed_facilities.facility_updated",
        facility_type="cell_factory",
        country="US",
        city="Austin",
        changed_fields=["status", "capacity_notes", "latitude", "longitude", "data_source"],
    ) in mock_log.info.call_args_list


def test_company_not_found_increments_counter(monkeypatch):
    entry = {
        "company_canonical_name": "MissingCo",
        "facility_type": "cell_factory",
        "country": "US",
        "city": "Austin",
    }
    monkeypatch.setattr(seed_facilities_module, "_FACILITIES", [entry])

    session = MagicMock()
    session.scalar.side_effect = [None]

    result = seed_facilities_module.seed_facilities(session)

    assert result == {
        "inserted": 0,
        "updated": 0,
        "skipped": 0,
        "companies_not_found": 1,
    }
