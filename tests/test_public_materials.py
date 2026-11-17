"""Public material browse + profile tests (content-site Piece B, 2026-10-24).

Contract:
  * /materials lists only RATED materials (insufficient-data gate matches
    risk-summary), band + derived slug, score-desc order
  * /materials/{slug}: 404 for unknown AND for unrated materials
  * stages come from the scorer's own selection (fresh stages only) with
    is_driving marked, sources + reference year carried, shares as raw
    fractions
  * regulations = material-scoped registry rows only (archived and
    SUGGESTED_ excluded); linked_event_count counts direct, feed-eligible
    events only
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.deps import get_db
from app.api.routes.intelligence_entities import router as entities_router
from app.db.base import Base
from app.models.country import Country
from app.models.regulatory import (
    Regulation,
    RegulationMaterialScope,
    RiskEvent,
    RiskEventMaterial,
)
from app.models.scoring import MaterialGeographyRiskScore, MaterialGlobalRiskScore
from app.models.supply import HsCodeMaterialMapping, HsCodeProductionShare, Material


@pytest.fixture(autouse=True)
def _clear_stage_cache():
    """The profile stage cache is keyed (material_id, as_of) — both repeat
    across tests' fresh in-memory DBs, so a stale hit would serve one
    test's stages to another. Clear around every test."""
    import app.api.routes.intelligence_entities as ent

    ent._STAGE_CACHE.clear()
    yield
    ent._STAGE_CACHE.clear()


@pytest.fixture()
def session_factory():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, autocommit=False, autoflush=False, class_=Session)
    engine.dispose()


@pytest.fixture()
def db(session_factory) -> Iterator[Session]:
    s = session_factory()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture()
def client(session_factory) -> Iterator[TestClient]:
    app = FastAPI()
    app.include_router(entities_router, prefix="/api/v1")

    def _db() -> Iterator[Session]:
        s = session_factory()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _db
    yield TestClient(app)


AS_OF = date(2026, 10, 1)


def _seed(db: Session) -> None:
    ree = Material(canonical_name="Rare Earth Elements", category="metal")
    zinc = Material(canonical_name="Zinc", category="metal")
    sodium = Material(canonical_name="Sodium", category="metal")  # unrated
    db.add_all([ree, zinc, sodium])
    db.add_all([
        Country(iso2="CN", name="China", is_major_producer=True, is_major_consumer=False),
        Country(iso2="AU", name="Australia", is_major_producer=True, is_major_consumer=False),
    ])
    db.flush()

    # Latest-rollup selection: an older row must NOT win.
    db.add_all([
        MaterialGlobalRiskScore(
            material_id=ree.id, as_of_date=date(2026, 7, 1),
            overall_risk_score=50.0, material_concentration_score=50.0,
            scoring_version="1.2", trade_weighted_geo_count=0,
        ),
        MaterialGlobalRiskScore(
            material_id=ree.id, as_of_date=AS_OF,
            overall_risk_score=96.7, material_concentration_score=96.7,
            scoring_version="1.2", trade_weighted_geo_count=0,
        ),
        MaterialGlobalRiskScore(
            material_id=zinc.id, as_of_date=AS_OF,
            overall_risk_score=23.59, material_concentration_score=23.59,
            scoring_version="1.2", trade_weighted_geo_count=0,
        ),
        # Sodium: the stored-zero quirk — must be gated out, not shown Low.
        MaterialGlobalRiskScore(
            material_id=sodium.id, as_of_date=AS_OF,
            overall_risk_score=0.0, material_concentration_score=0.0,
            scoring_version="1.2", trade_weighted_geo_count=0,
        ),
    ])

    # Per-geo score rows: the index's driving_stage comes from the LATEST
    # top-concentration geo's stored diagnostics — the older row and the
    # lower-concentration AU row must both lose.
    db.add_all([
        MaterialGeographyRiskScore(
            material_id=ree.id, geography_code="CN", as_of_date=date(2026, 7, 1),
            material_concentration_score=90.0, scoring_version="1.2",
            rationale_json={"sub_inputs": {"material": {"driving_stage": "ore"}}},
        ),
        MaterialGeographyRiskScore(
            material_id=ree.id, geography_code="CN", as_of_date=AS_OF,
            material_concentration_score=96.7, scoring_version="1.2",
            rationale_json={"sub_inputs": {"material": {"driving_stage": "refined"}}},
        ),
        MaterialGeographyRiskScore(
            material_id=ree.id, geography_code="AU", as_of_date=AS_OF,
            material_concentration_score=20.0, scoring_version="1.2",
            rationale_json={"sub_inputs": {"material": {"driving_stage": "concentrate"}}},
        ),
        # Zinc: geo row with no diagnostics -> driving_stage None, not a crash.
        MaterialGeographyRiskScore(
            material_id=zinc.id, geography_code="AU", as_of_date=AS_OF,
            material_concentration_score=23.59, scoring_version="1.2",
            rationale_json=None,
        ),
    ])

    # Shares: one fresh refined stage for REE (year within 24m of AS_OF).
    mapping = HsCodeMaterialMapping(
        hs_code_prefix="2805", material_id=ree.id, confidence=1.0,
        supply_chain_stage="refined", digit_count=4, market_scope="global",
    )
    db.add(mapping)
    db.flush()
    db.add_all([
        HsCodeProductionShare(
            hs_mapping_id=mapping.id, country_code="CN", production_share=0.9,
            reference_year=2025, market_scope="global", source="iea_gcmo",
        ),
        HsCodeProductionShare(
            hs_mapping_id=mapping.id, country_code="AU", production_share=0.1,
            reference_year=2025, market_scope="global", source="iea_gcmo",
        ),
    ])

    # Regulations: one scoped (shown), one archived (hidden).
    scoped = Regulation(regulation_key="CN_REE_EXPORT_2025", title="CN REE controls",
                        geography="CN", status="effective", pillar="dual")
    archived = Regulation(regulation_key="OLD_REG", title="old", status="archived")
    db.add_all([scoped, archived])
    db.flush()
    db.add(RegulationMaterialScope(regulation_id=scoped.id, material_id=ree.id,
                                   scope_type="restricted"))
    db.add(RegulationMaterialScope(regulation_id=archived.id, material_id=ree.id,
                                   scope_type="covered"))

    # Events: one feed-eligible direct, one pending (not counted).
    for title, status_, ch in (("counted", "scoring", "m1"), ("pending", "pending_triage", "m2")):
        e = RiskEvent(
            event_type="Export ban", title=title, summary="s",
            event_date=datetime(2026, 9, 1, tzinfo=timezone.utc),
            risk_categories_json=["geopolitical_trade"], triage_status=status_,
            content_hash=ch, verified=False,
        )
        db.add(e)
        db.flush()
        db.add(RiskEventMaterial(risk_event_id=e.id, material_id=ree.id,
                                 relevance_score=0.9, match_reason="hs_code",
                                 status="suggested", is_direct=True))
    db.commit()


class TestMaterialsIndex:
    def test_rated_only_with_slugs_and_order(self, client, db):
        _seed(db)
        body = client.get("/api/v1/intelligence/materials").json()
        assert [i["slug"] for i in body] == ["rare-earth-elements", "zinc"]
        assert body[0]["band"]["level"] == "crit"
        assert body[1]["band"]["level"] == "low"
        # Sodium's stored 0.0 is gated, never shown as Low.
        assert all(i["name"] != "Sodium" for i in body)
        # driving_stage: latest run's top-concentration geo diagnostics
        # (not the older run's "ore", not low-share AU's "concentrate");
        # missing diagnostics -> None, never an error.
        assert body[0]["driving_stage"] == "refined"
        assert body[1]["driving_stage"] is None


class TestMaterialProfile:
    def test_profile_payload(self, client, db, monkeypatch):
        # _tagged_posts uses PG's `@>` array containment, which the SQLite
        # test shim cannot compile (same reason the legacy intelligence
        # route tests never ran it). Stub it: the passthrough is what this
        # endpoint owns, the tag query belongs to PG-side testing.
        import app.api.routes.intelligence_entities as ent
        from app.schemas.intelligence_entities import LinkedPostOut
        monkeypatch.setattr(
            ent, "_tagged_posts",
            lambda _db, _tag, limit=6: [LinkedPostOut(
                slug="ree-analysis", title="REE analysis", content_type="analysis",
            )],
        )
        _seed(db)
        body = client.get("/api/v1/intelligence/materials/rare-earth-elements").json()
        assert body["name"] == "Rare Earth Elements"
        assert body["band"]["label"] == "Critical"
        assert body["as_of_date"] == str(AS_OF)
        [stage] = body["stages"]
        assert stage["stage"] == "refined"
        assert stage["reference_year"] == 2025
        assert stage["is_driving"] is True
        assert stage["sources"] == ["iea_gcmo"]
        assert stage["shares"][0] == {"code": "CN", "name": "China", "share": 0.9}
        assert [r["regulation_key"] for r in body["regulations"]] == ["CN_REE_EXPORT_2025"]
        assert body["regulations"][0]["pillar"] == "dual"
        assert [p["slug"] for p in body["linked_posts"]] == ["ree-analysis"]
        assert body["linked_event_count"] == 1

    def test_unknown_and_unrated_404(self, client, db):
        _seed(db)
        assert client.get("/api/v1/intelligence/materials/unobtainium").status_code == 404
        assert client.get("/api/v1/intelligence/materials/sodium").status_code == 404
