"""Public company profile — "market + map" v4 display architecture.

One score basis per section (2026-07-21, after three same-day iterations
on mixed bases):

  * exposures  — GLOBAL rollup per material, ONE deduped row per material
    (stages combined), insufficient-data gated, no fallbacks.
  * geographies — one row per country (facility countries ∪ CME source
    geos); chips = L1 material×country scores at that place.

The regression this architecture exists to prevent (Glencore case): an
exposure column where cobalt meant "risk at CD" but nickel meant "global
fallback" while the page displayed the CA nickel mine the fallback
claimed not to know about.

``_tagged_posts`` (linked_posts) is monkeypatched out: it uses PG's ``@>``
containment, which SQLite cannot parse — the conftest shim covers DDL only.
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


AS_OF = date(2026, 7, 20)


def _seed_glencore_shape(db):
    """The exact scenario that motivated v4: cobalt sourced from CD with
    facilities there; nickel with NO CME source geography but a CA mine;
    plus a gated material (germanium) with no scores at all."""
    company = Company(canonical_name="Glenco", slug="glenco", is_published=True)
    cobalt = Material(canonical_name="Cobalt")
    nickel = Material(canonical_name="Nickel")
    germanium = Material(canonical_name="Germanium")
    db.add_all([company, cobalt, nickel, germanium])
    db.flush()

    db.add_all(
        [
            # Global rollups (exposure bars).
            MaterialGlobalRiskScore(
                material_id=cobalt.id, as_of_date=AS_OF,
                overall_risk_score=64.8, material_concentration_score=90.2,
            ),
            MaterialGlobalRiskScore(
                material_id=nickel.id, as_of_date=AS_OF,
                overall_risk_score=59.9, material_concentration_score=85.0,
            ),
            MaterialGlobalRiskScore(
                material_id=germanium.id, as_of_date=AS_OF,
                overall_risk_score=18.2, material_concentration_score=0.0,  # gated
            ),
            # L1 scores (footprint chips).
            MaterialGeographyRiskScore(
                material_id=cobalt.id, geography_code="CD",
                as_of_date=AS_OF, overall_risk_score=70.0,
            ),
            MaterialGeographyRiskScore(
                material_id=nickel.id, geography_code="CA",
                as_of_date=AS_OF, overall_risk_score=31.5,
            ),
            # CME rows — cobalt at two stages (dedupe check), nickel geo-less.
            CompanyMaterialExposure(
                company_id=company.id, material_id=cobalt.id,
                supply_chain_stage="extraction", exposure_score=0.9,
                source_geography="CD",
            ),
            CompanyMaterialExposure(
                company_id=company.id, material_id=cobalt.id,
                supply_chain_stage="refining", exposure_score=0.7,
                source_geography="CD",
            ),
            CompanyMaterialExposure(
                company_id=company.id, material_id=nickel.id,
                supply_chain_stage="extraction", exposure_score=0.6,
                source_geography=None,
            ),
            CompanyMaterialExposure(
                company_id=company.id, material_id=germanium.id,
                supply_chain_stage="refining", exposure_score=0.2,
                source_geography=None,
            ),
        ]
    )

    fac_cd1 = Facility(facility_type="mine", country="CD", city="Kolwezi", status="operating")
    fac_cd2 = Facility(facility_type="integrated", country="CD", city="Fungurume", status="operating")
    fac_ca = Facility(facility_type="mine", country="CA", city="Sudbury", status="operating")
    db.add_all([fac_cd1, fac_cd2, fac_ca])
    db.flush()
    db.add_all(
        [
            CompanyFacility(company_id=company.id, facility_id=fac_cd1.id),
            CompanyFacility(company_id=company.id, facility_id=fac_cd2.id),
            CompanyFacility(company_id=company.id, facility_id=fac_ca.id),
            FacilityMaterialLink(facility_id=fac_cd1.id, material_id=cobalt.id),
            FacilityMaterialLink(facility_id=fac_cd2.id, material_id=cobalt.id),
            FacilityMaterialLink(facility_id=fac_ca.id, material_id=nickel.id),
        ]
    )
    db.commit()
    return company


class TestExposuresGlobalOnly:
    def test_one_deduped_row_per_material_global_scores(self, client, db):
        _seed_glencore_shape(db)
        r = client.get("/api/v1/intelligence/companies/glenco")
        assert r.status_code == 200, r.text
        expo = r.json()["exposures"]

        # 4 CME rows -> 3 material rows; sorted global DESC, gated last.
        assert [e["material"] for e in expo] == ["Cobalt", "Nickel", "Germanium"]

        cobalt = expo[0]
        assert cobalt["stage_label"] == "Extraction · Refining"  # deduped + combined
        assert cobalt["risk_score"] == 64.8  # GLOBAL, never the CD 70.0
        assert cobalt["band"]["level"] == "crit"
        assert "geography" not in cobalt and "score_basis" not in cobalt

        nickel = expo[1]
        assert nickel["risk_score"] == 59.9  # global — no fallback concept anymore
        assert nickel["band"]["level"] == "high"

        germanium = expo[2]
        assert germanium["risk_score"] is None  # gate
        assert germanium["band"] is None


class TestGeographicFootprint:
    def test_one_row_per_country_with_chips(self, client, db):
        _seed_glencore_shape(db)
        r = client.get("/api/v1/intelligence/companies/glenco")
        assert r.status_code == 200
        body = r.json()
        geos = body["geographies"]
        assert body["facilities_total"] == 3

        # Two countries, riskiest first. The CA nickel mine that the v3
        # design contradicted now has its own row.
        assert [g["country"] for g in geos] == ["CD", "CA"]

        cd = geos[0]
        assert cd["facility_count"] == 2  # grouped, not one row per facility
        assert cd["activities"] == ["integrated", "mine"]
        assert cd["sourcing_materials"] == ["Cobalt"]
        assert cd["location_risk"]["level"] == "crit"
        assert cd["materials"] == [
            {"material": "Cobalt", "score": 70.0, "level": "crit"},
        ]

        ca = geos[1]
        assert ca["facility_count"] == 1
        assert ca["sourcing_materials"] == []  # facility-linked, not CME-sourced
        assert ca["materials"] == [
            {"material": "Nickel", "score": 31.5, "level": "med"},
        ]

    def test_sourcing_only_country_gets_a_row(self, client, db):
        """A CME source geography with no facilities still appears."""
        company = Company(canonical_name="Trader Co", slug="trader", is_published=True)
        lithium = Material(canonical_name="Lithium")
        db.add_all([company, lithium])
        db.flush()
        db.add_all(
            [
                MaterialGlobalRiskScore(
                    material_id=lithium.id, as_of_date=AS_OF,
                    overall_risk_score=54.3, material_concentration_score=83.9,
                ),
                MaterialGeographyRiskScore(
                    material_id=lithium.id, geography_code="CL",
                    as_of_date=AS_OF, overall_risk_score=42.0,
                ),
                CompanyMaterialExposure(
                    company_id=company.id, material_id=lithium.id,
                    supply_chain_stage="extraction", exposure_score=0.8,
                    source_geography="CL",
                ),
            ]
        )
        db.commit()

        r = client.get("/api/v1/intelligence/companies/trader")
        assert r.status_code == 200
        geos = r.json()["geographies"]
        assert len(geos) == 1
        cl = geos[0]
        assert cl["country"] == "CL"
        assert cl["facility_count"] == 0  # sourcing-only
        assert cl["sourcing_materials"] == ["Lithium"]
        assert cl["materials"] == [
            {"material": "Lithium", "score": 42.0, "level": "med"},
        ]
