"""Tests for app/services/ingestion/seed_companies.py"""

from __future__ import annotations

import collections
import uuid
from unittest.mock import MagicMock

import pytest

from app.services.ingestion.seed_companies import (
    _ALLOWED_STAGES,
    _COMPANIES,
    seed_companies,
)

# Total expected company count (40 original + 35 new)
_EXPECTED_TOTAL = 75

# Companies expected to carry a pre-seeded LEI
_SEEDED_LEI_COMPANIES = {
    "Volkswagen Group": "529900HNOAA1KXQJUQ27",
    "BMW Group": "VGRQXHF3J8VDLUA7XE92",
}

# Known parent→child relationships that must be present in the seed data
_EXPECTED_PARENT_LINKS = {
    "LG Energy Solution": "LG Chem",
    "SK On": "SK Innovation",
    "Panasonic Energy": "Panasonic Holdings",
    "POSCO Future M": "POSCO Holdings",
    "Tenke Fungurume Mining": "CMOC Group",
    "Kisanfu Mining": "CMOC Group",
    "Mutanda Mining": "Glencore",
    "Kamoto Copper Company": "Glencore",
    "Congo DPR Huayou Cobalt": "Huayou Cobalt",
    "PT Freeport Indonesia": "Freeport-McMoRan",
    "Cerro Verde": "Freeport-McMoRan",
    "Vale Base Metals": "Vale",
    "BHP Nickel West": "BHP Group",
    "Escondida": "BHP Group",
    "Lynas Malaysia": "Lynas Rare Earths",
    "Balama Graphite": "Syrah Resources",
    "FinDreams Battery": "BYD",
    "PowerCo SE": "Volkswagen Group",
    "Prime Planet and Energy Solutions": "Toyota Motor Corporation",
    "Geely Auto Group": "Zhejiang Geely Holding Group",
    "Volvo Car Group": "Zhejiang Geely Holding Group",
    "Polestar Automotive": "Volvo Car Group",
    "Zeekr": "Zhejiang Geely Holding Group",
    "Kia Corporation": "Hyundai Motor Company",
}


# ---------------------------------------------------------------------------
# Static data validation
# ---------------------------------------------------------------------------

class TestCompaniesData:
    def test_no_duplicate_canonical_names(self):
        names = [c["canonical_name"] for c in _COMPANIES]
        assert len(names) == len(set(names)), "Duplicate canonical_name entries found"

    def test_count(self):
        assert len(_COMPANIES) == _EXPECTED_TOTAL

    def test_all_headquarters_country_are_iso2(self):
        for c in _COMPANIES:
            country = c["headquarters_country"]
            assert isinstance(country, str), f"{c['canonical_name']}: country must be str"
            assert len(country) == 2, f"{c['canonical_name']}: '{country}' is not 2-char ISO2"
            assert country.isupper(), f"{c['canonical_name']}: '{country}' must be uppercase"

    def test_all_supply_chain_stages_valid(self):
        for c in _COMPANIES:
            stage = c["supply_chain_stage"]
            assert stage in _ALLOWED_STAGES, (
                f"{c['canonical_name']}: '{stage}' not in allowed stages"
            )

    def test_allowed_stages_includes_recycler_and_holding(self):
        assert "recycler" in _ALLOWED_STAGES
        assert "holding" in _ALLOWED_STAGES

    def test_all_data_confidence_in_range(self):
        for c in _COMPANIES:
            conf = c["data_confidence"]
            assert 0.0 <= conf <= 1.0, (
                f"{c['canonical_name']}: data_confidence {conf} out of [0, 1]"
            )

    def test_all_data_source_manual(self):
        for c in _COMPANIES:
            assert c["data_source"] == "manual", f"{c['canonical_name']}: data_source must be 'manual'"

    def test_aliases_are_lists(self):
        for c in _COMPANIES:
            assert isinstance(c["aliases"], list), f"{c['canonical_name']}: aliases must be a list"

    def test_all_alias_types_valid(self):
        valid_types = {"aka", "ticker", "former_name", "abbreviation"}
        for c in _COMPANIES:
            for alias in c["aliases"]:
                assert alias["alias_type"] in valid_types, (
                    f"{c['canonical_name']}: alias_type '{alias['alias_type']}' not in {valid_types}"
                )

    def test_stages_distribution(self):
        stages = [c["supply_chain_stage"] for c in _COMPANIES]
        assert stages.count("miner") >= 10
        assert stages.count("refiner") >= 8
        assert stages.count("cell_maker") >= 8
        assert stages.count("oem") >= 8
        assert stages.count("recycler") >= 2
        assert stages.count("holding") >= 5

    def test_all_notes_populated(self):
        for c in _COMPANIES:
            assert c.get("notes"), f"{c['canonical_name']}: notes must not be empty"

    def test_seeded_lei_values_present(self):
        by_name = {c["canonical_name"]: c for c in _COMPANIES}
        for canonical_name, expected_lei in _SEEDED_LEI_COMPANIES.items():
            assert canonical_name in by_name, f"{canonical_name} not in _COMPANIES"
            assert by_name[canonical_name].get("lei") == expected_lei

    def test_gleif_search_name_entries_are_strings(self):
        for c in _COMPANIES:
            if "gleif_search_name" in c:
                assert isinstance(c["gleif_search_name"], str), (
                    f"{c['canonical_name']}: gleif_search_name must be str"
                )
                assert c["gleif_search_name"].strip(), (
                    f"{c['canonical_name']}: gleif_search_name must not be blank"
                )

    def test_known_override_companies_have_gleif_search_name(self):
        expected = {
            "Volkswagen Group", "BMW Group", "CATL", "CALB Group", "SQM",
            "Huayou Cobalt", "POSCO Future M", "Lynas Rare Earths", "ShanShan Corporation",
        }
        by_name = {c["canonical_name"]: c for c in _COMPANIES}
        for canonical_name in expected:
            assert canonical_name in by_name, f"{canonical_name} missing from _COMPANIES"
            assert "gleif_search_name" in by_name[canonical_name], (
                f"{canonical_name}: expected gleif_search_name key"
            )

    def test_parent_canonical_names_reference_valid_companies(self):
        """Every parent_canonical_name must be the canonical_name of another entry."""
        all_names = {c["canonical_name"] for c in _COMPANIES}
        for c in _COMPANIES:
            pcn = c.get("parent_canonical_name")
            if pcn is not None:
                assert pcn in all_names, (
                    f"{c['canonical_name']}: parent_canonical_name '{pcn}' not in _COMPANIES"
                )

    def test_known_parent_links_present(self):
        """Spot-check that specific parent→child relationships are in the seed data."""
        by_name = {c["canonical_name"]: c for c in _COMPANIES}
        for child_name, expected_parent in _EXPECTED_PARENT_LINKS.items():
            assert child_name in by_name, f"{child_name} missing from _COMPANIES"
            assert by_name[child_name].get("parent_canonical_name") == expected_parent, (
                f"{child_name}: expected parent '{expected_parent}', "
                f"got '{by_name[child_name].get('parent_canonical_name')}'"
            )

    def test_no_self_referential_parent(self):
        """A company must not list itself as its own parent."""
        for c in _COMPANIES:
            pcn = c.get("parent_canonical_name")
            if pcn is not None:
                assert pcn != c["canonical_name"], (
                    f"{c['canonical_name']}: self-referential parent_canonical_name"
                )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_session() -> MagicMock:
    """Build a mock session that returns None for all scalar() calls."""
    session = MagicMock()
    session.scalar.return_value = None
    session.flush = MagicMock()
    session.commit = MagicMock()
    session.add = MagicMock()
    return session


def _flush_that_sets_ids(session: MagicMock) -> None:
    """Install a flush side-effect that gives each new Company a UUID and empty aliases list."""
    def flush_side_effect():
        for call_args in session.add.call_args_list:
            obj = call_args[0][0]
            if hasattr(obj, "canonical_name") and not hasattr(obj, "_id_set"):
                obj.id = uuid.uuid4()
                obj.aliases = []
                obj.parent_company_id = None
                obj._id_set = True

    session.flush.side_effect = flush_side_effect


def _build_exact_return_deque(company_lookup: dict) -> collections.deque:
    """Build a deque of scalar() return values matching the exact call sequence.

    Pass 1 makes one scalar call per _COMPANIES entry (canonical_name lookup).
    Pass 2 makes two scalar calls per entry with parent_canonical_name —
    child query first, then parent query — in _COMPANIES iteration order.
    """
    returns: list = []
    for c in _COMPANIES:
        returns.append(company_lookup.get(c["canonical_name"]))
    for c in _COMPANIES:
        pcn = c.get("parent_canonical_name")
        if pcn:
            returns.append(company_lookup.get(c["canonical_name"]))
            returns.append(company_lookup.get(pcn))
    return collections.deque(returns)


def _session_with_deque(company_lookup: dict) -> MagicMock:
    """Build a session mock whose scalar() uses an exact return-value deque."""
    session = MagicMock()
    returns = _build_exact_return_deque(company_lookup)

    def scalar_se(stmt):
        return returns.popleft() if returns else None

    def flush_se():
        for ca in session.add.call_args_list:
            obj = ca[0][0]
            if hasattr(obj, "canonical_name") and not hasattr(obj, "_id_set"):
                obj.id = uuid.uuid4()
                obj.aliases = []
                obj.parent_company_id = None
                obj._id_set = True

    session.scalar.side_effect = scalar_se
    session.flush.side_effect = flush_se
    return session


# ---------------------------------------------------------------------------
# seed_companies() — pass 1 tests
# ---------------------------------------------------------------------------

class TestSeedCompanies:
    def test_inserts_all_companies_on_empty_table(self):
        session = _make_session()
        _flush_that_sets_ids(session)

        result = seed_companies(session)

        assert result["inserted"] == _EXPECTED_TOTAL
        assert result["updated"] == 0
        session.commit.assert_called_once()

    def test_returns_parents_linked_key(self):
        """Return dict must always contain 'parents_linked'."""
        session = _make_session()
        _flush_that_sets_ids(session)
        result = seed_companies(session)
        assert "parents_linked" in result

    def test_idempotent_skips_existing(self):
        """When all companies already exist, inserted=0."""
        company_lookup = {}
        for entry in _COMPANIES:
            existing = MagicMock()
            existing.id = uuid.uuid4()
            existing.canonical_name = entry["canonical_name"]
            existing.legal_name = entry["legal_name"]
            existing.headquarters_country = entry["headquarters_country"]
            existing.lei = entry.get("lei")
            existing.notes = entry.get("notes")
            existing.data_confidence = entry.get("data_confidence")
            existing.supply_chain_stage = entry.get("supply_chain_stage")
            existing.headquarters_region = entry.get("headquarters_region")
            existing.aliases = []
            existing.parent_company_id = None
            company_lookup[entry["canonical_name"]] = existing

        session = _session_with_deque(company_lookup)

        result = seed_companies(session)

        assert result["inserted"] == 0
        assert result["updated"] == 0
        session.commit.assert_called_once()

    def test_existing_company_partial_upsert_updates_only_mutable_fields(self):
        """Existing rows update allowed mutable fields and keep immutable fields unchanged."""
        vw_seed = next(c for c in _COMPANIES if c["canonical_name"] == "Volkswagen Group")

        vw_mock = MagicMock()
        vw_mock.id = uuid.uuid4()
        vw_mock.canonical_name = "Volkswagen Group"
        vw_mock.legal_name = "Immutable Legal Name"
        vw_mock.headquarters_country = "GB"
        vw_mock.lei = "IMMUTABLE_LEI"
        vw_mock.notes = "stale notes"
        vw_mock.data_confidence = 0.1
        vw_mock.supply_chain_stage = "other"
        vw_mock.headquarters_region = "Other Region"
        vw_mock.aliases = []
        vw_mock.parent_company_id = None

        session = _session_with_deque({"Volkswagen Group": vw_mock})

        result = seed_companies(session)

        assert result["updated"] >= 1
        assert vw_mock.notes == vw_seed["notes"]
        assert vw_mock.data_confidence == vw_seed["data_confidence"]
        assert vw_mock.supply_chain_stage == vw_seed["supply_chain_stage"]
        assert vw_mock.headquarters_region == vw_seed["headquarters_region"]
        assert vw_mock.legal_name == "Immutable Legal Name"
        assert vw_mock.headquarters_country == "GB"
        assert vw_mock.lei == "IMMUTABLE_LEI"

    def test_aliases_added_for_new_companies(self):
        session = _make_session()
        added_objects: list = []

        def flush_and_track():
            for call_args in session.add.call_args_list:
                obj = call_args[0][0]
                if hasattr(obj, "canonical_name") and not hasattr(obj, "_id_set"):
                    obj.id = uuid.uuid4()
                    obj.aliases = []
                    obj.parent_company_id = None
                    obj._id_set = True

        session.flush.side_effect = flush_and_track
        session.add.side_effect = lambda obj: added_objects.append(obj)

        seed_companies(session)

        expected_seed_aliases = sum(len(c["aliases"]) for c in _COMPANIES)
        expected_lei_aliases = sum(1 for c in _COMPANIES if "lei" in c)
        expected_total = expected_seed_aliases + expected_lei_aliases
        alias_objects = [o for o in added_objects if hasattr(o, "alias_type")]
        assert len(alias_objects) == expected_total

    def test_seed_does_not_mutate_companies_list(self):
        """seed_companies must not remove 'aliases', 'gleif_search_name', or
        'parent_canonical_name' keys from _COMPANIES entries."""
        session = _make_session()
        _flush_that_sets_ids(session)

        had_gleif = {c["canonical_name"] for c in _COMPANIES if "gleif_search_name" in c}
        had_parent = {c["canonical_name"] for c in _COMPANIES if "parent_canonical_name" in c}

        seed_companies(session)

        for c in _COMPANIES:
            assert "aliases" in c, f"{c['canonical_name']} lost 'aliases' key after seed"

        for name in had_gleif:
            entry = next(c for c in _COMPANIES if c["canonical_name"] == name)
            assert "gleif_search_name" in entry, f"{name} lost 'gleif_search_name' after seed"

        for name in had_parent:
            entry = next(c for c in _COMPANIES if c["canonical_name"] == name)
            assert "parent_canonical_name" in entry, (
                f"{name} lost 'parent_canonical_name' after seed"
            )

    def test_lei_written_to_company_for_new_row(self):
        session = _make_session()
        added_objects: list = []

        def flush_side_effect():
            for call_args in session.add.call_args_list:
                obj = call_args[0][0]
                if hasattr(obj, "canonical_name") and not hasattr(obj, "_id_set"):
                    obj.id = uuid.uuid4()
                    obj.aliases = []
                    obj.parent_company_id = None
                    obj._id_set = True

        session.flush.side_effect = flush_side_effect
        session.add.side_effect = lambda obj: added_objects.append(obj)

        seed_companies(session)

        lei_aliases = [
            o for o in added_objects
            if hasattr(o, "alias_type") and o.alias_type == "lei"
        ]
        assert len(lei_aliases) == len(_SEEDED_LEI_COMPANIES)
        lei_values = {a.alias for a in lei_aliases}
        assert lei_values == set(_SEEDED_LEI_COMPANIES.values())

    def test_lei_not_backfilled_on_existing_company_with_null_lei(self):
        """Existing rows keep lei immutable even when seed contains one."""
        vw_mock = MagicMock()
        vw_mock.canonical_name = "Volkswagen Group"
        vw_mock.id = uuid.uuid4()
        vw_mock.lei = None
        vw_mock.notes = "notes"
        vw_mock.data_confidence = 0.95
        vw_mock.supply_chain_stage = "oem"
        vw_mock.headquarters_region = "Europe"
        vw_mock.aliases = []
        vw_mock.parent_company_id = None

        session = _session_with_deque({"Volkswagen Group": vw_mock})

        seed_companies(session)

        assert vw_mock.lei is None

    def test_lei_not_overwritten_on_existing_company(self):
        """An existing company with a non-null lei is never overwritten."""
        vw_mock = MagicMock()
        vw_mock.canonical_name = "Volkswagen Group"
        vw_mock.id = uuid.uuid4()
        vw_mock.lei = "PREVIOUSLY_SET_LEI"
        vw_mock.notes = "notes"
        vw_mock.data_confidence = 0.95
        vw_mock.supply_chain_stage = "oem"
        vw_mock.headquarters_region = "Europe"
        vw_mock.aliases = []
        vw_mock.parent_company_id = None

        session = _session_with_deque({"Volkswagen Group": vw_mock})

        seed_companies(session)

        assert vw_mock.lei == "PREVIOUSLY_SET_LEI"


# ---------------------------------------------------------------------------
# seed_companies() — pass 2 (parent linking) tests
# ---------------------------------------------------------------------------

class TestParentLinking:
    def test_parent_linked_when_parent_exists(self):
        """Pass 2 sets child.parent_company_id when both child and parent are found."""
        child_id = uuid.uuid4()
        parent_id = uuid.uuid4()

        child_mock = MagicMock()
        child_mock.id = child_id
        child_mock.canonical_name = "LG Energy Solution"
        child_mock.lei = None
        child_mock.aliases = []
        child_mock.parent_company_id = None

        parent_mock = MagicMock()
        parent_mock.id = parent_id
        parent_mock.canonical_name = "LG Chem"
        parent_mock.lei = None
        parent_mock.aliases = []
        parent_mock.parent_company_id = None

        session = _session_with_deque(
            {"LG Energy Solution": child_mock, "LG Chem": parent_mock}
        )

        result = seed_companies(session)

        assert result["parents_linked"] >= 1
        assert child_mock.parent_company_id == parent_id

    def test_parent_not_set_when_already_linked(self):
        """Pass 2 does not overwrite child.parent_company_id if it is already set."""
        existing_parent_id = uuid.uuid4()

        child_mock = MagicMock()
        child_mock.id = uuid.uuid4()
        child_mock.canonical_name = "LG Energy Solution"
        child_mock.lei = None
        child_mock.aliases = []
        child_mock.parent_company_id = existing_parent_id  # already set

        parent_mock = MagicMock()
        parent_mock.id = uuid.uuid4()
        parent_mock.canonical_name = "LG Chem"
        parent_mock.lei = None
        parent_mock.aliases = []
        parent_mock.parent_company_id = None

        session = _session_with_deque(
            {"LG Energy Solution": child_mock, "LG Chem": parent_mock}
        )

        seed_companies(session)

        assert child_mock.parent_company_id == existing_parent_id

    def test_parent_not_found_does_not_raise(self):
        """When a parent company is not in the DB, seed_companies warns and continues."""
        session = _make_session()
        _flush_that_sets_ids(session)

        result = seed_companies(session)
        assert result["parents_linked"] == 0
        session.commit.assert_called_once()

    def test_commit_called_once(self):
        """session.commit() is called exactly once after both passes."""
        session = _make_session()
        _flush_that_sets_ids(session)
        seed_companies(session)
        session.commit.assert_called_once()
