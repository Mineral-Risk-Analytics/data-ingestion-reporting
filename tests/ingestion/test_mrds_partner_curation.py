"""F-MRDS-2 (2026-06-09): partner-curation protection for MRDS re-run.

Unit tests for the pure helper functions that gate facility / link field
updates against the partner-curated lock lists stored in
``Facility.metadata_json``.

Integration coverage of the full ingest_mrds loop is intentionally deferred
to a future fixture-backed test — these helpers carry all the new logic.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.services.ingestion.mrds import (
    _merge_mrds_metadata,
    _read_partner_locks,
)


def _fac(metadata):
    """Minimal Facility stand-in — only ``metadata_json`` is read."""
    return SimpleNamespace(metadata_json=metadata)


# ---------------------------------------------------------------------------
# _read_partner_locks
# ---------------------------------------------------------------------------

class TestReadPartnerLocks:
    def test_none_facility_returns_empty(self):
        fac_locks, link_locks = _read_partner_locks(None)
        assert fac_locks == frozenset()
        assert link_locks == {}

    def test_missing_metadata_returns_empty(self):
        fac_locks, link_locks = _read_partner_locks(_fac(None))
        assert fac_locks == frozenset()
        assert link_locks == {}

    def test_non_dict_metadata_returns_empty(self):
        # Defensive: schema drift or test-fixture noise shouldn't blow up
        fac_locks, link_locks = _read_partner_locks(_fac("not a dict"))
        assert fac_locks == frozenset()
        assert link_locks == {}

    def test_metadata_without_lock_keys_returns_empty(self):
        meta = {"source": "mrds", "dep_id": "12345"}
        fac_locks, link_locks = _read_partner_locks(_fac(meta))
        assert fac_locks == frozenset()
        assert link_locks == {}

    def test_facility_locks_parsed(self):
        meta = {"partner_curated_fields": ["status", "facility_type"]}
        fac_locks, link_locks = _read_partner_locks(_fac(meta))
        assert fac_locks == frozenset({"status", "facility_type"})
        assert link_locks == {}

    def test_link_locks_parsed(self):
        meta = {
            "link_partner_curated_fields": {
                "301": ["supply_chain_stage"],
                "289": ["is_primary_product", "annual_capacity_tpy"],
            }
        }
        fac_locks, link_locks = _read_partner_locks(_fac(meta))
        assert fac_locks == frozenset()
        assert link_locks == {
            "301": frozenset({"supply_chain_stage"}),
            "289": frozenset({"is_primary_product", "annual_capacity_tpy"}),
        }

    def test_both_lock_kinds_parsed(self):
        meta = {
            "partner_curated_fields": ["name"],
            "link_partner_curated_fields": {"42": ["supply_chain_stage"]},
        }
        fac_locks, link_locks = _read_partner_locks(_fac(meta))
        assert fac_locks == frozenset({"name"})
        assert link_locks == {"42": frozenset({"supply_chain_stage"})}

    def test_malformed_locks_silently_dropped(self):
        # Non-list value for partner_curated_fields → empty (defensive)
        meta = {
            "partner_curated_fields": "status",   # string, not list
            "link_partner_curated_fields": {
                "301": ["supply_chain_stage"],
                "289": "supply_chain_stage",  # not a list — dropped
            },
        }
        fac_locks, link_locks = _read_partner_locks(_fac(meta))
        assert fac_locks == frozenset()
        assert link_locks == {"301": frozenset({"supply_chain_stage"})}

    def test_non_string_entries_filtered(self):
        meta = {
            "partner_curated_fields": ["status", 42, None, "facility_type"],
        }
        fac_locks, _ = _read_partner_locks(_fac(meta))
        assert fac_locks == frozenset({"status", "facility_type"})


# ---------------------------------------------------------------------------
# _merge_mrds_metadata
# ---------------------------------------------------------------------------

class TestMergeMrdsMetadata:
    def test_no_existing_returns_mrds_keys(self):
        merged = _merge_mrds_metadata(None, {"source": "mrds", "dep_id": "X"})
        assert merged == {"source": "mrds", "dep_id": "X"}

    def test_non_dict_existing_returns_mrds_keys(self):
        merged = _merge_mrds_metadata("garbage", {"source": "mrds"})
        assert merged == {"source": "mrds"}

    def test_partner_curation_preserved(self):
        existing = {
            "source": "mrds",
            "partner_curated_fields": ["status"],
            "link_partner_curated_fields": {"42": ["supply_chain_stage"]},
            "partner_note": "Mothballed Q2 2025 per partner intel",
        }
        new_mrds = {"source": "mrds", "dep_id": "abc123", "mrds_names": "Acme"}
        merged = _merge_mrds_metadata(existing, new_mrds)

        # MRDS keys overlaid
        assert merged["dep_id"] == "abc123"
        assert merged["mrds_names"] == "Acme"
        # Partner annotations survive
        assert merged["partner_curated_fields"] == ["status"]
        assert merged["link_partner_curated_fields"] == {
            "42": ["supply_chain_stage"]
        }
        assert merged["partner_note"] == "Mothballed Q2 2025 per partner intel"

    def test_mrds_keys_overwrite_existing_mrds_values(self):
        existing = {"source": "mrds", "dep_id": "OLD", "mrds_names": "OldName"}
        new_mrds = {"source": "mrds", "dep_id": "NEW", "mrds_names": "NewName"}
        merged = _merge_mrds_metadata(existing, new_mrds)
        assert merged["dep_id"] == "NEW"
        assert merged["mrds_names"] == "NewName"

    def test_input_not_mutated(self):
        existing = {"partner_curated_fields": ["status"]}
        new_mrds = {"source": "mrds"}
        merged = _merge_mrds_metadata(existing, new_mrds)
        assert merged is not existing
        # Existing dict untouched
        assert existing == {"partner_curated_fields": ["status"]}
