"""End-to-end test for G5 — HS-node operational sub-score (2026-05-09).

Verifies all five scoring paths in ``score_hs_node_geography``:

  1. ``hhi_anchored``                  — HHI + tariff + export, no facility data
  2. ``hhi_anchored_with_operational`` — HHI + tariff + export + operational
  3. ``event_only_no_hhi``             — events only (Option-1 fallback)
  4. ``event_only_with_operational``   — events + operational, no HHI
  5. ``operational_only``              — facility data only, no shares/events

Plus the batch enumerator: ``score_all_hs_nodes`` should now pick up
mappings whose only signal is facility capacity data.
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
        Facility, CompanyFacility, FacilityMaterialLink,
    )
    from app.models.regulatory import (
        RiskEvent, RiskEventGeography, RiskEventHsMapping,
    )
    from app.models.scoring import HsCodeGeographyRiskScore
    from app.models.source import Source
    from app.models.supply import (
        Material, HsCodeMaterialMapping, HsCodeProductionShare,
    )

    from app.services.scoring.hs_node_scorer import (
        score_hs_node_geography,
        score_all_hs_nodes,
        _compute_operational_signal,
    )

    print("=== G5: HS-node operational sub-score ===\n")

    engine = create_engine("sqlite:///:memory:")
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
        material = Material(canonical_name="Lithium", category="cathode_active")
        session.add(material)
        source = Source(name="test", source_type="trade", phase=1,
                        is_active=True, config_json={})
        session.add(source)
        session.flush()
        doc = SourceDocument(
            source_id=source.id, external_id="test-doc",
            title="test", document_type="trade_policy_database",
            metadata_json={},
        )
        session.add(doc)
        session.flush()

        # Five distinct HS mappings — one per scoring path
        mappings = {}
        for stage, hs in (
            ("ore",                "2530"),   # path 1 + 2 (hhi anchored)
            ("intermediate",       "282739"),  # path 4 (event + operational)
            ("refined",            "2825"),   # path 3 (event-only)
            ("battery_grade",      "2836"),   # path 5 (operational-only)
            ("scrap",              "8104"),    # never scored — no data
        ):
            m = HsCodeMaterialMapping(
                hs_code_prefix=hs, material_id=material.id,
                description=f"{stage} stage", confidence=0.9,
                supply_chain_stage=stage, digit_count=4, market_scope="global",
            )
            session.add(m)
            session.flush()
            mappings[stage] = m

        # Production shares for ore stage only — drives HHI for paths 1/2
        for cc, share in [("AU", 0.55), ("CN", 0.20), ("CL", 0.15), ("AR", 0.10)]:
            session.add(HsCodeProductionShare(
                hs_mapping_id=mappings["ore"].id,
                country_code=cc, reference_year=2024,
                production_share=share, production_volume=share * 1000,
                market_scope="global",
            ))

        # Events for ore (CN), intermediate (CN), refined (CN)
        evt_dt = datetime(2024, 8, 1, tzinfo=timezone.utc)
        for stage, mat_id_attr, has_tariff, has_export in [
            ("ore",          mappings["ore"].id,          True,  True),
            ("intermediate", mappings["intermediate"].id, True,  True),
            ("refined",      mappings["refined"].id,      True,  True),
        ]:
            for kind in ("tariff", "export"):
                ev = RiskEvent(
                    source_document_id=doc.id,
                    event_type=f"{kind}_{stage}",
                    event_subtype=("IMPORT_DISRUPTION" if kind == "tariff"
                                   else "EXPORT_RESTRICTION"),
                    event_date=evt_dt,
                    title=f"{kind} event for {stage}",
                    severity_score=0.7, confidence_score=0.9,
                    risk_categories_json=["geopolitical_trade"],
                    content_hash=f"ev_{stage}_{kind}",
                    verified=True,
                )
                session.add(ev)
                session.flush()
                session.add(RiskEventHsMapping(
                    risk_event_id=ev.id, hs_mapping_id=mat_id_attr,
                    relevance_score=0.95, match_reason="hs_code",
                ))
                session.add(RiskEventGeography(
                    risk_event_id=ev.id, country_code="CN",
                    geography_context=("affected" if kind == "tariff" else "primary"),
                    relevance_score=1.0,
                ))

        # Facilities for ore (CN), intermediate (CN), battery_grade (CN)
        # — with mixed status to give a non-trivial operational signal
        company = Company(canonical_name="TestLi Corp", verified=True)
        session.add(company)
        session.flush()
        for stage, capacities in [
            # (status, capacity) tuples — at_risk if mothballed/closed
            ("ore",           [("operating", 100_000), ("mothballed", 30_000)]),  # 23.1% at risk
            ("intermediate",  [("operating",  50_000), ("closed",      50_000)]),  # 50% at risk
            ("battery_grade", [("operating",  10_000), ("care_maintenance", 40_000)]),  # 80% at risk
        ]:
            for i, (status, cap) in enumerate(capacities):
                f = Facility(
                    facility_type=("mine" if stage == "ore" else "refinery"),
                    name=f"Facility {stage} {i}", country="CN",
                    status=status, verified=True,
                )
                session.add(f)
                session.flush()
                session.add(CompanyFacility(
                    company_id=company.id, facility_id=f.id,
                    ownership_type="operator", ownership_pct=1.0,
                ))
                session.add(FacilityMaterialLink(
                    facility_id=f.id, material_id=material.id,
                    annual_capacity_tpy=cap, capacity_unit="t/yr",
                    is_primary_product=True, supply_chain_stage=stage,
                ))

        session.commit()
        as_of = date(2024, 9, 1)

        # ── Test 1: helper function correctness ───────────────────────────
        print("Test 1: _compute_operational_signal correctness")
        signal, count, at_risk, total = _compute_operational_signal(
            session, material_id=material.id, country_code="CN",
            supply_chain_stage="ore",
        )
        # ore stage: 30k mothballed + 100k operating = 30/130 = 0.231
        if signal is None or abs(signal - 0.231) > 0.01:
            failures.append(f"Test 1 ore: signal={signal}, expected ~0.231")
        else:
            print(f"  [OK] ore stage: signal={signal:.3f} ({count} facilities, "
                  f"{at_risk:.0f}/{total:.0f} t/yr at risk)")

        signal, count, at_risk, total = _compute_operational_signal(
            session, material_id=material.id, country_code="CN",
            supply_chain_stage="battery_grade",
        )
        # battery_grade: 40k care_maintenance / 50k total = 0.800
        if signal is None or abs(signal - 0.800) > 0.01:
            failures.append(f"Test 1 bg: signal={signal}, expected 0.800")
        else:
            print(f"  [OK] battery_grade: signal={signal:.3f} ({count} facilities)")

        # No facility data for refined stage → returns None
        signal, count, _, _ = _compute_operational_signal(
            session, material_id=material.id, country_code="CN",
            supply_chain_stage="refined",
        )
        if signal is not None:
            failures.append(f"Test 1 refined: signal={signal}, expected None")
        else:
            print(f"  [OK] refined (no facilities): signal=None (count={count})")

        # ── Test 2: hhi_anchored_with_operational (path 2) ────────────────
        print("\nTest 2: hhi_anchored_with_operational (4-component path)")
        score_ore_cn = score_hs_node_geography(
            session, hs_mapping_id=mappings["ore"].id,
            country_code="CN", as_of_date=as_of,
        )
        session.commit()
        if score_ore_cn is None:
            failures.append("Test 2: ore × CN returned None")
        else:
            method = score_ore_cn.metadata_json.get("score_method")
            if method != "hhi_anchored_with_operational":
                failures.append(
                    f"Test 2: score_method={method}, expected hhi_anchored_with_operational"
                )
            else:
                print(f"  [OK] score_method = '{method}'")
            weights = score_ore_cn.metadata_json.get("weight_breakdown", {})
            expected = {"hhi": 0.40, "tariff": 0.20, "export": 0.20, "operational": 0.20}
            if {k: weights.get(k) for k in expected} != expected:
                failures.append(f"Test 2: weights = {weights}, expected {expected}")
            else:
                print(f"  [OK] weights = HHI 0.40 / tariff 0.20 / export 0.20 / op 0.20")
            op_sig = score_ore_cn.metadata_json.get("operational_signal")
            if op_sig is None or abs(op_sig - 0.231) > 0.01:
                failures.append(f"Test 2: operational_signal = {op_sig}, expected ~0.231")
            else:
                print(f"  [OK] operational_signal = {op_sig:.3f}")
            print(f"  composite score = {score_ore_cn.composite_node_score:.2f}")

        # ── Test 3: hhi_anchored without operational (path 1) ─────────────
        print("\nTest 3: hhi_anchored — country with HHI but NO facility data")
        # Use AU (Australia) — has production share but no AU facilities
        score_ore_au = score_hs_node_geography(
            session, hs_mapping_id=mappings["ore"].id,
            country_code="AU", as_of_date=as_of,
        )
        session.commit()
        if score_ore_au is None:
            failures.append("Test 3: ore × AU returned None")
        else:
            method = score_ore_au.metadata_json.get("score_method")
            if method != "hhi_anchored":
                failures.append(
                    f"Test 3: score_method={method}, expected hhi_anchored (no facility data for AU)"
                )
            else:
                print(f"  [OK] score_method = '{method}' (no AU facility data)")
            weights = score_ore_au.metadata_json.get("weight_breakdown", {})
            if weights.get("operational") is not None:
                failures.append(
                    f"Test 3: operational weight should be None, got {weights.get('operational')}"
                )
            else:
                print(f"  [OK] operational weight = None")

        # ── Test 4: event_only_with_operational (path 4) ──────────────────
        print("\nTest 4: event_only_with_operational")
        # intermediate × CN: events present, facility data present, NO production shares
        score_int_cn = score_hs_node_geography(
            session, hs_mapping_id=mappings["intermediate"].id,
            country_code="CN", as_of_date=as_of,
        )
        session.commit()
        if score_int_cn is None:
            failures.append("Test 4: intermediate × CN returned None")
        else:
            method = score_int_cn.metadata_json.get("score_method")
            if method != "event_only_with_operational":
                failures.append(
                    f"Test 4: score_method={method}, expected event_only_with_operational"
                )
            else:
                print(f"  [OK] score_method = '{method}'")
            weights = score_int_cn.metadata_json.get("weight_breakdown", {})
            expected = {"hhi": None, "tariff": 0.40, "export": 0.40, "operational": 0.20}
            if {k: weights.get(k) for k in expected} != expected:
                failures.append(f"Test 4: weights = {weights}, expected {expected}")
            else:
                print(f"  [OK] weights = tariff 0.40 / export 0.40 / op 0.20 (no HHI)")

        # ── Test 5: event_only_no_hhi (path 3) — no operational data ──────
        print("\nTest 5: event_only_no_hhi — events + no facility, no shares")
        # refined × CN: events present, NO facility data for refined, no shares
        score_ref_cn = score_hs_node_geography(
            session, hs_mapping_id=mappings["refined"].id,
            country_code="CN", as_of_date=as_of,
        )
        session.commit()
        if score_ref_cn is None:
            failures.append("Test 5: refined × CN returned None")
        else:
            method = score_ref_cn.metadata_json.get("score_method")
            if method != "event_only_no_hhi":
                failures.append(
                    f"Test 5: score_method={method}, expected event_only_no_hhi"
                )
            else:
                print(f"  [OK] score_method = '{method}'")

        # ── Test 6: operational_only (path 5) ─────────────────────────────
        print("\nTest 6: operational_only — facility data only, no shares/events")
        # battery_grade × CN: facility data present, no shares, no events
        score_bg_cn = score_hs_node_geography(
            session, hs_mapping_id=mappings["battery_grade"].id,
            country_code="CN", as_of_date=as_of,
        )
        session.commit()
        if score_bg_cn is None:
            failures.append("Test 6: battery_grade × CN returned None")
        else:
            method = score_bg_cn.metadata_json.get("score_method")
            if method != "operational_only":
                failures.append(
                    f"Test 6: score_method={method}, expected operational_only"
                )
            else:
                print(f"  [OK] score_method = '{method}'")
            # composite = operational_signal × 100 = 80
            if abs(score_bg_cn.composite_node_score - 80.0) > 1.0:
                failures.append(
                    f"Test 6: composite={score_bg_cn.composite_node_score}, expected ~80"
                )
            else:
                print(f"  [OK] composite = {score_bg_cn.composite_node_score:.2f} "
                      f"(80% of capacity at risk)")

        # ── Test 7: no-data short-circuit ─────────────────────────────────
        print("\nTest 7: no-data path — scrap stage with nothing")
        score_scrap = score_hs_node_geography(
            session, hs_mapping_id=mappings["scrap"].id,
            country_code="CN", as_of_date=as_of,
        )
        session.commit()
        if score_scrap is not None:
            failures.append(f"Test 7: scrap × CN should be None, got composite={score_scrap.composite_node_score}")
        else:
            print(f"  [OK] returned None (no shares, no events, no facilities)")

        # ── Test 8: batch enumerator picks up facility-only pairs ─────────
        print("\nTest 8: score_all_hs_nodes picks up operational_only mappings")
        session.commit()
        session.query(HsCodeGeographyRiskScore).delete()
        session.commit()
        result = score_all_hs_nodes(session, as_of_date=as_of)
        # Expect: ore × {AU,CN,CL,AR} (4) + intermediate × CN (1) +
        # refined × CN (1) + battery_grade × CN (1) = 7
        scored_rows = session.query(HsCodeGeographyRiskScore).all()
        methods_seen = {
            r.metadata_json.get("score_method") for r in scored_rows
        }
        expected_methods = {
            "hhi_anchored",                   # ore × {AU,CL,AR}
            "hhi_anchored_with_operational",  # ore × CN
            "event_only_with_operational",    # intermediate × CN
            "event_only_no_hhi",              # refined × CN
            "operational_only",               # battery_grade × CN
        }
        if methods_seen != expected_methods:
            missing = expected_methods - methods_seen
            extra = methods_seen - expected_methods
            failures.append(
                f"Test 8: methods seen = {methods_seen}; missing={missing}, extra={extra}"
            )
        else:
            print(f"  [OK] all 5 score_method values present: {sorted(methods_seen)}")
        print(f"  Batch result: pairs_scored={result['pairs_scored']}, "
              f"pairs_skipped={result['pairs_skipped']}, "
              f"nodes_processed={result['nodes_processed']}")

    print("\n" + "=" * 60)
    if failures:
        print("FAIL:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS — G5 operational sub-score:")
    print("  ✓ _compute_operational_signal computes at_risk/total fraction by stage")
    print("  ✓ hhi_anchored_with_operational fires when all 4 components present")
    print("  ✓ hhi_anchored falls back when no facility data")
    print("  ✓ event_only_with_operational fires for events + facility, no HHI")
    print("  ✓ event_only_no_hhi unchanged from Option-1 fallback")
    print("  ✓ operational_only fires when only facility data exists")
    print("  ✓ no-data short-circuit returns None")
    print("  ✓ batch enumerator picks up facility-only mappings")
    return 0


if __name__ == "__main__":
    sys.exit(main())
