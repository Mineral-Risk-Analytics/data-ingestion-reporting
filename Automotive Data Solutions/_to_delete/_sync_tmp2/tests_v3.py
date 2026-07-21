"""Public company profile — exposure scoring block.

2026-07-21 v2 semantics: every exposure row displays the material's
latest L2 GLOBAL rollup score — the same number as /risk-summary — so a
material never shows two different bands across public surfaces. The
source geography is a descriptive tag only (a brief per-geography L1
variant earlier the same day was reverted for exactly that mismatch).
Insufficient-data gate matches the sidebar (concentration NULL/0 ->
no score, no band). Unscored rows sort last.

Same in-memory SQLite harness as test_intelligence_routes.py.

``_tagged_posts`` (linked_posts) is monkeypatched out: it uses PG's ``@>``
containment, which SQLite cannot parse — the conftest shim covers DDL
only. Linked-post behaviour is orthogonal to the exposure block.
"""

from __future__ import annotations

from datetime import date
from typing import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401  — register all ORM models
import app.api.routes.intelligence_entities as entities_routes
from app.api.deps import get_db
from app.db.base import Base
from app.main import app
from app.models.company import Company, CompanyMaterialExposure
from app.models.scoring import MaterialGeographyRiskScore, MaterialGlobalRiskScore
from app.models.supply import Material


@pytest.fixture()
def engine():
    engine = create_engine(
        "sqlite:///:memory:",
        echo=False,
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
def client(session_factory) -> Iterator[TestClient]:
    def _override_get_db() -> Iterator[Session]:
        s = session_factory()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _override_get_db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.fixture(autouse=True)
def _no_tagged_posts(monkeypatch):
    """linked_posts uses PG-only ``@>`` — stub it out under SQLite."""
    monkeypatch.setattr(entities_routes, "_tagged_posts", lambda *a, **kw: [])


@pytest.fixture()
def db(session_factory) -> Iterator[Session]:
    s = session_factory()
    try:
        yield s
    finally:
        s.close()


class TestExposureScores:
    def test_rows_use_global_score_not_l1_geo_score(self, client, db):
        """The sidebar-consistency contract: even when an L1 row exists for
        the exposure's exact (material, source_geography), the row must
        show the GLOBAL rollup — one number per material, everywhere."""
        company = Company(canonical_name="Acme Cells", slug="acme-cells", is_published=True)
        lithium = Material(canonical_name="Lithium")
        db.add_all([company, lithium])
        db.flush()
        as_of = date(2026, 7, 20)
        db.add_all(
            [
                # Tempting L1 row (CL 42.0 would band "med") — must be ignored.
                MaterialGeographyRiskScore(
                    material_id=lithium.id,
                    geography_code="CL",
                    as_of_date=as_of,
                    overall_risk_score=42.0,
                ),
                MaterialGlobalRiskScore(
                    material_id=lithium.id,
                    as_of_date=as_of,
                    overall_risk_score=54.3,
                    material_concentration_score=83.9,
                ),
                CompanyMaterialExposure(
                    company_id=company.id,
                    material_id=lithium.id,
                    supply_chain_stage="extraction",
                    exposure_score=0.7,
                    source_geography="CL",
                ),
            ]
        )
        db.commit()

        r = client.get("/api/v1/intelligence/companies/acme-cells")
        assert r.status_code == 200, r.text
        expo = r.json()["exposures"]
        assert len(expo) == 1
        row = expo[0]
        assert row["geography"] == "CL"  # sourcing tag preserved
        assert row["risk_score"] == 54.3  # global, NOT the CL 42.0
        assert row["band"] == {"label": "High", "level": "high", "score": 54.3}

    def test_gate_sort_and_latest_row(self, client, db):
        company = Company(canonical_name="Beta", slug="beta", is_published=True)
        graphite = Material(canonical_name="Natural Graphite")
        iron = Material(canonical_name="Iron Ore")
        germanium = Material(canonical_name="Germanium")
        db.add_all([company, graphite, iron, germanium])
        db.flush()
        db.add_all(
            [
                # Graphite: stale row must lose to the newer one.
                MaterialGlobalRiskScore(
                    material_id=graphite.id,
                    as_of_date=date(2025, 10, 1),
                    overall_risk_score=20.0,
                    material_concentration_score=40.0,
                ),
                MaterialGlobalRiskScore(
                    material_id=graphite.id,
                    as_of_date=date(2026, 7, 20),
                    overall_risk_score=64.9,
                    material_concentration_score=99.7,
                ),
                MaterialGlobalRiskScore(
                    material_id=iron.id,
                    as_of_date=date(2026, 7, 20),
                    overall_risk_score=22.6,
                    material_concentration_score=34.7,
                ),
                # Germanium: concentration-gated -> unscored.
                MaterialGlobalRiskScore(
                    material_id=germanium.id,
                    as_of_date=date(2026, 7, 20),
                    overall_risk_score=18.2,
                    material_concentration_score=0.0,
                ),
                CompanyMaterialExposure(
                    company_id=company.id,
                    material_id=iron.id,
                    supply_chain_stage="refining",
                    exposure_score=0.5,
                    source_geography=None,
                ),
                CompanyMaterialExposure(
                    company_id=company.id,
                    material_id=graphite.id,
                    supply_chain_stage="cell",
                    exposure_score=0.8,
                    source_geography="CN",
                ),
                CompanyMaterialExposure(
                    company_id=company.id,
                    material_id=germanium.id,
                    supply_chain_stage="refining",
                    exposure_score=0.3,
                    source_geography=None,
                ),
            ]
        )
        db.commit()

        r = client.get("/api/v1/intelligence/companies/beta")
        assert r.status_code == 200
        expo = r.json()["exposures"]

        # Highest global score first; gated material last with no band.
        assert [e["material"] for e in expo] == [
            "Natural Graphite", "Iron Ore", "Germanium",
        ]
        assert expo[0]["risk_score"] == 64.9  # latest row, not the stale 20.0
        assert expo[0]["band"]["level"] == "crit"
        assert expo[1]["band"]["level"] == "low"
        assert expo[2]["risk_score"] is None
        assert expo[2]["band"] is None
