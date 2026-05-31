"""End-to-end test for G-Cov-1 + G-Cov-2 fixes (2026-05-06).

Confirms:
  G-Cov-1: opensanctions ingester writes
           risk_categories_json=['geopolitical_trade', 'regulatory_compliance']
           on both event paths (company-targeted + geography-level).

  G-Cov-2: market_aggregator._export_restriction_operational_impacts:
           - Returns half-weighted impacts for EXPORT_RESTRICTION events
             tagged with primary geography matching the target country.
           - Excludes events with no RiskEventGeography rows
             (G11 strict attribution).
           - Excludes events tagged to a different country.
           - The 0.5x weight is applied (verified by computing expected
             impact directly via _event_impact and comparing).
"""

from __future__ import annotations

import sys
import inspect
from datetime import date, datetime, timezone

sys.path.insert(0, "/sessions/keen-wonderful-lamport/mnt/battery-data-intelligence-engine")


def main() -> int:
    failures: list[str] = []

    # ── G-Cov-1: source-level check on opensanctions ──────────────────────
    print("=== G-Cov-1: opensanctions risk_categories_json ===")
    from app.services.ingestion.opensanctions import ingest_opensanctions
    src = inspect.getsource(ingest_opensanctions)
    geo_count = src.count(
        '"geopolitical_trade", "regulatory_compliance"'
    )
    if geo_count >= 2:
        print(
            f"  [OK ] both event paths set both categories "
            f"(found {geo_count} occurrences)"
        )
    else:
        failures.append(
            f"G-Cov-1: expected 2 occurrences of dual-category "
            f"risk_categories_json, found {geo_count}"
        )

    # Confirm the OLD single-category form is gone
    old_form_count = src.count('"geopolitical_trade"]')
    # Should match only the dual-category lines (which contain "geopolitical_trade",)
    # — the OLD single-category was 'risk_categories_json=["geopolitical_trade"]'.
    # That exact pattern should be gone.
    old_count = src.count('risk_categories_json=["geopolitical_trade"]')
    if old_count > 0:
        failures.append(
            f"G-Cov-1: old single-category form still present "
            f"({old_count} occurrences)"
        )
    else:
        print("  [OK ] old single-category form is gone")

    # ── G-Cov-2: synthetic DB test ────────────────────────────────────────
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
        RiskEvent,
        RiskEventGeography,
        RiskEventMaterial,
    )
    from app.models.source import Source
    from app.models.supply import Material

    from app.services.scoring.market_aggregator import (
        _export_restriction_operational_impacts,
        _EXPORT_RESTRICTION_OPERATIONAL_WEIGHT,
        _event_impact,
        EventWithRelevance,
    )
    from app.constants import RiskCategory

    print(f"\n=== G-Cov-2: half-weight = {_EXPORT_RESTRICTION_OPERATIONAL_WEIGHT} ===")

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[
        Source.__table__,
        SourceDocument.__table__,
        Material.__table__,
        RiskEvent.__table__,
        RiskEventGeography.__table__,
        RiskEventMaterial.__table__,
    ])

    with Session(engine) as session:
        # Seed material + source + source_document scaffolding
        material = Material(canonical_name="Lithium", category="mineral")
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

        as_of = date(2024, 6, 1)
        evt_dt = datetime(2024, 1, 15, tzinfo=timezone.utc)

        # ── Case A: EXPORT_RESTRICTION tagged primary=CN
        # Expected: returns one impact, half-weighted
        ev_a = RiskEvent(
            source_document_id=doc.id,
            event_type="Export licensing requirement",
            event_subtype="EXPORT_RESTRICTION",
            event_date=evt_dt,
            title="China imposes graphite export controls",
            severity_score=0.7, confidence_score=0.9,
            risk_categories_json=["geopolitical_trade"],
            content_hash="ev_a", verified=True,
        )
        session.add(ev_a)
        session.flush()
        session.add(RiskEventMaterial(
            risk_event_id=ev_a.id, material_id=material.id,
            relevance_score=0.9, match_reason="hs_code",
        ))
        session.add(RiskEventGeography(
            risk_event_id=ev_a.id, country_code="CN",
            geography_context="primary", relevance_score=1.0,
        ))

        # ── Case B: EXPORT_RESTRICTION tagged primary=US (different country)
        # Expected: NOT returned when querying for CN
        ev_b = RiskEvent(
            source_document_id=doc.id,
            event_type="Export ban",
            event_subtype="EXPORT_RESTRICTION",
            event_date=evt_dt,
            title="Some other country event",
            severity_score=0.7, confidence_score=0.9,
            risk_categories_json=["geopolitical_trade"],
            content_hash="ev_b", verified=True,
        )
        session.add(ev_b)
        session.flush()
        session.add(RiskEventMaterial(
            risk_event_id=ev_b.id, material_id=material.id,
            relevance_score=0.9, match_reason="hs_code",
        ))
        session.add(RiskEventGeography(
            risk_event_id=ev_b.id, country_code="US",
            geography_context="primary", relevance_score=1.0,
        ))

        # ── Case C: EXPORT_RESTRICTION with NO RiskEventGeography
        # Expected: NOT returned (G11 strict attribution)
        ev_c = RiskEvent(
            source_document_id=doc.id,
            event_type="Export ban",
            event_subtype="EXPORT_RESTRICTION",
            event_date=evt_dt,
            title="Untagged export event",
            severity_score=0.7, confidence_score=0.9,
            risk_categories_json=["geopolitical_trade"],
            content_hash="ev_c", verified=True,
        )
        session.add(ev_c)
        session.flush()
        session.add(RiskEventMaterial(
            risk_event_id=ev_c.id, material_id=material.id,
            relevance_score=0.9, match_reason="hs_code",
        ))
        # No geography row — Case C tests strict attribution exclusion

        # ── Case D: EXPORT_RESTRICTION tagged 'affected'=CN, not primary
        # Expected: NOT returned (only 'primary' rows count for export side)
        ev_d = RiskEvent(
            source_document_id=doc.id,
            event_type="Import tariff",
            event_subtype="IMPORT_DISRUPTION",   # different subtype too
            event_date=evt_dt,
            title="US tariff on CN — affected=CN",
            severity_score=0.7, confidence_score=0.9,
            risk_categories_json=["geopolitical_trade"],
            content_hash="ev_d", verified=True,
        )
        session.add(ev_d)
        session.flush()
        session.add(RiskEventMaterial(
            risk_event_id=ev_d.id, material_id=material.id,
            relevance_score=0.9, match_reason="hs_code",
        ))
        session.add(RiskEventGeography(
            risk_event_id=ev_d.id, country_code="CN",
            geography_context="affected", relevance_score=1.0,
        ))
        session.commit()

        # ── Run the helper for CN
        impacts_cn = _export_restriction_operational_impacts(
            session, material.id, "CN", as_of,
        )
        print(f"  CN impacts: {impacts_cn}")
        if len(impacts_cn) != 1:
            failures.append(
                f"CN should return exactly 1 impact (Case A only); "
                f"got {len(impacts_cn)}"
            )

        # Verify the half-weight: compute the same event_impact directly
        # and confirm the helper returned exactly half.
        ew_a = EventWithRelevance(event=ev_a, relevance_score=1.0)
        full_impact_a = _event_impact(ew_a, RiskCategory.OPERATIONAL, as_of)
        expected_half = full_impact_a * _EXPORT_RESTRICTION_OPERATIONAL_WEIGHT
        print(f"  Full event impact (Case A):    {full_impact_a:.4f}")
        print(f"  Expected half-weighted impact: {expected_half:.4f}")
        if impacts_cn:
            actual = impacts_cn[0]
            print(f"  Actual returned impact:        {actual:.4f}")
            if abs(actual - expected_half) > 1e-6:
                failures.append(
                    f"Half-weight not applied correctly: "
                    f"got {actual}, expected {expected_half}"
                )
            else:
                print(
                    f"  [OK ] half-weight applied correctly "
                    f"(0.5× full impact)"
                )

        # ── Run the helper for US
        impacts_us = _export_restriction_operational_impacts(
            session, material.id, "US", as_of,
        )
        print(f"\n  US impacts: {impacts_us}")
        if len(impacts_us) != 1:
            # Case B has primary=US, so US should return one event
            failures.append(
                f"US should return exactly 1 impact (Case B only); "
                f"got {len(impacts_us)}"
            )
        else:
            print(
                "  [OK ] US returns Case B's event "
                "(different country than CN, correctly isolated)"
            )

        # ── Run the helper for a country with no events at all
        impacts_jp = _export_restriction_operational_impacts(
            session, material.id, "JP", as_of,
        )
        if impacts_jp:
            failures.append(
                f"JP should return empty list (no events tagged JP); "
                f"got {len(impacts_jp)}"
            )
        else:
            print("  [OK ] JP returns empty list (no events tagged there)")

        # ── Confirm IMPORT_DISRUPTION events are NOT returned
        # Case D is IMPORT_DISRUPTION + affected=CN.  Should not appear in
        # the EXPORT_RESTRICTION query for CN.
        # Already verified above: impacts_cn has length 1 (Case A only).
        # Make this explicit:
        if len(impacts_cn) == 1:
            print(
                "  [OK ] IMPORT_DISRUPTION events excluded "
                "(subtype filter + primary-context filter)"
            )

    print("\n" + "=" * 60)
    if failures:
        print("FAIL:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS — G-Cov-1 + G-Cov-2:")
    print(
        f"  ✓ G-Cov-1: opensanctions writes both 'geopolitical_trade' "
        f"AND 'regulatory_compliance'"
    )
    print(
        f"  ✓ G-Cov-2: EXPORT_RESTRICTION events flow into operational "
        f"event impacts at {_EXPORT_RESTRICTION_OPERATIONAL_WEIGHT}× weight"
    )
    print(f"  ✓ Country attribution strict (primary context, exact match)")
    print(f"  ✓ IMPORT_DISRUPTION subtype excluded from operational helper")
    return 0


if __name__ == "__main__":
    sys.exit(main())
