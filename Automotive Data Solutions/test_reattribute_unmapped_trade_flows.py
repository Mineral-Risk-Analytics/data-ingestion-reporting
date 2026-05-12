"""End-to-end test for reattribute_unmapped_trade_flows (2026-05-11).

Verifies:

  RA.1  Rows whose HS code resolves under the current seed get BOTH
        material_id AND hs_mapping_id written.
  RA.2  Rows whose HS code still doesn't resolve are left untouched
        and counted in still_unmapped.
  RA.3  Rows with empty / NULL hs_code fall into still_unmapped cleanly.
  RA.4  Rows that already have material_id set are excluded from the
        filter (this function only operates on material_id IS NULL).
  RA.5  Idempotent — re-running examines the residual unresolvable
        rows but writes nothing.
  RA.6  attribution_samples returns up to log_attribution_samples
        examples for inspection.
  RA.7  Empty hs_code_material_mappings table → graceful early return.
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
        reattribute_unmapped_trade_flows,
    )

    print("=== reattribute_unmapped_trade_flows ===\n")

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
        chromium = Material(canonical_name="Chromium", category="structural")
        aluminum = Material(canonical_name="Aluminum", category="structural")
        copper = Material(canonical_name="Copper", category="current_collector")
        s.add_all([chromium, aluminum, copper]); s.flush()

        # Seed mappings — mirror the actual 2026-05-11 additions
        m_chr = HsCodeMaterialMapping(
            hs_code_prefix="720250", material_id=chromium.id,
            confidence=1.0, digit_count=6, market_scope="global",
        )
        m_al_corundum = HsCodeMaterialMapping(
            hs_code_prefix="281810", material_id=aluminum.id,
            confidence=1.0, digit_count=6, market_scope="global",
        )
        m_al_hydroxide = HsCodeMaterialMapping(
            hs_code_prefix="281830", material_id=aluminum.id,
            confidence=1.0, digit_count=6, market_scope="global",
        )
        m_al_4digit = HsCodeMaterialMapping(
            hs_code_prefix="2818", material_id=aluminum.id,
            confidence=0.9, digit_count=4, market_scope="global",
        )
        m_cu = HsCodeMaterialMapping(
            hs_code_prefix="262030", material_id=copper.id,
            confidence=1.0, digit_count=6, market_scope="global",
        )
        s.add_all([m_chr, m_al_corundum, m_al_hydroxide, m_al_4digit, m_cu])
        s.flush()

        # Seed source + doc
        src = Source(name="UN Comtrade", source_type="comtrade", phase="1",
                     is_active=True, config_json={})
        s.add(src); s.flush()
        doc = SourceDocument(
            source_id=src.id, external_id="comtrade_reattrib_test",
            title="Test", url="https://example.test",
            document_type="trade_data", metadata_json={},
        )
        s.add(doc); s.flush()
        s.commit()

        # Seed TradeFlow rows representing the post-seed-expansion state:
        # five resolvable rows + three that should remain unresolvable
        # + one row that already has material_id set (filter excluded).
        rows_to_add = [
            # (label, hs_code, pre_material_id)
            ("tf_chrom",      "720250", None),     # → Chromium (new mapping)
            ("tf_al_corund",  "281810", None),     # → Aluminum (new mapping, 6-digit)
            ("tf_al_hydrox",  "281830", None),     # → Aluminum (new mapping, 6-digit)
            ("tf_al_4digit",  "2818",   None),     # → Aluminum (4-digit catch-all)
            ("tf_al_pass2",   "281840", None),     # → Aluminum via Pass-2 fallback
            ("tf_copper",     "262030", None),     # → Copper (new mapping)
            ("tf_unmapped",   "999999", None),     # truly unresolvable
            ("tf_empty_hs",   "",       None),     # empty hs_code
            ("tf_excluded",   "720250", chromium.id),  # already has material_id; filter excludes
        ]
        ids: dict[str, int] = {}
        for label, hs_code, mat_id in rows_to_add:
            tf = TradeFlow(
                source_document_id=doc.id,
                period="2023",
                reporter_country="CN",
                partner_country="WLD",
                hs_code=hs_code,
                material_id=mat_id,
                hs_mapping_id=None,
                import_export_flag="export",
                trade_value_usd=1_000_000.0,
            )
            s.add(tf); s.flush()
            ids[label] = tf.id
        s.commit()

        # ── RA.1–RA.3: run the re-attribution ──────────────────────────
        print("Test RA.1–RA.3: resolves what it can, leaves the rest alone")
        result = reattribute_unmapped_trade_flows(s, batch_size=100)

        # Expectations: 8 rows examined (filter is material_id IS NULL).
        # 5 attributed (chrom + 3 aluminum 6-digit/4-digit + copper).
        # Wait — tf_al_pass2 (281840) also resolves to Aluminum via the
        # Pass-2 4-digit fallback.  So actually 6 are resolved.
        # 2 still_unmapped (tf_unmapped + tf_empty_hs).
        if result["examined"] != 8:
            failures.append(f"examined={result['examined']}, expected 8")
        if result["attributed"] != 6:
            failures.append(
                f"attributed={result['attributed']}, expected 6 "
                "(chrom + corundum + hydroxide + 2818 + 281840-via-fallback + copper)"
            )
        if result["still_unmapped"] != 2:
            failures.append(
                f"still_unmapped={result['still_unmapped']}, expected 2 "
                "(tf_unmapped + tf_empty_hs)"
            )
        if not failures:
            print(f"  [OK] examined=8, attributed=6, still_unmapped=2")

        # ── RA.6: attribution_samples populated ────────────────────────
        if len(result.get("attribution_samples", [])) < 1:
            failures.append("attribution_samples should be non-empty")
        else:
            print(f"  [OK] attribution_samples returned {len(result['attribution_samples'])} examples")

        # Verify actual writes
        s.expire_all()
        rows_after = {label: s.get(TradeFlow, tf_id) for label, tf_id in ids.items()}

        # Chromium
        if (rows_after["tf_chrom"].material_id != chromium.id
                or rows_after["tf_chrom"].hs_mapping_id != m_chr.id):
            failures.append(
                f"tf_chrom: (mid={rows_after['tf_chrom'].material_id}, "
                f"hsid={rows_after['tf_chrom'].hs_mapping_id})"
            )
        else:
            print(f"  [OK] tf_chrom (720250) → Chromium + hs_mapping_id")

        # Aluminum 6-digit (281810 corundum)
        if (rows_after["tf_al_corund"].material_id != aluminum.id
                or rows_after["tf_al_corund"].hs_mapping_id != m_al_corundum.id):
            failures.append(
                f"tf_al_corund: (mid={rows_after['tf_al_corund'].material_id}, "
                f"hsid={rows_after['tf_al_corund'].hs_mapping_id})"
            )
        else:
            print(f"  [OK] tf_al_corund (281810) → Aluminum (corundum mapping)")

        # Aluminum 6-digit (281830 hydroxide)
        if rows_after["tf_al_hydrox"].material_id != aluminum.id:
            failures.append("tf_al_hydrox should be Aluminum")
        else:
            print(f"  [OK] tf_al_hydrox (281830) → Aluminum (hydroxide mapping)")

        # Aluminum 4-digit catch-all
        if (rows_after["tf_al_4digit"].material_id != aluminum.id
                or rows_after["tf_al_4digit"].hs_mapping_id != m_al_4digit.id):
            failures.append(
                f"tf_al_4digit: (mid={rows_after['tf_al_4digit'].material_id}, "
                f"hsid={rows_after['tf_al_4digit'].hs_mapping_id})"
            )
        else:
            print(f"  [OK] tf_al_4digit (2818) → Aluminum via 4-digit catch-all")

        # Aluminum via Pass-2 fallback (281840 falls back to 4-digit 2818)
        if (rows_after["tf_al_pass2"].material_id != aluminum.id
                or rows_after["tf_al_pass2"].hs_mapping_id != m_al_4digit.id):
            failures.append(
                f"tf_al_pass2: (mid={rows_after['tf_al_pass2'].material_id}, "
                f"hsid={rows_after['tf_al_pass2'].hs_mapping_id})"
            )
        else:
            print(f"  [OK] tf_al_pass2 (281840) → Aluminum via Pass-2 fallback")

        # Copper
        if rows_after["tf_copper"].material_id != copper.id:
            failures.append("tf_copper should be Copper")
        else:
            print(f"  [OK] tf_copper (262030) → Copper")

        # Unresolvable rows untouched
        if rows_after["tf_unmapped"].material_id is not None:
            failures.append("tf_unmapped should still be NULL")
        else:
            print(f"  [OK] tf_unmapped (999999) → still NULL")

        if rows_after["tf_empty_hs"].material_id is not None:
            failures.append("tf_empty_hs should still be NULL")
        else:
            print(f"  [OK] tf_empty_hs (empty) → still NULL")

        # ── RA.4: tf_excluded should be untouched (filter excludes it) ─
        if rows_after["tf_excluded"].material_id != chromium.id:
            failures.append(
                f"tf_excluded (had material_id set pre-run) should be untouched; "
                f"got material_id={rows_after['tf_excluded'].material_id}"
            )
        elif rows_after["tf_excluded"].hs_mapping_id is not None:
            failures.append(
                "tf_excluded should NOT have hs_mapping_id written (this function "
                "doesn't touch the FK on already-resolved rows; that's the "
                "backfill's job)"
            )
        else:
            print(f"  [OK] tf_excluded (already attributed) → untouched")

        # ── RA.5: idempotent re-run ────────────────────────────────────
        print("\nTest RA.5: re-running is a no-op on the now-resolved rows")
        result2 = reattribute_unmapped_trade_flows(s, batch_size=100)
        if result2["attributed"] != 0:
            failures.append(
                f"second run attributed={result2['attributed']}, expected 0"
            )
        elif result2["examined"] != 2:
            # Only the 2 still-unmapped rows remain in the filter
            failures.append(
                f"second run examined={result2['examined']}, expected 2 "
                "(the 2 unresolvable rows; resolved ones fell off the filter)"
            )
        else:
            print(f"  [OK] second run: examined=2, attributed=0 (idempotent)")

        # ── RA.7: empty mapping table ───────────────────────────────────
        print("\nTest RA.7: empty hs_code_material_mappings table → graceful return")
        s.query(HsCodeMaterialMapping).delete()
        s.commit()
        result3 = reattribute_unmapped_trade_flows(s)
        if result3["examined"] != 0 or result3["attributed"] != 0:
            failures.append(
                f"empty-map run should be a no-op, got: {result3}"
            )
        else:
            print(f"  [OK] empty mapping table → examined=0, attributed=0 (no rows touched)")

    print("\n" + "=" * 64)
    if failures:
        print("FAIL:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS — reattribute_unmapped_trade_flows:")
    print("  ✓ RA.1 Resolvable rows get material_id + hs_mapping_id")
    print("  ✓ RA.2 Unresolvable rows left as NULL")
    print("  ✓ RA.3 Empty hs_code rows handled cleanly")
    print("  ✓ RA.4 Rows with material_id set are filter-excluded")
    print("  ✓ RA.5 Idempotent — second run writes zero")
    print("  ✓ RA.6 attribution_samples populated for inspection")
    print("  ✓ RA.7 Empty mapping table → graceful early return")
    return 0


if __name__ == "__main__":
    sys.exit(main())
