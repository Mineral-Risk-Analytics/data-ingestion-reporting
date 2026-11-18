"""Tests for app/services/ingestion/seed_materials.py.

All tests use mocked sessions — no live database.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.services.ingestion.seed_materials import (
    _JUNCTION_ROWS,
    _MATERIALS,
    seed_battery_chemistry_junctions,
    seed_materials_register,
)


# ---------------------------------------------------------------------------
# Full materials register (_MATERIALS)
# ---------------------------------------------------------------------------

class TestMaterialsRegisterData:
    def test_register_has_thirty_nine_entries(self):
        assert len(_MATERIALS) == 39

    def test_expected_motor_ree_and_sodium_names_present(self):
        names = {m["canonical_name"] for m in _MATERIALS}
        for n in ("Neodymium", "Praseodymium", "Dysprosium", "Terbium", "Sodium"):
            assert n in names

    def test_manual_ree_rows_have_criticality_score(self):
        for name in ("Neodymium", "Praseodymium", "Dysprosium", "Terbium"):
            m = next(x for x in _MATERIALS if x["canonical_name"] == name)
            assert m.get("criticality_score") is not None

    def test_sodium_has_low_criticality_score(self):
        sodium = next(m for m in _MATERIALS if m["canonical_name"] == "Sodium")
        assert sodium["criticality_score"] < 0.20

    def test_terbium_is_no_benchmark(self):
        tb = next(m for m in _MATERIALS if m["canonical_name"] == "Terbium")
        assert tb["data_availability"] == "no_benchmark"

    def test_all_entries_have_required_keys(self):
        required = {
            "canonical_name",
            "category",
            "hs_codes",
            "is_ira_critical_mineral",
            "is_eu_crma_critical",
            "notes",
        }
        for m in _MATERIALS:
            missing = required - set(m.keys())
            assert not missing, f"{m['canonical_name']} missing keys: {missing}"

    def test_rees_are_eu_crma_and_ira_critical(self):
        rees = {"Neodymium", "Praseodymium", "Dysprosium", "Terbium"}
        for m in _MATERIALS:
            if m["canonical_name"] in rees:
                assert m["is_eu_crma_critical"] is True
                assert m["is_ira_critical_mineral"] is True


# ---------------------------------------------------------------------------
# _JUNCTION_ROWS data validation
# ---------------------------------------------------------------------------

class TestJunctionRowsData:
    def test_all_expected_chemistries_present(self):
        slugs = {row[0] for row in _JUNCTION_ROWS}
        assert {"nmc", "lfp", "nca", "lfmp", "sodium_ion", "solid_state"} == slugs

    def test_all_intensities_between_0_and_1(self):
        for row in _JUNCTION_ROWS:
            slug, name, role, intensity, is_sub = row
            assert 0.0 < intensity <= 1.0, f"{slug}/{name} intensity={intensity} out of range"

    def test_is_substitutable_is_bool(self):
        for row in _JUNCTION_ROWS:
            slug, name, role, intensity, is_sub = row
            assert isinstance(is_sub, bool), f"{slug}/{name} is_substitutable not bool"

    def test_lfp_has_lithium_iron_phosphate(self):
        lfp_materials = {row[1] for row in _JUNCTION_ROWS if row[0] == "lfp"}
        assert "Lithium" in lfp_materials
        assert "Iron Ore" in lfp_materials
        assert "Phosphate" in lfp_materials

    def test_sodium_ion_does_not_use_lithium(self):
        nai_materials = {row[1] for row in _JUNCTION_ROWS if row[0] == "sodium_ion"}
        assert "Lithium" not in nai_materials

    def test_nmc_has_nickel_cobalt_manganese(self):
        nmc_materials = {row[1] for row in _JUNCTION_ROWS if row[0] == "nmc"}
        assert "Nickel" in nmc_materials
        assert "Cobalt" in nmc_materials
        assert "Manganese" in nmc_materials

    def test_solid_state_has_solid_electrolyte_materials(self):
        ss_materials = {row[1] for row in _JUNCTION_ROWS if row[0] == "solid_state"}
        # Should include at least one of the solid electrolyte materials
        electrolyte_mats = {"Germanium", "Silicon (Anode Grade)", "Tantalum"}
        assert ss_materials & electrolyte_mats, "solid_state should include solid electrolyte materials"


# ---------------------------------------------------------------------------
# seed_materials_register
# ---------------------------------------------------------------------------

def _make_session_for_seed(existing_names: set[str]) -> MagicMock:
    """Return a mock session where materials with names in existing_names already exist."""
    session = MagicMock()
    session.scalar.return_value = None
    return session


class TestSeedMaterialsRegister:
    def test_inserts_all_when_table_empty(self):
        session = MagicMock()
        session.scalar.return_value = None  # all materials are new

        stats = seed_materials_register(session)

        assert stats["inserted"] == len(_MATERIALS)
        assert session.add.call_count == len(_MATERIALS)

    def test_skips_existing_materials(self):
        session = MagicMock()
        # First row in _MATERIALS is Aluminum — treat it as already present.
        existing = MagicMock()
        call_count = {"n": 0}

        def scalar_se(stmt):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return existing
            return None

        session.scalar.side_effect = scalar_se

        stats = seed_materials_register(session)

        assert stats["inserted"] == len(_MATERIALS) - 1
        assert stats["skipped_existing"] == 1

    def test_flush_called_when_insertions_made(self):
        session = MagicMock()
        session.scalar.return_value = None

        seed_materials_register(session)

        session.flush.assert_called()

    def test_flush_not_called_when_nothing_inserted(self):
        session = MagicMock()
        session.scalar.return_value = MagicMock()  # all exist

        seed_materials_register(session)

        session.flush.assert_not_called()


# ---------------------------------------------------------------------------
# seed_battery_chemistry_junctions
# ---------------------------------------------------------------------------

def _make_chemistry(slug: str, id: int) -> MagicMock:
    c = MagicMock()
    c.id = id
    c.slug = slug
    return c


def _make_material(name: str, id: int) -> MagicMock:
    m = MagicMock()
    m.id = id
    m.canonical_name = name
    return m


class TestSeedBatteryChemistryJunctions:
    def _make_session(self, chemistries, materials, existing_junction=False):
        session = MagicMock()

        chem_result = MagicMock()
        chem_result.__iter__ = lambda self: iter(chemistries)
        chem_result.all = lambda: chemistries  # Not used directly

        mat_result = MagicMock()
        mat_result.__iter__ = lambda self: iter(materials)
        mat_result.all = lambda: materials

        call_count = {"n": 0}

        def scalars_se(stmt):
            call_count["n"] += 1
            result = MagicMock()
            if call_count["n"] == 1:
                result.all.return_value = chemistries
            else:
                result.all.return_value = materials
            return result

        session.scalars.side_effect = scalars_se
        session.scalar.return_value = MagicMock() if existing_junction else None
        return session

    def test_skips_when_all_junctions_already_exist(self):
        chems = [_make_chemistry("lfp", 1)]
        mats = [
            _make_material("Lithium", 1),
            _make_material("Iron Ore", 2),
            _make_material("Phosphate", 3),
            _make_material("Natural Graphite", 4),
            _make_material("Copper", 5),
            _make_material("Aluminum", 6),
            _make_material("Fluorspar", 7),
        ]
        session = self._make_session(chems, mats, existing_junction=True)

        result = seed_battery_chemistry_junctions(session)

        assert result["inserted"] == 0
        assert result["skipped_existing"] > 0

    def test_inserts_when_no_junctions_exist(self):
        chems = [_make_chemistry("lfp", 1)]
        mats = [
            _make_material("Lithium", 1),
            _make_material("Iron Ore", 2),
            _make_material("Phosphate", 3),
            _make_material("Natural Graphite", 4),
            _make_material("Copper", 5),
            _make_material("Aluminum", 6),
            _make_material("Fluorspar", 7),
        ]
        session = self._make_session(chems, mats, existing_junction=False)

        result = seed_battery_chemistry_junctions(session)

        # LFP has 7 materials in _JUNCTION_ROWS
        assert result["inserted"] == 7
        assert session.add.call_count == 7

    def test_skipped_missing_material_counted(self):
        chems = [_make_chemistry("nmc", 1)]
        # Only provide some of NMC's materials — rest will be "missing"
        mats = [_make_material("Lithium", 1)]
        session = self._make_session(chems, mats, existing_junction=False)

        result = seed_battery_chemistry_junctions(session)

        # 1 row inserted (nmc + Lithium found); everything else skipped:
        # - 9 NMC rows where material not in mat_map → skipped_missing
        # - 38 non-NMC rows where chemistry not in chem_map → skipped_missing
        # Total skipped_missing = 47, inserted = 1
        assert result["inserted"] == 1
        assert result["skipped_missing_material"] == 47

    def test_flush_called_after_insertions(self):
        chems = [_make_chemistry("lfp", 1)]
        mats = [
            _make_material("Lithium", 1),
            _make_material("Iron Ore", 2),
            _make_material("Phosphate", 3),
            _make_material("Natural Graphite", 4),
            _make_material("Copper", 5),
            _make_material("Aluminum", 6),
            _make_material("Fluorspar", 7),
        ]
        session = self._make_session(chems, mats, existing_junction=False)

        seed_battery_chemistry_junctions(session)

        session.flush.assert_called()
