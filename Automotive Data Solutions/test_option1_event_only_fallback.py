"""End-to-end test for Option 1 — event-only fallback in hs_node_scorer (2026-05-06).

Confirms:
  1. **HHI-anchored path** — mapping with production shares + events
     produces ``score_method="hhi_anchored"`` and the canonical
     ``0.50×HHI + 0.25×tariff + 0.25×export`` formula.
  2. **Event-only fallback** — mapping with NO production shares but WITH
     country-scoped events produces ``score_method="event_only_no_hhi"``
     and the renormalised ``0.5×tariff + 0.5×export`` formula.
  3. **No-data short-circuit** — mapping with neither shares nor events
     returns ``None`` (unchanged behaviour).
  4. **Batch enumerator** — ``score_all_hs_nodes`` picks up the event-only
     mapping (via the new event-pair query) and writes a row for it.

Why this matters: pre-fix, refined / intermediate / battery_grade HS codes
for materials like aluminum had hundreds of tariff/export events tagged
to them but never got a score because USGS MCS only publishes per-country
production shares for the ore stage.  The frontend showed "Events: 409
· Score: —", which is what prompted the partner question "what was the
point of breaking everything down by HS code if the 6-digit codes never
get scored?"  The fallback turns those events into a usable composite.
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timezone

sys.path.insert(0, "/sessions/keen-wonderful-lamport/mnt/battery-data-intelligence-engine")


def main() -> int:
    failures: list[str] = []

    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from sqlalchemy.ext.compiler import compiles
    from sqlalchemy.dialects.postgresql import JSONB, UUID, ARRAY

    @compiles(JSONB, "sqlite")
    def _jsonb_sqlite(type_, compiler, **kw):  # noqa: ARG001
        return "JSON"

    @compiles(UUID, "sqlite")
    def _uuid_sqlite(type_, compiler, **kw):  # noqa: ARG001
        return "VARCHAR(36)"

    @compiles(ARRAY, "sqlite")
    def _array_sqlite(type_, compiler, **kw):  # noqa: ARG001
        return "JSON"

    from app.db.base import Base
    from app.models.company import Company, CompanyAlias
    from app.models.documents import SourceDocument
    from app.models.facility import (
        CompanyFacility, Facility, FacilityMaterialLink,
    )
    from app.models.regulatory import (
        RiskEvent,
        RiskEventGeography,
        RiskEventHsMapping,
    )
    from app.models.scoring import HsCodeGeographyRiskScore
    from app.models.source import Source
    from app.models.supply import (
        HsCodeMaterialMapping,
        HsCodeProductionShare,
        Material,
    )

    from app.services.scoring.hs_node_scorer import (
        score_hs_node_geography,
        score_all_hs_nodes,
    )

    print("=== Option 1: hs_node_scorer event-only fallback ===\n")

    engine = create_engine("sqlite:///:memory:")
    # Note: facility tables added 2026-05-09 to support G5 operational
    # sub-score query inside score_hs_node_geography, even though this
    # test doesn't seed any facility rows (we want the operational signal
    # to be None on every path tested here).
    Base.metadata.create_all(engine, tables=[
        Source.__table__,
        SourceDocument.__table__,
        Material.__table__,
        HsCodeMaterialMapping.__table__,
        HsCodeProductionShare.__table__,
        Company.__table__,
        CompanyAlias.__table__,
        Facility.__table__,
        CompanyFacility.__table__,
        FacilityMaterialLink.__table__,
        RiskEvent.__table__,
        RiskEventGeography.__table__,
        RiskEventHsMapping.__table__,
        HsCodeGeographyRiskScore.__table__,
    ])

    with Session(engine) as session:
        # ── Scaffolding ───────────────────────────────────────────────────
        material = Material(canonical_name="Aluminum", category="metal")
        session.add(material)
        source = Source(
            name="test", source_type="trade", phase=1,
            is_active=True, config_json={},
        )
        session.add(source)
        session.flush()
        doc = SourceDocument(
            source_id=source.id, external_id="test-doc",
            title="test", document_type="trade_policy_database",
            metadata_json={},
        )
        session.add(doc)
        session.flush()

        # ── Mapping A: ORE stage with production shares + events ──────────
        # 7601.10 (refined aluminum, simulated as ore for brevity)
        # CN: 0.55, RU: 0.10, AU: 0.20, BR: 0.15
        # HHI = 0.55² + 0.10² + 0.20² + 0.15² = 0.3025 + 0.01 + 0.04 + 0.0225 = 0.375
        map_a = HsCodeMaterialMapping(
            hs_code_prefix="2606",
            material_id=material.id,
            description="Aluminum ores (bauxite)",
            confidence=0.95,
            supply_chain_stage="ore",
            digit_count=4,
            market_scope="global",
        )
        session.add(map_a)
        session.flush()

        for cc, share in [("CN", 0.55), ("RU", 0.10), ("AU", 0.20), ("BR", 0.15)]:
            session.add(HsCodeProductionShare(
                hs_mapping_id=map_a.id,
                country_code=cc,
                reference_year=2024,
                production_share=share,
                production_volume=share * 1000,
                market_scope="global",
            ))

        # ── Mapping B: REFINED stage — NO production shares, but events ───
        # This is the case from the partner question: 409 events tagged to
        # refined aluminum HS codes but no MCS production data → score=None
        # pre-fix.
        map_b = HsCodeMaterialMapping(
            hs_code_prefix="7601",
            material_id=material.id,
            description="Aluminum, unwrought",
            confidence=0.95,
            supply_chain_stage="refined",
            digit_count=4,
            market_scope="global",
        )
        session.add(map_b)
        session.flush()

        # ── Mapping C: NEITHER shares NOR events ─────────────────────────
        map_c = HsCodeMaterialMapping(
            hs_code_prefix="7616",
            material_id=material.id,
            description="Aluminum articles, other",
            confidence=0.80,
            supply_chain_stage="downstream",
            digit_count=4,
            market_scope="global",
        )
        session.add(map_c)
        session.flush()

        # ── Events for Mapping B (export + tariff, both targeting CN) ─────
        evt_dt = datetime(2024, 8, 1, tzinfo=timezone.utc)

        # Export restriction event — primary=CN
        ev_export = RiskEvent(
            source_document_id=doc.id,
            event_type="Export licensing requirement",
            event_subtype="EXPORT_RESTRICTION",
            event_date=evt_dt,
            title="China imposes aluminum export controls",
            severity_score=0.80, confidence_score=0.90,
            risk_categories_json=["geopolitical_trade"],
            content_hash="ev_export_b", verified=True,
        )
        session.add(ev_export)
        session.flush()
        session.add(RiskEventHsMapping(
            risk_event_id=ev_export.id, hs_mapping_id=map_b.id,
            relevance_score=0.95, match_reason="hs_code",
        ))
        session.add(RiskEventGeography(
            risk_event_id=ev_export.id, country_code="CN",
            geography_context="primary", relevance_score=1.0,
        ))

        # Tariff event — affected=CN (US tariff against CN)
        ev_tariff = RiskEvent(
            source_document_id=doc.id,
            event_type="Import tariff",
            event_subtype="IMPORT_DISRUPTION",
            event_date=evt_dt,
            title="US tariff on Chinese aluminum",
            severity_score=0.60, confidence_score=0.85,
            risk_categories_json=["geopolitical_trade"],
            content_hash="ev_tariff_b", verified=True,
        )
        session.add(ev_tariff)
        session.flush()
        session.add(RiskEventHsMapping(
            risk_event_id=ev_tariff.id, hs_mapping_id=map_b.id,
            relevance_score=0.95, match_reason="hs_code",
        ))
        session.add(RiskEventGeography(
            risk_event_id=ev_tariff.id, country_code="CN",
            geography_context="affected", relevance_score=1.0,
        ))

        session.commit()

        as_of = date(2024, 9, 1)

        # ── Test 1: HHI-anchored path on mapping A / CN ───────────────────
        print("Test 1: HHI-anchored path (mapping with production shares)")
        score_a = score_hs_node_geography(
            session, hs_mapping_id=map_a.id,
            country_code="CN", as_of_date=as_of,
        )
        if score_a is None:
            failures.append("Mapping A (with shares) returned None — should have scored")
        else:
            method = score_a.metadata_json.get("score_method")
            if method != "hhi_anchored":
                failures.append(
                    f"Mapping A: score_method={method}, expected 'hhi_anchored'"
                )
            else:
                print(f"  [OK] score_method = '{method}'")
            # Sanity: HHI weight present, ~0.50
            weights = score_a.metadata_json.get("weight_breakdown", {})
            if weights.get("hhi") != 0.50:
                failures.append(
                    f"Mapping A: hhi weight={weights.get('hhi')}, expected 0.50"
                )
            else:
                print(f"  [OK] weight_breakdown.hhi = {weights['hhi']}")
            # HHI sanity (=0.375)
            hhi_raw = score_a.metadata_json.get("hhi_raw")
            if hhi_raw is None or abs(hhi_raw - 0.375) > 0.001:
                failures.append(
                    f"Mapping A: hhi_raw={hhi_raw}, expected 0.375"
                )
            else:
                print(f"  [OK] hhi_raw = {hhi_raw}")
            # No tariff/export events on mapping A → composite = 50×0.375 = 18.75
            expected_composite = 50.0 * 0.375
            if abs(score_a.composite_node_score - expected_composite) > 0.01:
                failures.append(
                    f"Mapping A: composite={score_a.composite_node_score}, "
                    f"expected {expected_composite}"
                )
            else:
                print(
                    f"  [OK] HHI-anchored composite: "
                    f"{score_a.composite_node_score:.2f} (= 50×0.375)"
                )

        # ── Test 2: Event-only fallback on mapping B / CN ─────────────────
        print("\nTest 2: Event-only fallback (no production data, has events)")
        session.commit()  # commit Test 1's score before Test 2
        score_b = score_hs_node_geography(
            session, hs_mapping_id=map_b.id,
            country_code="CN", as_of_date=as_of,
        )
        if score_b is None:
            failures.append(
                "Mapping B (events only) returned None — fallback didn't fire"
            )
        else:
            method = score_b.metadata_json.get("score_method")
            if method != "event_only_no_hhi":
                failures.append(
                    f"Mapping B: score_method={method}, expected 'event_only_no_hhi'"
                )
            else:
                print(f"  [OK] score_method = '{method}'")
            # Weight breakdown: hhi=None, tariff=0.5, export=0.5
            weights = score_b.metadata_json.get("weight_breakdown", {})
            if weights.get("hhi") is not None:
                failures.append(
                    f"Mapping B: hhi weight={weights.get('hhi')}, expected None"
                )
            elif weights.get("tariff") != 0.5 or weights.get("export") != 0.5:
                failures.append(
                    f"Mapping B: tariff/export weights={weights.get('tariff')}/"
                    f"{weights.get('export')}, expected 0.5/0.5"
                )
            else:
                print(f"  [OK] weights renormalised: hhi=None, tariff=0.5, export=0.5")
            # hhi_raw should be None
            if score_b.metadata_json.get("hhi_raw") is not None:
                failures.append(
                    f"Mapping B: hhi_raw should be None, got "
                    f"{score_b.metadata_json.get('hhi_raw')}"
                )
            else:
                print(f"  [OK] hhi_raw = None")
            # production_share = 0.0 (no row for this country in shares)
            if score_b.production_share != 0.0:
                failures.append(
                    f"Mapping B: production_share={score_b.production_share}, "
                    f"expected 0.0"
                )
            # hhi_at_stage column should be NULL
            if score_b.hhi_at_stage is not None:
                failures.append(
                    f"Mapping B: hhi_at_stage={score_b.hhi_at_stage}, expected NULL"
                )
            else:
                print(f"  [OK] hhi_at_stage = NULL")
            # Composite = 0.5×tariff + 0.5×export, scaled to 0–100
            # Both events have severity 0.80 / 0.60.  We don't reproduce the
            # full formula here — just sanity-check that it's > 0 and <= 100.
            comp = score_b.composite_node_score
            if not (0 < comp <= 100):
                failures.append(
                    f"Mapping B: composite_node_score={comp}, expected 0 < x <= 100"
                )
            else:
                print(f"  [OK] event-only composite: {comp:.2f} (between 0 and 100)")
            # Both event_ids should be in the consumed list
            consumed = score_b.metadata_json.get("event_ids_consumed", [])
            if len(consumed) != 2:
                failures.append(
                    f"Mapping B: event_ids_consumed has {len(consumed)} entries, "
                    f"expected 2 (1 tariff + 1 export)"
                )
            else:
                print(f"  [OK] event_ids_consumed has both events ({len(consumed)})")

        # ── Test 3: No-data short-circuit on mapping C ────────────────────
        print("\nTest 3: No-data short-circuit (no shares, no events)")
        session.commit()
        score_c = score_hs_node_geography(
            session, hs_mapping_id=map_c.id,
            country_code="CN", as_of_date=as_of,
        )
        if score_c is not None:
            failures.append(
                f"Mapping C (no data) returned a score — expected None"
            )
        else:
            print(f"  [OK] returned None (no shares, no events)")

        # ── Test 4: Batch enumerator picks up event-only mapping ──────────
        print("\nTest 4: score_all_hs_nodes picks up event-only mappings")
        session.commit()
        # Wipe existing scores first so we count only this batch's writes
        session.query(HsCodeGeographyRiskScore).delete()
        session.commit()

        result = score_all_hs_nodes(session, as_of_date=as_of)
        # We expect at minimum:
        #   Mapping A: 4 country pairs × 1 row each = 4 (one per share country)
        #   Mapping B: CN (event-only, both export-primary AND tariff-affected
        #              produce a CN pair) = 1 row
        #   Mapping C: 0 (no data, skipped before scoring)
        scored_rows = session.query(HsCodeGeographyRiskScore).all()
        ids_with_event_only = [
            r.hs_mapping_id for r in scored_rows
            if r.metadata_json.get("score_method") == "event_only_no_hhi"
        ]
        ids_anchored = [
            r.hs_mapping_id for r in scored_rows
            if r.metadata_json.get("score_method") == "hhi_anchored"
        ]
        if map_b.id not in ids_with_event_only:
            failures.append(
                f"Mapping B (event-only) not picked up by score_all_hs_nodes; "
                f"event-only IDs: {ids_with_event_only}"
            )
        else:
            print(
                f"  [OK] Mapping B picked up via event-pair query "
                f"({len(ids_with_event_only)} event-only score(s))"
            )
        if map_a.id not in ids_anchored:
            failures.append(
                f"Mapping A (with shares) not picked up by score_all_hs_nodes"
            )
        else:
            print(
                f"  [OK] Mapping A picked up via share-pair query "
                f"({len(ids_anchored)} anchored score(s))"
            )
        if map_c.id in [r.hs_mapping_id for r in scored_rows]:
            failures.append("Mapping C should NOT have been scored")
        else:
            print(f"  [OK] Mapping C correctly skipped (no data)")
        print(
            f"  Batch result: pairs_scored={result['pairs_scored']}, "
            f"pairs_skipped={result['pairs_skipped']}, "
            f"nodes_processed={result['nodes_processed']}"
        )

    print("\n" + "=" * 60)
    if failures:
        print("FAIL:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS — Option 1 event-only fallback:")
    print("  ✓ HHI-anchored path unchanged for mappings with production shares")
    print("  ✓ Event-only fallback fires when shares missing but events exist")
    print("  ✓ score_method tag distinguishes the two paths")
    print("  ✓ Weights renormalised (0.5/0.5) when HHI absent")
    print("  ✓ No-data short-circuit returns None")
    print("  ✓ Batch enumerator picks up event-only mappings")
    return 0


if __name__ == "__main__":
    sys.exit(main())
