"""Public event feed tests — GET /intelligence/events (C3, 2026-10-24).

Mounts the intelligence router on a bare FastAPI app (the
test_triage_routes pattern): in-memory SQLite via StaticPool, JSONB/ARRAY
shimmed by tests/conftest.py at collection time.

Contract under test (concentration plan workstream C):
  * scoring + display_only only; pending / rejected / duplicates never
  * ``verified`` == human-confirmed (scoring); verified_only filter
  * material filter + listed materials = DIRECT links only (056 rule)
  * geographies = primary context only; source name + permalink carried
  * NO numeric severity anywhere in the payload (A6 public scope)
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.deps import get_db
from app.api.routes.intelligence import router as intelligence_router
from app.db.base import Base
from app.models.country import Country
from app.models.documents import SourceDocument
from app.models.regulatory import RiskEvent, RiskEventGeography, RiskEventMaterial
from app.models.source import Source
from app.models.supply import Material


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
    app.include_router(intelligence_router, prefix="/api/v1")

    def _db() -> Iterator[Session]:
        s = session_factory()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _db
    yield TestClient(app)


def _seed_corpus(db: Session) -> None:
    src = Source(name="Global Trade Alert", source_type="gta", phase="1", is_active=True)
    db.add(src)
    db.flush()
    doc = SourceDocument(
        source_id=src.id, external_id="gta-1",
        url="https://globaltradealert.org/intervention/99", document_type="x",
    )
    db.add(doc)
    db.add(Country(iso2="ID", name="Indonesia", is_major_producer=True, is_major_consumer=False))
    nickel = Material(canonical_name="Nickel", category="metal")
    cobalt = Material(canonical_name="Cobalt", category="metal")
    db.add_all([nickel, cobalt])
    db.flush()

    def ev(title, status, day, ch, dup_of=None):
        e = RiskEvent(
            source_document_id=doc.id,
            event_type="Export ban",
            event_subtype="EXPORT_BAN",
            event_date=datetime(2026, 10, day, tzinfo=timezone.utc),
            title=title,
            summary=f"{title} summary",
            severity_score=0.9,
            confidence_score=0.9,
            risk_categories_json=["geopolitical_trade"],
            suggested_category="geopolitical_trade",
            direction="restrictive",
            triage_status=status,
            duplicate_of_id=dup_of,
            content_hash=ch,
            verified=(status == "scoring"),
        )
        db.add(e)
        db.flush()
        return e

    confirmed = ev("confirmed ban", "scoring", 20, "c1")
    context = ev("context measure", "display_only", 18, "c2")
    ev("still pending", "pending_triage", 19, "c3")
    ev("dismissed", "rejected", 17, "c4")
    ev("dup of confirmed", "scoring", 16, "c5", dup_of=confirmed.id)

    # confirmed: direct Nickel + broad Cobalt; context: direct Cobalt.
    db.add(RiskEventMaterial(risk_event_id=confirmed.id, material_id=nickel.id,
                             relevance_score=0.9, match_reason="hs_code",
                             status="confirmed", is_direct=True))
    db.add(RiskEventMaterial(risk_event_id=confirmed.id, material_id=cobalt.id,
                             relevance_score=0.3, match_reason="hs_code",
                             status="suggested", is_direct=False))
    db.add(RiskEventMaterial(risk_event_id=context.id, material_id=cobalt.id,
                             relevance_score=0.9, match_reason="named_material",
                             status="suggested", is_direct=True))
    db.add(RiskEventGeography(risk_event_id=confirmed.id, country_code="ID",
                              geography_context="primary", relevance_score=1.0))
    db.add(RiskEventGeography(risk_event_id=confirmed.id, country_code="CN",
                              geography_context="affected", relevance_score=1.0))
    db.commit()


class TestPublicFeed:
    def test_scope_and_order(self, client, db):
        _seed_corpus(db)
        body = client.get("/api/v1/intelligence/events").json()
        titles = [i["title"] for i in body["data"]]
        # pending / rejected / duplicate structurally absent; newest first.
        assert titles == ["confirmed ban", "context measure"]
        assert body["total"] == 2

    def test_verified_flag_and_filter(self, client, db):
        _seed_corpus(db)
        body = client.get("/api/v1/intelligence/events").json()
        flags = {i["title"]: i["verified"] for i in body["data"]}
        assert flags == {"confirmed ban": True, "context measure": False}
        only = client.get("/api/v1/intelligence/events", params={"verified_only": True}).json()
        assert [i["title"] for i in only["data"]] == ["confirmed ban"]

    def test_material_filter_is_direct_only(self, client, db):
        _seed_corpus(db)
        nickel = client.get("/api/v1/intelligence/events", params={"material": "Nickel"}).json()
        assert [i["title"] for i in nickel["data"]] == ["confirmed ban"]
        # Cobalt's only tie to "confirmed ban" is a BROAD link — excluded;
        # the direct Cobalt link on the context measure matches.
        cobalt = client.get("/api/v1/intelligence/events", params={"material": "Cobalt"}).json()
        assert [i["title"] for i in cobalt["data"]] == ["context measure"]

    def test_material_slug_filter(self, client, db):
        """material_slug resolves to the same direct-only filter as
        material (2026-11-12: lets the profile page fetch signals in
        parallel). Unknown slug -> empty page, not 404; `material` wins
        when both are sent."""
        _seed_corpus(db)
        nickel = client.get(
            "/api/v1/intelligence/events", params={"material_slug": "nickel"}
        ).json()
        assert [i["title"] for i in nickel["data"]] == ["confirmed ban"]
        cobalt = client.get(
            "/api/v1/intelligence/events", params={"material_slug": "cobalt"}
        ).json()
        assert [i["title"] for i in cobalt["data"]] == ["context measure"]
        unknown = client.get(
            "/api/v1/intelligence/events", params={"material_slug": "unobtainium"}
        ).json()
        assert unknown["data"] == [] and unknown["total"] == 0
        both = client.get(
            "/api/v1/intelligence/events",
            params={"material": "Nickel", "material_slug": "cobalt"},
        ).json()
        assert [i["title"] for i in both["data"]] == ["confirmed ban"]

    def test_item_payload_shape(self, client, db):
        _seed_corpus(db)
        item = client.get("/api/v1/intelligence/events").json()["data"][0]
        assert item["source_name"] == "Global Trade Alert"
        assert item["source_url"] == "https://globaltradealert.org/intervention/99"
        assert item["materials"] == ["Nickel"]          # direct only
        assert item["geographies"] == [{"code": "ID", "name": "Indonesia"}]  # primary only
        assert item["direction"] == "restrictive"
        assert item["event_date"] == "2026-10-20"
        # Public scope: numeric severity must not leak.
        assert "severity_score" not in item and "severity" not in item

    def test_pagination(self, client, db):
        _seed_corpus(db)
        p1 = client.get("/api/v1/intelligence/events", params={"limit": 1, "page": 1}).json()
        p2 = client.get("/api/v1/intelligence/events", params={"limit": 1, "page": 2}).json()
        assert p1["total"] == p2["total"] == 2
        assert p1["data"][0]["title"] == "confirmed ban"
        assert p2["data"][0]["title"] == "context measure"
