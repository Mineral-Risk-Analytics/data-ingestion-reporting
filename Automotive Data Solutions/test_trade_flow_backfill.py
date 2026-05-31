"""End-to-end test for the TradeFlow.hs_mapping_id backfill (2026-05-09).

Verifies:

  BF.1  Matched rows have hs_mapping_id populated to the resolved
        HsCodeMaterialMapping.id.
  BF.2  Mismatched rows (where the resolver now points to a different
        material than what's stored) are left untouched and reported
        in the mismatched count + mismatch_samples.
  BF.3  Unmapped rows (HS code no longer in the mapping table at any
        prefix length) are left untouched and reported.
  BF.4  Rows that already have hs_mapping_id set are excluded from the
        filter — the backfill is a no-op on a previously-backfilled
        table (idempotent).
  BF.5  Empty HS code rows fall into the unmapped bucket cleanly.
"""

from __future__ import annotations

import sys

sys.path.insert(0, "/sessions/keen-wonderful-lamport/mnt/battery-data-intelligence-engine")


def main() -> int:
    failures: list[str] = []

    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session
    from sqlalchemy.ext.compiler import compiles
    from sqlalchemy.dialects.postgresql import JSONB, UUID, ARRAY

    @compiles(JSONB, "sqlite")
    def _jsonb_sqlite(t, c, **kw):  # noqa: ARG001
        return "JSON"

    @compiles(UUID, "sqlite")
    def _uuid_sqlite(t, c, **kw):  # noqa: ARG001
        return "VARCHAR(36)"

    @compiles(ARRAY, "sqlite")
    def _array_sqlite(t, c, **kw):  # noqa: ARG001
        return "JSON"

    from app.db.base import Base
    from app.models.documents import SourceDocument
    from app.models.source import Source
    from app.models.supply import (
        HsCodeMaterialMapping,
        Material,
        TradeFlow,
    )
    from app.services.ingestion.comtrade import (
        backfill_trade_flow_hs_mappings,
    )

    print("=== TradeFlow.hs_mapping_id backfill ===\n")

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=[
            Material.__table__,
            HsCodeMaterialMapping.__table__,
            Source.__table__,
            SourceDocument.__table__,
            TradeFlow.__table__,
        ],
    )

    with Session(engine) as s:
        # Seed materials
        lithium = Material(canonical_name="Lithium", category="cathode_active")
        cobalt = Material(canonical_name="Cobalt", category="cathode_active")
        s.add_all([lithium, cobalt]); s.flush()

        # Seed mappings:
        #   283691  → Lithium  (precise 6-digit)
        #   2530    → Lithium  (broad 4-digit, low confidence)
        #   810520  → Cobalt   (precise 6-digit)
        m_li_precise = HsCodeMaterialMapping(
            hs_code_prefix="283691", material_id=lithium.id,
            confidence=1.0, digit_count=6, market_scope="global",
        )
        m_li_broad = HsCodeMaterialMapping(
            hs_code_prefix="2530", material_id=lithium.id,
            confidence=0.4, digit_count=4, market_scope="global",
        )
        m_co_precise = HsCodeMaterialMapping(
            hs_code_prefix="810520", material_id=cobalt.id,
            confidence=1.0, digit_count=6, market_scope="global",
        )
        s.add_all([m_li_precise, m_li_broad, m_co_precise])
        s.flush()

        # Seed source + source_document
        src = Source(name="UN Comtrade", source_type="comtrade", phase="1",
                     is_active=True, config_json={})
        s.add(src); s.flush()
        doc = SourceDocument(
            source_id=src.id, external_id="comtrade_test_doc",
            title="Test Comtrade snapshot", url="https://example.test",
            document_type="trade_data", metadata_json={},
        )
        s.add(doc); s.flush()
        s.commit()

        # Seed TradeFlow rows representing the pre-Phase-1.5 state:
        # - tf_match_precise: 283691 + material=Lithium → should resolve cleanly to m_li_precise
        # - tf_match_broad:   253099 + material=Lithium → resolves via 4-digit fallback
        # - tf_mismatch:      283691 + material=Cobalt  → resolver says Lithium, stored says Cobalt: drift
        # - tf_unmapped:      999999 + material=Lithium → resolver finds nothing
        # - tf_empty_hs:      ""     + material=Lithium → empty HS, unmapped
        # - tf_already_done:  283691 + material=Lithium + hs_mapping_id=10 (pre-set) → excluded by filter
        # - tf_no_material:   283691 + material=NULL    → excluded by filter
        rows_to_add = [
            ("tf_match_precise", "283691", lithium.id, None),
            ("tf_match_broad",   "253099", lithium.id, None),
            ("tf_mismatch",      "283691", cobalt.id,  None),
            ("tf_unmapped",      "999999", lithium.id, None),
            ("tf_empty_hs",      "",       lithium.id, None),
            ("tf_already_done",  "283691", lithium.id, m_li_precise.id),
            ("tf_no_material",   "283691", None,       None),
        ]
        ids: dict[str, int] = {}
        for label, hs_code, mat_id, hs_id in rows_to_add:
            tf = TradeFlow(
                source_document_id=doc.id,
                period="2022",
                reporter_country="CN",
                partner_country="WLD",
                hs_code=hs_code,
                material_id=mat_id,
                hs_mapping_id=hs_id,
                import_export_flag="export",
                trade_value_usd=1_000_000.0,
            )
            s.add(tf); s.flush()
            ids[label] = tf.id
        s.commit()

        # ── BF.1–BF.3: run the backfill ─────────────────────────────────
        print("Test BF.1–BF.3: backfill resolves matches, flags mismatches & unmapped")
        result = backfill_trade_flow_hs_mappings(s, batch_size=100)

        # Examined = 5 rows that pass the filter (NULL hs_mapping_id + non-NULL material_id):
        # tf_match_precise, tf_match_broad, tf_mismatch, tf_unmapped, tf_empty_hs
        if result["examined"] != 5:
            failures.append(f"examined={result['examined']}, expected 5")
        if result["updated"] != 2:
            failures.append(f"updated={result['updated']}, expected 2 (precise + broad)")
        if result["mismatched"] != 1:
            failures.append(f"mismatched={result['mismatched']}, expected 1 (tf_mismatch)")
        if result["unmapped"] != 2:
            failures.append(
                f"unmapped={result['unmapped']}, expected 2 (tf_unmapped + tf_empty_hs)"
            )
        if not any(
            sample.get("trade_flow_id") == ids["tf_mismatch"]
            for sample in result.get("mismatch_samples", [])
        ):
            failures.append("mismatch_samples missing tf_mismatch row")
        if not failures:
            print(f"  [OK] examined=5, updated=2, mismatched=1, unmapped=2")
            print(f"  [OK] mismatch sample includes tf_mismatch row")

        # Verify the actual row updates
        s.expire_all()  # force fresh read from DB
        rows_after = {
            label: s.get(TradeFlow, tf_id)
            for label, tf_id in ids.items()
        }

        if rows_after["tf_match_precise"].hs_mapping_id != m_li_precise.id:
            failures.append(
                f"tf_match_precise.hs_mapping_id = "
                f"{rows_after['tf_match_precise'].hs_mapping_id}, "
                f"expected {m_li_precise.id}"
            )
        else:
            print(f"  [OK] tf_match_precise → hs_mapping_id={m_li_precise.id}")

        if rows_after["tf_match_broad"].hs_mapping_id != m_li_broad.id:
            failures.append(
                f"tf_match_broad.hs_mapping_id = "
                f"{rows_after['tf_match_broad'].hs_mapping_id}, "
                f"expected {m_li_broad.id}"
            )
        else:
            print(f"  [OK] tf_match_broad → hs_mapping_id={m_li_broad.id} (broad fallback)")

        if rows_after["tf_mismatch"].hs_mapping_id is not None:
            failures.append(
                f"tf_mismatch.hs_mapping_id should be NULL (drift), got "
                f"{rows_after['tf_mismatch'].hs_mapping_id}"
            )
        if rows_after["tf_mismatch"].material_id != cobalt.id:
            failures.append(
                f"tf_mismatch.material_id should be UNCHANGED (Cobalt), got "
                f"{rows_after['tf_mismatch'].material_id}"
            )
        if (rows_after["tf_mismatch"].hs_mapping_id is None
                and rows_after["tf_mismatch"].material_id == cobalt.id):
            print(f"  [OK] tf_mismatch left untouched (drift logged, no silent rewrite)")

        if rows_after["tf_unmapped"].hs_mapping_id is not None:
            failures.append("tf_unmapped should remain NULL")
        if rows_after["tf_empty_hs"].hs_mapping_id is not None:
            failures.append("tf_empty_hs should remain NULL")

        # ── BF.4: idempotency ──────────────────────────────────────────
        print("\nTest BF.4: re-running is a no-op (idempotent)")
        result2 = backfill_trade_flow_hs_mappings(s, batch_size=100)
        # After the first run, the remaining unfilterable rows are the
        # mismatch + 2 unmapped = 3 rows.  None of them are updatable,
        # so the second run examines them (filter still surfaces them
        # since hs_mapping_id IS NULL) and produces zero updates.
        if result2["updated"] != 0:
            failures.append(
                f"second run updated={result2['updated']}, expected 0"
            )
        else:
            print(f"  [OK] second run: updated=0 (no-op on previously-resolved rows)")

        # tf_already_done should still have its original hs_mapping_id
        s.expire_all()
        already_done_after = s.get(TradeFlow, ids["tf_already_done"])
        if already_done_after.hs_mapping_id != m_li_precise.id:
            failures.append(
                f"tf_already_done.hs_mapping_id mutated: "
                f"{already_done_after.hs_mapping_id}"
            )
        else:
            print(f"  [OK] tf_already_done unchanged across runs")

        # ── BF.5: empty mapping table guard ───────────────────────────
        print("\nTest BF.5: empty hs_code_material_mappings table → early return")
        # Empty the mapping table and confirm graceful handling
        s.query(HsCodeMaterialMapping).delete()
        s.commit()
        result3 = backfill_trade_flow_hs_mappings(s)
        if result3["examined"] != 0 or result3["updated"] != 0:
            failures.append(
                f"empty-map run did work it shouldn't have: {result3}"
            )
        else:
            print(f"  [OK] empty mapping table → examined=0, updated=0 (no rows touched)")

    print("\n" + "=" * 64)
    if failures:
        print("FAIL:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS — TradeFlow.hs_mapping_id backfill:")
    print("  ✓ BF.1 Precise mappings backfilled")
    print("  ✓ BF.2 Broad 4-digit fallback backfilled")
    print("  ✓ BF.3 Drift mismatches flagged + left untouched")
    print("  ✓ BF.3 Unmapped / empty-HS rows left untouched")
    print("  ✓ BF.4 Idempotent — second run is a no-op")
    print("  ✓ BF.5 Empty mapping table handled gracefully")
    return 0


if __name__ == "__main__":
    sys.exit(main())
