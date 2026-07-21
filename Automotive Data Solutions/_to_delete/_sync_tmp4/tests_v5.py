"""Public company profile — exposure + facility scoring blocks.

Exposures, v3 semantics (2026-07-21, third iteration that day — v1
at-source was reverted as unlabeled, v2 global-only was superseded once
section captions declared the basis): each row shows the L1 score at its
OWN source geography; multi-geo sourcing = multiple rows, no aggregation.
Rows without a geography fall back to the material's gated L2 global
score (score_basis distinguishes). Unscored rows sort last.

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
from app.models.facility import CompanyFacility, Facility, FacilityMaterialLink
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
    def test_at_source_score_beats_global(self, client, db):
        """v3: a row WITH a source geography shows the L1 at-source score,
        not the global rollup (both exist here; L1 must win)."""
        company = Company(canonical_name="Acme Cells", slug="acme-cells", is_published=True)
        lithium = Material(canonical_name="Lithium")
        db.add_all([company, lithium])
        db.flush()
        as_of = date(2026, 7, 20)
        db.add_all(
            [
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
        assert row["score_basis"] == "geography"
        assert row["risk_score"] == 42.0  # at-source CL, NOT global 54.3
        assert row["band"] == {"label": "Moderate", "level": "med", "score": 42.0}

    def test_global_fallback_gate_and_sort(self, client, db):
        company = Company(canonical_name="Beta", slug="beta", is_published=True)
        graphite = Material(canonical_name="Natural Graphite")
        iron = Material(canonical_name="Iron Ore")
        germanium = Material(canonical_name="Germanium")
        db.add_all([company, graphite, iron, germanium])
        db.flush()
        as_of = date(2026, 7, 20)
        db.add_all(
            [
                # Graphite row has a geo + L1 -> at-source basis.
                MaterialGeographyRiskScore(
                    material_id=graphite.id,
                    geography_code="CN",
                    as_of_date=as_of,
                    overall_risk_score=74.1,
                ),
                # Iron Ore row has NO geo -> global fallback.
                MaterialGlobalRiskScore(
                    material_id=iron.id,
                    as_of_date=as_of,
                    overall_risk_score=22.6,
                    material_concentration_score=34.7,
                ),
                # Germanium: no geo AND concentration-gated -> unscored.
                MaterialGlobalRiskScore(
                    material_id=germanium.id,
                    as_of_date=as_of,
                    overall_risk_score=18.2,
                    material_concentration_score=0.0,
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
                    material_id=iron.id,
                    supply_chain_stage="refining",
                    exposure_score=0.5,
                    source_geography=None,
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

        assert [e["material"] for e in expo] == [
            "Natural Graphite", "Iron Ore", "Germanium",
        ]
        graphite_row = expo[0]
        assert graphite_row["score_basis"] == "geography"
        assert graphite_row["risk_score"] == 74.1
        assert graphite_row["band"]["level"] == "crit"

        iron_row = expo[1]
        assert iron_row["score_basis"] == "global"
        assert iron_row["risk_score"] == 22.6
        assert iron_row["band"]["level"] == "low"

        germanium_row = expo[2]
        assert germanium_row["score_basis"] is None
        assert germanium_row["risk_score"] is None
        assert germanium_row["band"] is None


class TestFacilityLocationRisk:
    """2026-07-21: facilities carry at-location risk — max L1 material×
    facility-country score (headline + driver) plus per-material chips,
    sorted riskiest location first, unscored last."""

    def test_headline_chips_and_sort(self, client, db):
        company = Company(canonical_name="Gamma Mining", slug="gamma", is_published=True)
        cobalt = Material(canonical_name="Cobalt")
        copper = Material(canonical_name="Copper")
        lithium = Material(canonical_name="Lithium")
        db.add_all([company, cobalt, copper, lithium])
        db.flush()

        as_of = date(2026, 7, 20)
        db.add_all(
            [
                MaterialGeographyRiskScore(
                    material_id=cobalt.id, geography_code="CD",
                    as_of_date=as_of, overall_risk_score=70.0,
                ),
                MaterialGeographyRiskScore(
                    material_id=copper.id, geography_code="CD",
                    as_of_date=as_of, overall_risk_score=55.4,
                ),
                MaterialGeographyRiskScore(
                    material_id=lithium.id, geography_code="CL",
                    as_of_date=as_of, overall_risk_score=42.0,
                ),
            ]
        )

        fac_cd = Facility(facility_type="integrated", country="CD",
                          city="Kolwezi", status="operating")
        fac_cl = Facility(facility_type="mine", country="CL",
                          city="Salar de Atacama", status="operating")
        fac_de = Facility(facility_type="cell_plant", country="DE",
                          city="Berlin", status="construction")
        db.add_all([fac_cd, fac_cl, fac_de])
        db.flush()
        db.add_all(
            [
                CompanyFacility(company_id=company.id, facility_id=fac_cd.id),
                CompanyFacility(company_id=company.id, facility_id=fac_cl.id),
                CompanyFacility(company_id=company.id, facility_id=fac_de.id),
                FacilityMaterialLink(facility_id=fac_cd.id, material_id=cobalt.id),
                FacilityMaterialLink(facility_id=fac_cd.id, material_id=copper.id),
                FacilityMaterialLink(facility_id=fac_cl.id, material_id=lithium.id),
                # DE plant links lithium, but no L1 row for (Li, DE) -> unscored.
                FacilityMaterialLink(facility_id=fac_de.id, material_id=lithium.id),
            ]
        )
        db.commit()

        r = client.get("/api/v1/intelligence/companies/gamma")
        assert r.status_code == 200, r.text
        facs = r.json()["facilities"]
        assert len(facs) == 3

        # Sorted by headline at-location risk; unscored (DE) last.
        assert [f["country"] for f in facs] == ["CD", "CL", "DE"]

        cd = facs[0]
        assert cd["location_risk"] == {"label": "Critical", "level": "crit", "score": 70.0}
        assert cd["location_risk_material"] == "Cobalt"
        # Chips: every scored linked material, score DESC.
        assert cd["material_scores"] == [
            {"material": "Cobalt", "score": 70.0, "level": "crit"},
            {"material": "Copper", "score": 55.4, "level": "high"},
        ]

        cl = facs[1]
        assert cl["location_risk"]["level"] == "med"  # 42.0 -> MOD
        assert cl["location_risk_material"] == "Lithium"
        assert len(cl["material_scores"]) == 1

        de = facs[2]
        assert de["location_risk"] is None
        assert de["location_risk_material"] is None
        assert de["material_scores"] == []
