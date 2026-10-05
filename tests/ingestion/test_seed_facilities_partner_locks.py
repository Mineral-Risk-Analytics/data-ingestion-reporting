"""F-MRDS-2 partner-lock auto-population in the seed-facilities loader.

Verifies that the pure helper functions correctly union new partner-lock
field sets into existing metadata, preserving any other annotations and
the MRDS bookkeeping keys.  Integration coverage of the full
``load_partner_facility_seed`` is intentionally deferred to a future
DB-fixture test — these helpers carry all the new logic that closes the
F-MRDS-2 hole for the seed-loader path.
"""

from __future__ import annotations

from app.services.ingestion.seed_facilities_partner import (
    _merge_link_lock_metadata,
    _merge_partner_lock_metadata,
)


class TestMergePartnerLockMetadata:
    def test_no_existing_metadata_creates_locks(self):
        merged = _merge_partner_lock_metadata(None, {"status", "facility_type"})
        assert merged == {"partner_curated_fields": ["facility_type", "status"]}

    def test_non_dict_existing_treated_as_empty(self):
        merged = _merge_partner_lock_metadata("garbage", {"status"})
        assert merged == {"partner_curated_fields": ["status"]}

    def test_preserves_mrds_bookkeeping(self):
        existing = {
            "source": "mrds",
            "dep_id": "abc123",
            "mrds_names": "Acme Mining",
        }
        merged = _merge_partner_lock_metadata(existing, {"status", "name"})
        assert merged["source"] == "mrds"
        assert merged["dep_id"] == "abc123"
        assert merged["mrds_names"] == "Acme Mining"
        assert merged["partner_curated_fields"] == ["name", "status"]

    def test_unions_with_existing_locks(self):
        existing = {"partner_curated_fields": ["status", "name"]}
        merged = _merge_partner_lock_metadata(existing, {"facility_type", "name"})
        # Union: status (from existing) + name (already there) + facility_type (new)
        assert merged["partner_curated_fields"] == [
            "facility_type", "name", "status",
        ]

    def test_empty_locks_drops_key(self):
        merged = _merge_partner_lock_metadata({"source": "mrds"}, set())
        # When no fields are locked AND no existing locks, the key isn't added
        assert "partner_curated_fields" not in merged

    def test_input_not_mutated(self):
        existing = {"partner_curated_fields": ["status"]}
        _merge_partner_lock_metadata(existing, {"name"})
        assert existing == {"partner_curated_fields": ["status"]}

    def test_malformed_existing_locks_replaced(self):
        existing = {"partner_curated_fields": "not_a_list"}
        merged = _merge_partner_lock_metadata(existing, {"name"})
        assert merged["partner_curated_fields"] == ["name"]


class TestMergeLinkLockMetadata:
    def test_no_existing_creates_link_map(self):
        merged = _merge_link_lock_metadata(None, 301, {"supply_chain_stage"})
        assert merged == {
            "link_partner_curated_fields": {"301": ["supply_chain_stage"]}
        }

    def test_preserves_other_keys(self):
        existing = {
            "source": "mrds",
            "partner_curated_fields": ["status"],
            "link_partner_curated_fields": {
                "289": ["is_primary_product"],
            },
        }
        merged = _merge_link_lock_metadata(
            existing, 301, {"supply_chain_stage"}
        )
        assert merged["source"] == "mrds"
        assert merged["partner_curated_fields"] == ["status"]
        assert merged["link_partner_curated_fields"] == {
            "289": ["is_primary_product"],
            "301": ["supply_chain_stage"],
        }

    def test_unions_existing_link_locks_for_same_material(self):
        existing = {
            "link_partner_curated_fields": {
                "42": ["supply_chain_stage"],
            }
        }
        merged = _merge_link_lock_metadata(
            existing, 42, {"is_primary_product"}
        )
        assert merged["link_partner_curated_fields"]["42"] == [
            "is_primary_product", "supply_chain_stage",
        ]

    def test_empty_locks_no_op(self):
        existing = {"link_partner_curated_fields": {"42": ["supply_chain_stage"]}}
        merged = _merge_link_lock_metadata(existing, 99, set())
        # Empty input — existing entries preserved, new material_id not added
        assert merged["link_partner_curated_fields"] == {
            "42": ["supply_chain_stage"],
        }
        assert "99" not in merged["link_partner_curated_fields"]

    def test_input_not_mutated(self):
        existing = {
            "link_partner_curated_fields": {"42": ["supply_chain_stage"]}
        }
        _merge_link_lock_metadata(existing, 99, {"is_primary_product"})
        # Original untouched
        assert existing == {
            "link_partner_curated_fields": {"42": ["supply_chain_stage"]}
        }
