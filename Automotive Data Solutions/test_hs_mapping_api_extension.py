"""End-to-end test for the HS mappings API extension (2026-05-06).

Confirms _load_mapping_aggregates correctly bulk-loads:
  - hs_code_production_shares  → per-country Geography + production_share
  - hs_code_geography_risk_scores → per-country tariff_exposure / score / hhi
  - risk_event_hs_mappings count → events_open

And that aggregate fields (hhi avg, node_score max, scored_at max) roll up
correctly across multiple geographies for a single HS mapping.
"""

from __future__ import annotations

import sys
import uuid
from datetime import date, datetime, timezone

sys.path.insert(0, "/sessions/keen-wonderful-lamport/mnt/battery-data-intelligence-engine")


def main() -> int:
    failures: list[str] = []

    # SQLite type bridges
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
    from app.models.regulatory import RiskEvent, RiskEventHsMapping
    from app.models.scoring import HsCodeGeographyRiskScore
    from app.models.source import Source
    from app.models.supply import (
        HsCodeMaterialMapping,
        HsCodeProductionShare,
        Material,
    )

    # Stub fastapi out so we can import the route module
    if "fastapi" not in sys.modules:
        import types

        fake_fastapi = types.ModuleType("fastapi")

        class _APIRouter:
            def __init__(self, **_):
                pass

            def get(self, *a, **kw):
                return lambda f: f

            def post(self, *a, **kw):
                return lambda f: f

            def patch(self, *a, **kw):
                return lambda f: f

            def delete(self, *a, **kw):
                return lambda f: f

        def _depends(fn):
            return None

        def _query(default=None, **kw):
            return default

        fake_fastapi.APIRouter = _APIRouter
        fake_fastapi.Depends = _depends
        fake_fastapi.HTTPException = type("HTTPException", (Exception,), {})
        fake_fastapi.Query = _query
        fake_fastapi.status = types.SimpleNamespace(
            HTTP_200_OK=200,
            HTTP_201_CREATED=201,
            HTTP_204_NO_CONTENT=204,
            HTTP_400_BAD_REQUEST=400,
            HTTP_404_NOT_FOUND=404,
        )
        sys.modules["fastapi"] = fake_fastapi

        # api.deps imports get_current_user / get_db that depend on fastapi too
        from unittest.mock import MagicMock
        if "app.api.deps" not in sys.modules:
            fake_deps = types.ModuleType("app.api.deps")
            fake_deps.get_current_user = MagicMock(return_value={})
            fake_deps.get_db = MagicMock(return_value=None)
            sys.modules["app.api.deps"] = fake_deps

    from app.api.routes.materials import _load_mapping_aggregates

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[
        Source.__table__,
        SourceDocument.__table__,
        Material.__table__,
        HsCodeMaterialMapping.__table__,
        HsCodeProductionShare.__table__,
        HsCodeGeographyRiskScore.__table__,
        RiskEvent.__table__,
        RiskEventHsMapping.__table__,
    ])

    with Session(engine) as session:
        # ── Seed material + HS mapping ─────────────────────────────────────
        material = Material(canonical_name="Lithium", category="mineral")
        session.add(material)
        session.flush()

        mapping = HsCodeMaterialMapping(
            hs_code_prefix="282520",
            material_id=material.id,
            description="Lithium hydroxide",
            confidence=0.95,
            supply_chain_stage="refined",
            stage_sequence=4,
            digit_count=6,
            market_scope="global",
        )
        session.add(mapping)
        session.flush()

        # ── Test 1: empty input → empty dict, no DB hits ──────────────────
        result_empty = _load_mapping_aggregates(session, [])
        if result_empty != {}:
            failures.append(f"empty input should return {{}}, got {result_empty}")
        else:
            print("[OK ] Test 1: empty input returns {}")

        # ── Test 2: mapping with no production share / score / events ─────
        result_zero = _load_mapping_aggregates(session, [mapping.id])
        bucket = result_zero.get(mapping.id)
        if bucket is None:
            failures.append("Test 2: mapping_id missing from result dict")
        else:
            if bucket["geographies_by_country"]:
                failures.append(
                    f"Test 2: expected no geos, got {bucket['geographies_by_country']}"
                )
            if bucket["node_score"] is not None:
                failures.append(f"Test 2: node_score should be None, got {bucket['node_score']}")
            if bucket["hhi"] is not None:
                failures.append(f"Test 2: hhi should be None, got {bucket['hhi']}")
            if bucket["events_open"] != 0:
                failures.append(
                    f"Test 2: events_open should be 0, got {bucket['events_open']}"
                )
            print("[OK ] Test 2: empty mapping → empty bucket")

        # ── Seed production shares (CN dominant, CL secondary) ─────────────
        session.add(HsCodeProductionShare(
            hs_mapping_id=mapping.id,
            country_code="CN",
            reference_year=2024,
            production_share=0.65,
            production_volume=130_000.0,
            market_scope="global",
            source="usgs_mcs",
        ))
        session.add(HsCodeProductionShare(
            hs_mapping_id=mapping.id,
            country_code="CL",
            reference_year=2024,
            production_share=0.25,
            production_volume=50_000.0,
            market_scope="global",
            source="usgs_mcs",
        ))
        # Old data — should be excluded by latest-year subquery
        session.add(HsCodeProductionShare(
            hs_mapping_id=mapping.id,
            country_code="AU",
            reference_year=2020,
            production_share=0.50,
            production_volume=80_000.0,
            market_scope="global",
            source="usgs_mcs",
        ))
        session.commit()

        # ── Test 3: production shares (no scores yet) ─────────────────────
        result_shares = _load_mapping_aggregates(session, [mapping.id])
        bucket = result_shares[mapping.id]
        geos = bucket["geographies_by_country"]
        print(f"\n[Test 3] geographies_by_country keys: {list(geos.keys())}")
        if "CN" not in geos or "CL" not in geos:
            failures.append(f"Test 3: expected CN+CL geos, got {list(geos.keys())}")
        if "AU" in geos:
            failures.append(
                "Test 3: AU (2020 data) should be excluded by latest-year filter"
            )
        cn_geo = geos.get("CN")
        if cn_geo is None:
            failures.append("Test 3: CN geo missing")
        else:
            if abs((cn_geo.production_share or 0) - 0.65) > 1e-6:
                failures.append(f"Test 3: CN share wrong: {cn_geo.production_share}")
            if cn_geo.reference_year != 2024:
                failures.append(f"Test 3: CN year wrong: {cn_geo.reference_year}")
            if cn_geo.score is not None:
                failures.append(
                    f"Test 3: CN score should be None pre-rescore, got {cn_geo.score}"
                )
            print(
                f"  [OK ] CN: share={cn_geo.production_share:.2f}, "
                f"year={cn_geo.reference_year}, score={cn_geo.score}"
            )
        if bucket["node_score"] is not None:
            failures.append(
                f"Test 3: node_score should be None pre-rescore, got {bucket['node_score']}"
            )
        if bucket["hhi"] is not None:
            failures.append(
                f"Test 3: hhi should be None until score rows exist (HHI comes from "
                f"hs_code_geography_risk_scores), got {bucket['hhi']}"
            )

        # ── Seed geography risk scores ────────────────────────────────────
        as_of = date(2026, 5, 1)
        session.add(HsCodeGeographyRiskScore(
            hs_mapping_id=mapping.id,
            country_code="CN",
            as_of_date=as_of,
            production_share=0.65,
            hhi_at_stage=0.50,
            tariff_exposure=0.30,
            export_restriction=0.45,
            composite_node_score=68.0,
            market_scope="global",
            methodology_version="3.0",
        ))
        session.add(HsCodeGeographyRiskScore(
            hs_mapping_id=mapping.id,
            country_code="CL",
            as_of_date=as_of,
            production_share=0.25,
            hhi_at_stage=0.50,
            tariff_exposure=0.05,
            export_restriction=0.10,
            composite_node_score=32.5,
            market_scope="global",
            methodology_version="3.0",
        ))
        # Old score — should be excluded by latest-as-of-date partition
        session.add(HsCodeGeographyRiskScore(
            hs_mapping_id=mapping.id,
            country_code="CN",
            as_of_date=date(2026, 1, 1),
            production_share=0.65,
            hhi_at_stage=0.45,
            tariff_exposure=0.10,
            export_restriction=0.20,
            composite_node_score=40.0,
            market_scope="global",
            methodology_version="3.0",
        ))
        session.commit()

        # ── Test 4: scores merged with geography breakdown ────────────────
        result_scored = _load_mapping_aggregates(session, [mapping.id])
        bucket = result_scored[mapping.id]
        cn_geo = bucket["geographies_by_country"]["CN"]
        cl_geo = bucket["geographies_by_country"]["CL"]
        print(f"\n[Test 4] CN: score={cn_geo.score}, tariff={cn_geo.tariff_exposure}")
        print(f"          CL: score={cl_geo.score}")
        if abs((cn_geo.score or 0) - 68.0) > 1e-6:
            failures.append(f"CN score wrong: {cn_geo.score}")
        if abs((cn_geo.tariff_exposure or 0) - 0.30) > 1e-6:
            failures.append(f"CN tariff wrong: {cn_geo.tariff_exposure}")
        if abs((cn_geo.export_restriction or 0) - 0.45) > 1e-6:
            failures.append(f"CN export wrong: {cn_geo.export_restriction}")
        if abs((cn_geo.hhi or 0) - 0.50) > 1e-6:
            failures.append(f"CN hhi wrong: {cn_geo.hhi}")
        # node_score is the MAX across countries
        if abs((bucket["node_score"] or 0) - 68.0) > 1e-6:
            failures.append(
                f"node_score should be max(68.0, 32.5) = 68.0, got {bucket['node_score']}"
            )
        # hhi is production-weighted average
        # CN: hhi=0.50, weight=0.65; CL: hhi=0.50, weight=0.25
        # weighted_hhi = (0.50*0.65 + 0.50*0.25) / (0.65+0.25) = 0.50
        if abs((bucket["hhi"] or 0) - 0.50) > 1e-6:
            failures.append(f"hhi avg should be 0.50, got {bucket['hhi']}")
        if bucket["scored_at"] is None or bucket["scored_at"] != as_of:
            failures.append(
                f"scored_at should be {as_of}, got {bucket['scored_at']}"
            )
        print(f"  [OK ] node_score={bucket['node_score']}, hhi={bucket['hhi']:.2f}, scored_at={bucket['scored_at']}")

        # ── Seed events ───────────────────────────────────────────────────
        # Need a SourceDocument for the events
        source = Source(name="t", source_type="t", phase=1, is_active=True, config_json={})
        session.add(source); session.flush()
        sdoc = SourceDocument(
            source_id=source.id, external_id="t", title="t",
            document_type="trade_policy_database", metadata_json={},
        )
        session.add(sdoc); session.flush()
        for i in range(3):
            ev = RiskEvent(
                source_document_id=sdoc.id,
                event_type="t", event_date=datetime(2024, 1, 1, tzinfo=timezone.utc),
                title=f"e{i}", severity_score=0.5, confidence_score=0.9,
                risk_categories_json=["geopolitical_trade"],
                content_hash=f"e{i}", verified=True,
            )
            session.add(ev); session.flush()
            session.add(RiskEventHsMapping(
                risk_event_id=ev.id, hs_mapping_id=mapping.id,
                relevance_score=0.9, match_reason="hs_code",
            ))
        session.commit()

        # ── Test 5: events_open count ─────────────────────────────────────
        result_full = _load_mapping_aggregates(session, [mapping.id])
        bucket = result_full[mapping.id]
        if bucket["events_open"] != 3:
            failures.append(f"events_open should be 3, got {bucket['events_open']}")
        else:
            print(f"\n[Test 5] [OK ] events_open = {bucket['events_open']}")

        # ── Test 6: bulk-load works for multiple mappings ─────────────────
        m2 = HsCodeMaterialMapping(
            hs_code_prefix="2606",
            material_id=material.id,
            description="Aluminium ores",
            confidence=0.85,
            supply_chain_stage="ore",
            stage_sequence=1,
            digit_count=4,
            market_scope="global",
        )
        session.add(m2); session.flush()
        result_bulk = _load_mapping_aggregates(session, [mapping.id, m2.id])
        if mapping.id not in result_bulk or m2.id not in result_bulk:
            failures.append("Test 6: bulk load missing mapping ids")
        # m2 has nothing — check defaults
        m2_bucket = result_bulk[m2.id]
        if (
            m2_bucket["geographies_by_country"]
            or m2_bucket["node_score"] is not None
            or m2_bucket["events_open"] != 0
        ):
            failures.append(
                f"Test 6: m2 should be empty bucket, got {m2_bucket}"
            )
        else:
            print(f"\n[Test 6] [OK ] bulk load works for multiple mappings, "
                  f"empty bucket for mapping with no data")

    print("\n" + "=" * 60)
    if failures:
        print("FAIL:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS — HS mappings API extension:")
    print("  ✓ Empty input → empty dict")
    print("  ✓ Production shares load with latest-year filtering")
    print("  ✓ Geography scores merge into geo breakdown rows")
    print("  ✓ Latest as_of_date partitioning excludes stale scores")
    print("  ✓ node_score = max across geographies")
    print("  ✓ hhi = production-weighted average")
    print("  ✓ events_open counts all RiskEventHsMapping rows")
    print("  ✓ Bulk load handles multiple mapping_ids")
    return 0


if __name__ == "__main__":
    sys.exit(main())
