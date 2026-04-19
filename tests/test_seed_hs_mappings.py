"""Tests for app/services/ingestion/seed_hs_mappings.py."""

from __future__ import annotations

import re
from unittest.mock import MagicMock

import pytest

from app.services.ingestion.seed_hs_mappings import _MAPPINGS, upsert_hs_mappings


# ---------------------------------------------------------------------------
# Static data validation
# ---------------------------------------------------------------------------

class TestMappingsData:
    def test_all_confidence_values_in_range(self):
        for hs, name, desc, conf in _MAPPINGS:
            assert 0.0 <= conf <= 1.0, (
                f"Confidence {conf} out of range for ({hs}, {name!r})"
            )

    def test_no_blank_descriptions(self):
        for hs, name, desc, conf in _MAPPINGS:
            assert desc and desc.strip(), (
                f"Blank description for ({hs}, {name!r})"
            )

    def test_all_hs_prefixes_are_4_digit_strings(self):
        pattern = re.compile(r"^\d{4}$")
        for hs, name, desc, conf in _MAPPINGS:
            assert pattern.match(hs), (
                f"HS prefix {hs!r} for {name!r} is not a 4-digit string"
            )

    def test_mappings_list_is_non_empty(self):
        assert len(_MAPPINGS) > 50, "Expected at least 50 mappings"

    def test_known_materials_have_entries(self):
        names_in_mappings = {name for _, name, _, _ in _MAPPINGS}
        expected = {"Lithium", "Nickel", "Cobalt", "Natural Graphite", "Manganese", "Copper"}
        for mat in expected:
            assert mat in names_in_mappings, f"Expected '{mat}' to have at least one HS mapping"

    def test_multi_material_prefixes_are_allowed(self):
        """Prefixes like 2615 should appear multiple times for different materials."""
        prefix_counts: dict[str, int] = {}
        for hs, _, _, _ in _MAPPINGS:
            prefix_counts[hs] = prefix_counts.get(hs, 0) + 1
        # 2615 maps to Vanadium, Niobium, Tantalum, Zirconium
        assert prefix_counts.get("2615", 0) >= 4

    def test_8112_maps_to_multiple_materials(self):
        """Chapter 81 (8112) should cover Gallium, Germanium, Indium, Niobium, Chromium."""
        ch81_materials = {name for hs, name, _, _ in _MAPPINGS if hs == "8112"}
        expected = {"Gallium", "Germanium", "Indium", "Niobium", "Chromium"}
        assert expected.issubset(ch81_materials)

    def test_lithium_has_multiple_prefixes(self):
        """Lithium should have entries for 2825 (hydroxide), 2836 (carbonate), 2805 (metal)."""
        lithium_prefixes = {hs for hs, name, _, _ in _MAPPINGS if name == "Lithium"}
        assert {"2825", "2836", "2805"}.issubset(lithium_prefixes)

    def test_no_duplicate_hs_material_pairs(self):
        """Each (hs_prefix, canonical_name) pair must appear at most once."""
        seen: set[tuple[str, str]] = set()
        for hs, name, _, _ in _MAPPINGS:
            pair = (hs, name)
            assert pair not in seen, f"Duplicate mapping: {pair}"
            seen.add(pair)

    def test_sodium_ion_materials_covered(self):
        """Sodium should have HS mappings for Na-ion tracking."""
        sodium_prefixes = {hs for hs, name, _, _ in _MAPPINGS if name == "Sodium"}
        assert len(sodium_prefixes) >= 1

    def test_ree_individual_covered(self):
        """Individual motor REEs should have HS mappings."""
        covered = {name for _, name, _, _ in _MAPPINGS}
        for ree in ("Neodymium", "Praseodymium", "Dysprosium", "Terbium"):
            assert ree in covered, f"{ree} has no HS mappings"


# ---------------------------------------------------------------------------
# upsert_hs_mappings
# ---------------------------------------------------------------------------

def _make_material(name: str, id: int) -> MagicMock:
    m = MagicMock()
    m.id = id
    m.canonical_name = name
    return m


def _make_session(materials: list[MagicMock]) -> MagicMock:
    """Build a mock session for upsert_hs_mappings tests.

    session.scalar.return_value controls whether existing rows are found:
      None  → no existing row (insert)
      Mock  → existing row found (skip)
    Set this on the returned session after calling this helper.
    """
    session = MagicMock()

    mat_result = MagicMock()
    mat_result.all.return_value = materials
    session.scalars.return_value = mat_result

    # Default: no existing rows → all inserts. Tests can override.
    session.scalar.return_value = None
    return session


class TestUpsertHsMappings:
    def test_inserts_mappings_for_known_materials(self):
        """Rows are inserted for materials present in the DB."""
        materials = [
            _make_material("Natural Graphite", 1),
            _make_material("Nickel", 2),
            _make_material("Cobalt", 3),
        ]
        session = _make_session(materials)  # default: no existing rows

        result = upsert_hs_mappings(session)

        assert result["inserted"] > 0
        # Natural Graphite → 1 mapping, Nickel → 3, Cobalt → 2 = 6 total minimum
        assert result["inserted"] >= 6

    def test_skips_unknown_materials(self):
        """Materials in _MAPPINGS but not in the DB are counted as skipped."""
        # Provide only 3 of ~36+ materials
        materials = [
            _make_material("Natural Graphite", 1),
            _make_material("Nickel", 2),
            _make_material("Cobalt", 3),
        ]
        session = _make_session(materials)

        result = upsert_hs_mappings(session)

        assert result["skipped_unknown_material"] > 0

    def test_skipped_unknown_plus_inserted_equals_total_mappings(self):
        """inserted + skipped_unknown + skipped_existing should equal len(_MAPPINGS)."""
        materials = [
            _make_material("Natural Graphite", 1),
            _make_material("Nickel", 2),
            _make_material("Cobalt", 3),
        ]
        session = _make_session(materials)

        result = upsert_hs_mappings(session)

        total = result["inserted"] + result["skipped_unknown_material"] + result["skipped_existing"]
        assert total == len(_MAPPINGS)

    def test_idempotent_skips_existing_rows(self):
        """Second call skips all previously inserted rows."""
        materials = [_make_material("Natural Graphite", 1)]
        session = _make_session(materials)

        # First call: no existing rows
        session.scalar.return_value = None
        result1 = upsert_hs_mappings(session)
        first_inserted = result1["inserted"]

        # Second call: all rows exist now
        existing_mock = MagicMock()  # truthy → exists
        session.scalar.return_value = existing_mock
        result2 = upsert_hs_mappings(session)

        assert result2["inserted"] == 0
        assert result2["skipped_existing"] == first_inserted

    def test_add_called_for_each_inserted_row(self):
        """session.add() is called once per inserted mapping."""
        materials = [_make_material("Natural Graphite", 1)]
        session = _make_session(materials)
        session.scalar.return_value = None  # no existing rows

        result = upsert_hs_mappings(session)

        assert session.add.call_count == result["inserted"]

    def test_commit_always_called(self):
        """session.commit() is called even if nothing was inserted."""
        materials = [_make_material("Natural Graphite", 1)]
        session = _make_session(materials)
        session.scalar.return_value = MagicMock()  # all exist

        upsert_hs_mappings(session)

        session.commit.assert_called_once()

    def test_flush_called_when_insertions_made(self):
        """session.flush() is called after insertions."""
        materials = [_make_material("Natural Graphite", 1)]
        session = _make_session(materials)
        session.scalar.return_value = None

        upsert_hs_mappings(session)

        session.flush.assert_called()

    def test_flush_not_called_when_nothing_inserted(self):
        """session.flush() is NOT called when all rows already exist."""
        materials = [_make_material("Natural Graphite", 1)]
        session = _make_session(materials)
        session.scalar.return_value = MagicMock()  # all exist

        upsert_hs_mappings(session)

        session.flush.assert_not_called()

    def test_returns_correct_keys(self):
        """Result dict always contains the three expected keys."""
        session = _make_session([])

        result = upsert_hs_mappings(session)

        assert set(result.keys()) == {"inserted", "skipped_existing", "skipped_unknown_material"}
