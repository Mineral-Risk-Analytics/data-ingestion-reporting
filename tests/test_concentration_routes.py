"""Supply Concentration transparency API tests (2026-08-05).

Mounts the concentration router on a bare FastAPI app, in-memory SQLite via
StaticPool, per the test_triage_routes pattern.  The fixtures deliberately
reproduce the four audit states the Workstream A instrument must tell apart:
fully fresh, stale-but-not-binding, understated-by-staleness, and no stage
data — plus the amplifier and prior-vintage ore HHI.
"""

from __future__ import annotations

from datetime import date
from typing import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.deps import get_current_user, get_db
from app.api.routes.concentration import router as concentration_router
from app.db.base import Base
from app.models import CountryGovernanceSignal
from app.models.supply import (
    HsCodeMaterialMapping,
    HsCodeProductionShare,
    Material,
    MaterialProductionShare,
)


def _patch_sqlite_jsonb() -> None:
    from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler  # type: ignore[import]

    if not hasattr(SQLiteTypeCompiler, "visit_JSONB"):
        SQLiteTypeCompiler.visit_JSONB = SQLiteTypeCompiler.visit_JSON  # type: ignore[attr-defined]


@pytest.fixture()
def engine():
    _patch_sqlite_jsonb()
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture()
def session_factory(engine):
    return sessionmaker(bind=engine, autocommit=False, autoflush=False, class_=Session)


@pytest.fixture()
def db(session_factory) -> Iterator[Session]:
    s = session_factory()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture()
def client(session_factory) -> Iterator[TestClient]:
    test_app = FastAPI()
    test_app.include_router(concentration_router, prefix="/api/v1")

    def _db() -> Iterator[Session]:
        s = session_factory()
        try:
            yield s
        finally:
            s.close()

    test_app.dependency_overrides[get_db] = _db
    test_app.dependency_overrides[get_current_user] = lambda: {"email": "nicole@test"}
    yield TestClient(test_app)


AS_OF = "2026-07-12"  # pinned so freshness verdicts cannot rot with real time


def _material(db, name, symbol=None) -> Material:
    m = Material(canonical_name=name, symbol_or_code=symbol, category="metal")
    db.add(m)
    db.flush()
    return m


def _stage_rows(db, material, stage, hs_prefix, shares, year, source="benchmark", digit_count=4):
    mapping = HsCodeMaterialMapping(
        hs_code_prefix=hs_prefix, material_id=material.id,
        digit_count=digit_count, market_scope="global", supply_chain_stage=stage,
    )
    db.add(mapping)
    db.flush()
    for cc, share in shares.items():
        db.add(HsCodeProductionShare(
            hs_mapping_id=mapping.id, country_code=cc, production_share=share,
            reference_year=year, market_scope="global", source=source,
        ))
    db.flush()
    return mapping


def _producers(db, material, shares, year=2025):
    for cc, share in shares.items():
        db.add(MaterialProductionShare(
            material_id=material.id, country_code=cc, reference_year=year,
            production_share=share, production_volume=share * 1000,
            unit_of_measure="metric tons", data_source="usgs_mcs",
        ))
    db.flush()


class TestDetail:
    def test_fresh_material_binding_and_amplifier(self, client, db):
        """Fresh two-stage material: binding stage, raw vs amplified score, WGI."""
        m = _material(db, "Natural Graphite", "C")
        _stage_rows(db, m, "ore", "2504", {"CN": 0.65, "MZ": 0.09}, 2025, source="usgs_mcs")
        _stage_rows(db, m, "battery_grade", "3801", {"CN": 0.92, "KR": 0.04}, 2025)
        _producers(db, m, {"CN": 0.65, "MZ": 0.09})
        db.add(CountryGovernanceSignal(
            country_code="CN", reference_year=2024, composite_pct=40.0,
            n_dimensions_present=6,
        ))
        db.commit()

        r = client.get(f"/api/v1/concentration/materials/{m.id}", params={"as_of": AS_OF})
        assert r.status_code == 200
        body = r.json()

        assert body["freshness_years"] == 2
        assert [s["stage"] for s in body["stages"]] == ["ore", "battery_grade"]
        assert all(s["fresh"] for s in body["stages"])
        cn = body["per_geo"]["CN"]
        # battery_grade is far more concentrated — it must bind for CN.
        assert cn["binding_stage"] == "battery_grade"
        # Amplified: WGI 40 → instability 0.6; score must exceed the raw max.
        assert cn["amplified"] is True
        assert cn["score"] > cn["raw_score"] > 0
        assert cn["wgi_pct"] == 40.0
        assert body["driving_geo"] == "CN"
        assert body["headline_understated"] is False
        # MZ has no WGI row → no-op, served as null, score == raw.
        mz = body["per_geo"]["MZ"]
        assert mz["wgi_pct"] is None
        assert mz["score"] == mz["raw_score"]

    def test_understatement_detected_for_driving_geo(self, client, db):
        """The canonical A2 case: a stale stage that would out-score the fresh
        max must flag the material as understated, with the exact numbers."""
        m = _material(db, "Cobalt", "Co")
        _stage_rows(db, m, "ore", "2605", {"CD": 0.71}, 2025, source="usgs_mcs")
        # CN's only fresh presence is a modest refined share...
        _stage_rows(db, m, "refined", "8105", {"CN": 0.40, "FI": 0.07}, 2025)
        # ...but its battery-grade snapshot @2022 (stale at a 2026 as-of) is 85%.
        _stage_rows(db, m, "battery_grade", "2822", {"CN": 0.85, "KR": 0.08}, 2022)
        _producers(db, m, {"CD": 0.71})
        db.commit()

        body = client.get(
            f"/api/v1/concentration/materials/{m.id}", params={"as_of": AS_OF}
        ).json()

        stale = [s for s in body["stages"] if not s["fresh"]]
        assert [s["stage"] for s in stale] == ["battery_grade"]
        assert stale[0]["age_years"] == 4

        cn = body["per_geo"]["CN"]
        assert cn["understated"] is True
        assert cn["stale_best"]["stage"] == "battery_grade"
        assert cn["stale_best"]["score"] > cn["raw_score"]
        # KR appears ONLY at the stale stage: scores 0 today, understated too.
        kr = body["per_geo"]["KR"]
        assert kr["raw_score"] == 0
        assert kr["binding_stage"] is None
        assert kr["understated"] is True
        # Whether the HEADLINE is understated depends on the driving geo.
        driving = body["driving_geo"]
        assert body["headline_understated"] == body["per_geo"][driving]["understated"]

    def test_stale_stage_that_does_not_bind_is_not_headline(self, client, db):
        """Stale stage weaker than the fresh max: reported, but no headline flag."""
        m = _material(db, "Lithium", "Li")
        _stage_rows(db, m, "refined", "2825", {"CN": 0.67}, 2025)
        _stage_rows(db, m, "battery_grade", "2836", {"CN": 0.30}, 2022)
        _producers(db, m, {"AU": 0.32, "CL": 0.28})
        db.commit()

        body = client.get(
            f"/api/v1/concentration/materials/{m.id}", params={"as_of": AS_OF}
        ).json()
        cn = body["per_geo"]["CN"]
        assert cn["stale_best"] is not None
        assert cn["understated"] is False
        assert body["headline_understated"] is False

    def test_prior_ore_hhi_served_for_yoy(self, client, db):
        m = _material(db, "Nickel", "Ni")
        mp = _stage_rows(db, m, "ore", "2604", {"ID": 0.49, "PH": 0.11}, 2025, source="usgs_mcs")
        # Prior vintage on the same mapping.
        for cc, share in {"ID": 0.42, "PH": 0.12}.items():
            db.add(HsCodeProductionShare(
                hs_mapping_id=mp.id, country_code=cc, production_share=share,
                reference_year=2024, market_scope="global", source="usgs_mcs",
            ))
        _producers(db, m, {"ID": 0.49})
        db.commit()

        body = client.get(
            f"/api/v1/concentration/materials/{m.id}", params={"as_of": AS_OF}
        ).json()
        assert body["prior_ore_hhi"]["reference_year"] == 2024
        # 0.42² + 0.12² = 0.1908
        assert abs(body["prior_ore_hhi"]["hhi_raw"] - 0.1908) < 1e-6
        # And the current ore HHI uses ONLY the 2025 vintage.
        ore = next(s for s in body["stages"] if s["stage"] == "ore")
        assert abs(ore["hhi_raw"] - (0.49**2 + 0.11**2)) < 1e-6

    def test_404_for_unknown_material(self, client):
        assert client.get("/api/v1/concentration/materials/999").status_code == 404


class TestOverview:
    def test_audit_tally_counts_launch_list_only(self, client, db):
        # Launch-list, fully fresh.
        ni = _material(db, "Nickel", "Ni")
        _stage_rows(db, ni, "ore", "2604", {"ID": 0.49}, 2025, source="usgs_mcs")
        _producers(db, ni, {"ID": 0.49})
        # Launch-list, understated (stale stage would win).
        co = _material(db, "Cobalt", "Co")
        _stage_rows(db, co, "refined", "8105", {"CN": 0.40}, 2025)
        _stage_rows(db, co, "battery_grade", "2822", {"CN": 0.85}, 2022)
        _producers(db, co, {"CD": 0.71})
        # Launch-list, production rows but NO stage mapping.
        ree = _material(db, "Rare Earth Elements", "REE")
        _producers(db, ree, {"CN": 0.69})
        # NON-launch-list material with stage data — must not enter the tally.
        ag = _material(db, "Silver", "Ag")
        _stage_rows(db, ag, "ore", "2616", {"MX": 0.24}, 2025, source="usgs_mcs")
        _producers(db, ag, {"MX": 0.24})
        # Registry row with nothing at all → uncovered list, not an item.
        _material(db, "Separator Polymer", "PP")
        db.commit()

        body = client.get("/api/v1/concentration/overview", params={"as_of": AS_OF}).json()

        assert body["audit"] == {
            "fully_fresh": 1, "with_stale": 1, "understated": 1, "no_stage": 1,
        }
        states = {i["name"]: i["audit_state"] for i in body["items"]}
        assert states["Nickel"] == "fresh"
        assert states["Cobalt"] == "understated"
        assert states["Rare Earth Elements"] == "nostage"
        assert states["Silver"] == "fresh"  # listed, just not tallied
        assert [u["name"] for u in body["uncovered"]] == ["Separator Polymer"]
        assert body["as_of"] == AS_OF

    def test_overview_row_shape(self, client, db):
        m = _material(db, "Nickel", "Ni")
        _stage_rows(db, m, "ore", "2604", {"ID": 0.49, "PH": 0.11}, 2025, source="usgs_mcs")
        _stage_rows(db, m, "refined", "7502", {"CN": 0.43, "ID": 0.32}, 2025)
        _producers(db, m, {"ID": 0.49, "PH": 0.11})
        db.add(CountryGovernanceSignal(
            country_code="ID", reference_year=2024, composite_pct=47.0,
            n_dimensions_present=6,
        ))
        db.commit()

        body = client.get("/api/v1/concentration/overview", params={"as_of": AS_OF}).json()
        assert body["wgi_vintage"] == 2024
        row = next(i for i in body["items"] if i["name"] == "Nickel")
        assert row["is_launch_list"] is True
        assert row["driving_geo"] in ("ID", "CN")
        assert row["binding_stage"] in ("ore", "refined")
        assert row["ore"]["fresh"] is True
        assert row["top_production"][0]["country_code"] == "ID"
        assert body["country_names"] == {}  # no Country rows seeded — absent, not defaulted


class TestQueryBudget:
    def test_overview_query_count_is_constant(self, engine, client, db):
        """The overview must issue a FIXED number of queries regardless of how
        many materials exist.  The first version looped single-material
        loaders (~5 queries × N materials ≈ 200+ round trips against remote
        Postgres ≈ 11-second page loads).  This pins the ceiling so an
        innocent-looking per-material helper can't reintroduce the N+1."""
        from sqlalchemy import event as sa_event

        for i in range(25):
            m = _material(db, f"QB Material {i:02d}")
            _stage_rows(db, m, "ore", f"{2700 + i}", {"CN": 0.5, "AU": 0.2}, 2025,
                        source="usgs_mcs")
            _producers(db, m, {"CN": 0.5, "AU": 0.2})
        db.commit()

        count = {"n": 0}

        def _count(conn, cursor, statement, parameters, context, executemany):
            if statement.lstrip().upper().startswith("SELECT"):
                count["n"] += 1

        sa_event.listen(engine, "before_cursor_execute", _count)
        try:
            body = client.get(
                "/api/v1/concentration/overview", params={"as_of": AS_OF}
            ).json()
        finally:
            sa_event.remove(engine, "before_cursor_execute", _count)

        assert len(body["items"]) == 25
        assert count["n"] <= 10, (
            f"overview issued {count['n']} SELECTs for 25 materials — "
            "per-material querying is back"
        )
