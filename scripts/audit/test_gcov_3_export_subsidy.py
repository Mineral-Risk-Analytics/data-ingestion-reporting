"""End-to-end test for G-Cov-3 — EXPORT_SUBSIDY event_subtype + geopolitical
sub-input (2026-05-09).

Verifies:

  1. GTA parser: ``_INTERVENTION_SUBTYPE_MAP`` tags subsidy-class
     intervention_type strings as ``EXPORT_SUBSIDY`` (8 GTA subsidy
     intervention types + 3 production-subsidy synonyms).
  2. IEA Policy Tracker: ``_derive_event_subtype`` returns
     ``"EXPORT_SUBSIDY"`` for INVESTMENT_PLEDGE events and ``None`` for
     POLICY_MILESTONE events.
  3. ``_classify_geo_events`` splits events into three buckets
     (export / tariff / subsidy) — subsidy events don't leak into the
     tariff or export buckets.
  4. ``score_geopolitical_trade`` runs the 3-component profile when
     ``production_subsidy_distortion`` is None (backward compat) and the
     4-component profile (0.40/0.30/0.20/0.10) when any numeric value is
     provided.
  5. ``_derive_market_geopolitical_inputs`` returns a 5-tuple; subsidy
     value is None when no subsidy events exist for the (material × geo)
     pair, and a 0-1 float when at least one exists.
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
    from app.models.documents import SourceDocument
    from app.models.regulatory import (
        RiskEvent, RiskEventGeography, RiskEventMaterial,
    )
    from app.models.source import Source
    from app.models.supply import Material, MaterialProductionShare
    from app.constants import RiskCategory

    from app.services.ingestion.gta import _INTERVENTION_SUBTYPE_MAP
    from app.services.ingestion.iea_policy_tracker import _derive_event_subtype
    from app.services.scoring.geopolitical_risk import score_geopolitical_trade
    from app.services.scoring.market_aggregator import (
        _classify_geo_events,
        _derive_market_geopolitical_inputs,
        EventWithRelevance,
    )

    print("=== G-Cov-3: EXPORT_SUBSIDY subtype + Geopolitical sub-input ===\n")

    # ── Test 1: GTA subtype map covers all subsidy intervention types ────
    print("Test 1: GTA _INTERVENTION_SUBTYPE_MAP has EXPORT_SUBSIDY entries")
    expected_subsidy_inputs = {
        "Trade finance", "State loan", "Financial assistance in foreign market",
        "Loan guarantee", "Financial grant", "State aid, unspecified",
        "Equity stake", "Financial investment support",
        "Production subsidy", "Production subsidies", "Producer support",
    }
    missing = [
        k for k in expected_subsidy_inputs
        if _INTERVENTION_SUBTYPE_MAP.get(k) != "EXPORT_SUBSIDY"
    ]
    if missing:
        failures.append(
            f"GTA subtype map missing/wrong EXPORT_SUBSIDY for: {missing}"
        )
    else:
        print(f"  [OK] all {len(expected_subsidy_inputs)} subsidy intervention "
              f"types map to EXPORT_SUBSIDY")
    # Confirm we didn't accidentally clobber existing mappings
    if _INTERVENTION_SUBTYPE_MAP.get("Export ban") != "EXPORT_RESTRICTION":
        failures.append("Export ban no longer maps to EXPORT_RESTRICTION — regression")
    if _INTERVENTION_SUBTYPE_MAP.get("Import tariff") != "IMPORT_DISRUPTION":
        failures.append("Import tariff no longer maps to IMPORT_DISRUPTION — regression")

    # ── Test 2: IEA event_subtype derivation ─────────────────────────────
    print("\nTest 2: IEA Policy Tracker _derive_event_subtype")
    cases = [
        ("INVESTMENT_PLEDGE", "EXPORT_SUBSIDY"),
        ("POLICY_MILESTONE",  None),
        ("UNKNOWN_TYPE",      None),
    ]
    for et, expected in cases:
        got = _derive_event_subtype(et)
        if got != expected:
            failures.append(f"_derive_event_subtype({et!r}) = {got!r}, expected {expected!r}")
        else:
            print(f"  [OK] event_type={et!r:<22} → subtype={got!r}")

    # ── Test 3: 3-bucket classifier ──────────────────────────────────────
    print("\nTest 3: _classify_geo_events splits into (export, tariff, subsidy)")
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[
        Source.__table__, SourceDocument.__table__, Material.__table__,
        MaterialProductionShare.__table__,
        RiskEvent.__table__, RiskEventGeography.__table__,
        RiskEventMaterial.__table__,
    ])
    with Session(engine) as s:
        material = Material(canonical_name="Lithium", category="cathode_active")
        s.add(material)
        src = Source(name="test", source_type="trade", phase=1,
                     is_active=True, config_json={})
        s.add(src); s.flush()
        doc = SourceDocument(source_id=src.id, external_id="t",
                             title="t", document_type="trade_policy_database",
                             metadata_json={})
        s.add(doc); s.flush()

        # Add subsidy production share for CN so country_concentration > 0
        s.add(MaterialProductionShare(
            material_id=material.id, country_code="CN",
            reference_year=2024, production_share=0.5,
        ))

        events = []
        for i, (subtype, title) in enumerate([
            ("EXPORT_SUBSIDY",    "China EXIM Bank finances battery export"),
            ("EXPORT_RESTRICTION","China imposes graphite export controls"),
            ("IMPORT_DISRUPTION", "US tariff on Chinese lithium"),
            ("EXPORT_SUBSIDY",    "State loan to lithium refiner"),
        ]):
            ev = RiskEvent(
                source_document_id=doc.id,
                event_type=f"test_{i}", event_subtype=subtype,
                event_date=datetime(2024, 8, 1, tzinfo=timezone.utc),
                title=title, severity_score=0.7, confidence_score=0.9,
                risk_categories_json=["geopolitical_trade"],
                content_hash=f"ev_{i}", verified=True,
            )
            s.add(ev); s.flush()
            s.add(RiskEventMaterial(
                risk_event_id=ev.id, material_id=material.id,
                relevance_score=0.95, match_reason="hs_code",
            ))
            s.add(RiskEventGeography(
                risk_event_id=ev.id, country_code="CN",
                geography_context="primary", relevance_score=1.0,
            ))
            events.append(EventWithRelevance(event=ev, relevance_score=0.95))
        s.commit()

        exp, tar, sub = _classify_geo_events(events)
        if len(sub) != 2:
            failures.append(f"subsidy bucket has {len(sub)} events, expected 2")
        elif len(exp) != 1:
            failures.append(f"export bucket has {len(exp)} events, expected 1")
        elif len(tar) != 1:
            failures.append(f"tariff bucket has {len(tar)} events, expected 1")
        else:
            print(f"  [OK] split: export={len(exp)}, tariff={len(tar)}, "
                  f"subsidy={len(sub)}")
        # Confirm subsidy events did NOT leak into export bucket
        if any("EXIM" in (ew.event.title or "") for ew in exp):
            failures.append("EXIM event leaked into export bucket")
        if any("State loan" in (ew.event.title or "") for ew in tar):
            failures.append("State-loan event leaked into tariff bucket")

        # ── Test 4: 6-tuple return from _derive_market_geopolitical_inputs ──
        # Bumped from 5-tuple → 6-tuple on 2026-06-06 (11.4-Geo): the
        # function now also returns sub_input_diagnostic.
        print("\nTest 4: _derive_market_geopolitical_inputs returns subsidy term")
        result = _derive_market_geopolitical_inputs(
            s, material.id, "CN", events, date(2024, 9, 1),
            eligible_nodes=None,
        )
        if len(result) != 6:
            failures.append(f"function returned {len(result)}-tuple, expected 6")
        else:
            ctry_conc, exp_rest, tariff, sub_dist, method, diag = result
            if sub_dist is None:
                failures.append(
                    "subsidy_distortion is None but 2 EXPORT_SUBSIDY events exist for CN"
                )
            elif not (0.0 <= sub_dist <= 1.0):
                failures.append(f"subsidy_distortion={sub_dist} outside [0,1]")
            else:
                print(f"  [OK] returns 6-tuple; subsidy_distortion={sub_dist:.4f}")
            # 11.4-Geo: diagnostic dict shape sanity-check.
            if not isinstance(diag, dict) or "subsidy" not in diag:
                failures.append("sub_input_diagnostic dict missing 'subsidy' key")
            elif diag["subsidy"]["data_backed"] is not True:
                failures.append(
                    "subsidy.data_backed=False but EXPORT_SUBSIDY events exist"
                )

        # And: no subsidy events for a different country → subsidy stays None
        result_us = _derive_market_geopolitical_inputs(
            s, material.id, "US", [], date(2024, 9, 1),
            eligible_nodes=None,
        )
        if result_us[3] is not None:
            failures.append(
                f"subsidy_distortion={result_us[3]} for US (no events) — expected None"
            )
        else:
            print(f"  [OK] subsidy_distortion=None when no subsidy events present")
            # 11.4-Geo: confirm diagnostic flags the asymmetric-default state.
            if result_us[5]["subsidy"]["data_backed"] is not False:
                failures.append("subsidy.data_backed=True but no events present")
            if result_us[5]["subsidy"]["scoring_profile"] != "3_component":
                failures.append(
                    f"subsidy.scoring_profile={result_us[5]['subsidy']['scoring_profile']}, "
                    "expected '3_component' when subsidy is None"
                )

    # ── Test 5: score_geopolitical_trade 3-arg vs 4-arg semantics ────────
    print("\nTest 5: score_geopolitical_trade backwards-compat + 4-component")
    # 3-arg (None subsidy_distortion) → 3-component profile (0.40/0.35/0.25)
    s3 = score_geopolitical_trade(0.50, 0.30, 0.20)
    # 0.40*50 + 0.35*30 + 0.25*20 = 20 + 10.5 + 5 = 35.5
    if abs(s3 - 35.5) > 0.01:
        failures.append(f"3-arg score = {s3}, expected 35.5")
    else:
        print(f"  [OK] 3-arg score = {s3:.2f} (0.40/0.35/0.25 weights)")

    # 4-arg with subsidy=0.5 → 4-component profile (0.40/0.30/0.20/0.10)
    s4 = score_geopolitical_trade(0.50, 0.30, 0.20, production_subsidy_distortion=0.5)
    # 0.40*50 + 0.30*30 + 0.20*20 + 0.10*50 = 20 + 9 + 4 + 5 = 38
    if abs(s4 - 38.0) > 0.01:
        failures.append(f"4-arg score = {s4}, expected 38.0")
    else:
        print(f"  [OK] 4-arg score = {s4:.2f} (0.40/0.30/0.20/0.10 weights, subsidy=0.5)")

    # Passing 0.0 should still trigger 4-component profile (not skip the term)
    s4_zero = score_geopolitical_trade(0.50, 0.30, 0.20, production_subsidy_distortion=0.0)
    # 0.40*50 + 0.30*30 + 0.20*20 + 0.10*0 = 20 + 9 + 4 + 0 = 33
    if abs(s4_zero - 33.0) > 0.01:
        failures.append(f"4-arg score (subsidy=0) = {s4_zero}, expected 33.0")
    else:
        print(f"  [OK] 4-arg score (subsidy=0.0) = {s4_zero:.2f} "
              f"(4-component fires even at subsidy=0)")

    print("\n" + "=" * 60)
    if failures:
        print("FAIL:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS — G-Cov-3 EXPORT_SUBSIDY + geopolitical sub-input:")
    print("  ✓ GTA subtype map covers 11 subsidy-class intervention types")
    print("  ✓ IEA INVESTMENT_PLEDGE events tagged EXPORT_SUBSIDY")
    print("  ✓ Classifier splits into (export, tariff, subsidy) without leak")
    print("  ✓ _derive_market_geopolitical_inputs returns subsidy term in tuple")
    print("  ✓ Subsidy=None preserves 3-component profile (backwards-compat)")
    print("  ✓ Subsidy=numeric triggers 4-component profile (0.40/0.30/0.20/0.10)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
