"""Triage API tests (2026-08-02).

Mounts the triage router on a bare FastAPI app (rather than importing
``app.main``) so the tests exercise exactly this surface without coupling to
the full router registry.  In-memory SQLite via StaticPool, per the
test_intelligence_routes pattern.
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

from app.api.deps import get_current_user, get_db
from app.api.routes.triage import router as triage_router
from app.db.base import Base
from app.models.documents import SourceDocument
from app.models.regulatory import RiskEvent, RiskEventMaterial
from app.models.source import Source
from app.models.supply import Material


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
    test_app.include_router(triage_router, prefix="/api/v1")

    def _db() -> Iterator[Session]:
        s = session_factory()
        try:
            yield s
        finally:
            s.close()

    test_app.dependency_overrides[get_db] = _db
    test_app.dependency_overrides[get_current_user] = lambda: {"email": "nicole@test"}
    yield TestClient(test_app)


def _seed(db: Session, **event_kwargs) -> RiskEvent:
    src = db.query(Source).filter_by(name="Global Trade Alert").first()
    if src is None:
        src = Source(name="Global Trade Alert", source_type="gta", phase="1", is_active=True)
        db.add(src)
        db.flush()
        doc = SourceDocument(
            source_id=src.id, external_id="doc-1",
            url="https://globaltradealert.org/intervention/1", document_type="x",
        )
        db.add(doc)
        db.flush()
        db.info["doc_id"] = doc.id
    kwargs = dict(
        source_document_id=db.info.get("doc_id"),
        event_type="Export ban",
        event_date=datetime(2026, 1, 10, tzinfo=timezone.utc),
        title="Test export ban",
        summary="A ban.",
        severity_score=0.9,
        risk_categories_json=["geopolitical_trade"],
        suggested_category="geopolitical_trade",
        direction="restrictive",
        triage_status="pending_triage",
        verified=False,
    )
    kwargs.update(event_kwargs)
    ev = RiskEvent(**kwargs)
    db.add(ev)
    db.flush()
    mat = db.query(Material).filter_by(canonical_name="Nickel").first()
    if mat is None:
        mat = Material(canonical_name="Nickel", category="metal")
        db.add(mat)
        db.flush()
    db.add(RiskEventMaterial(
        risk_event_id=ev.id, material_id=mat.id, relevance_score=0.9,
        match_reason="hs_code", status="suggested",
    ))
    db.commit()
    return ev


class TestReads:
    def test_queue_default_hides_rejected(self, client, db):
        _seed(db)
        _seed(db, title="dismissed one", triage_status="rejected", content_hash="x2")
        r = client.get("/api/v1/triage/events")
        assert r.status_code == 200
        titles = [i["title"] for i in r.json()["items"]]
        assert "dismissed one" not in titles

    def test_row_shape_and_defect_facet(self, client, db):
        _seed(db, summary="x" * 500, metadata_json={"needs_material_review": True})
        r = client.get("/api/v1/triage/events", params={"defect": "needs_material_review"})
        assert r.status_code == 200
        rows = r.json()["items"]
        assert len(rows) == 1
        row = rows[0]
        assert "truncated_summary" in row["quality_defects"]
        assert "needs_material_review" in row["quality_defects"]
        assert row["suggested_category"] == "geopolitical_trade"
        assert row["direction"] == "restrictive"
        assert row["links"][0]["status"] == "suggested"
        assert row["links"][0]["match_reason"] == "hs_code"
        assert row["source_url"].startswith("https://globaltradealert.org")

    def test_direction_filter(self, client, db):
        _seed(db)
        _seed(db, title="a grant", direction="supportive",
              suggested_category="financial_pressure",
              triage_status="display_only", content_hash="x3")
        r = client.get("/api/v1/triage/events",
                       params={"direction": "supportive", "status": "display_only"})
        assert [i["title"] for i in r.json()["items"]] == ["a grant"]
        # DB-true supportive shape: suggestion present even on display_only.
        assert r.json()["items"][0]["suggested_category"] == "financial_pressure"


class TestStatusTransitions:
    def test_accept_confirms_suggestions(self, client, db):
        ev = _seed(db)
        r = client.post(f"/api/v1/triage/events/{ev.id}/accept")
        assert r.status_code == 200
        body = r.json()
        assert body["triage_status"] == "scoring"
        assert body["primary_category"] == "geopolitical_trade"
        assert body["verified"] is True
        assert body["triaged_by"] == "nicole@test"
        assert all(l["status"] == "confirmed" for l in body["links"])

    def test_unapprove_reverses_everything(self, client, db):
        ev = _seed(db)
        client.post(f"/api/v1/triage/events/{ev.id}/accept")
        r = client.patch(f"/api/v1/triage/events/{ev.id}/status",
                         json={"status": "pending_triage"})
        body = r.json()
        assert body["triage_status"] == "pending_triage"
        assert body["primary_category"] is None
        assert body["verified"] is False
        assert body["triaged_by"] is None

    def test_scoring_without_pillar_400(self, client, db):
        # risk_categories_json emptied too — otherwise the suggestion
        # listener (correctly) derives a suggested_category on insert.
        ev = _seed(db, suggested_category=None, risk_categories_json=[],
                   content_hash="x4")
        r = client.patch(f"/api/v1/triage/events/{ev.id}/status",
                         json={"status": "scoring"})
        assert r.status_code == 400

    def test_positive_direction_refused(self, client, db):
        ev = _seed(db, event_subtype="POSITIVE_POLICY", content_hash="x5")
        r = client.patch(f"/api/v1/triage/events/{ev.id}/status",
                         json={"status": "scoring"})
        assert r.status_code == 400
        assert "excluded from risk arithmetic" in r.json()["detail"]

    def test_news_candidate_refused_plain_approve(self, client, db):
        ev = _seed(db, event_type="operational_news_candidate",
                   severity_score=None, suggested_category="operational",
                   content_hash="x6")
        r = client.patch(f"/api/v1/triage/events/{ev.id}/status",
                         json={"status": "scoring"})
        assert r.status_code == 400
        assert "promote-operational" in r.json()["detail"]

    def test_promote_operational_with_ladder(self, client, db):
        ev = _seed(db, event_type="operational_news_candidate",
                   severity_score=None, suggested_category="operational",
                   metadata_json={"scoring": "display_only",
                                  "display_only_reason": "pending_triage",
                                  "suggested_subtype": "labor_strike"},
                   content_hash="x7")
        r = client.post(f"/api/v1/triage/events/{ev.id}/promote-operational",
                        json={"subtype": "labor_strike"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["triage_status"] == "scoring"
        assert body["primary_category"] == "operational"
        assert body["severity_score"] == 0.55  # taxonomy default
        assert body["verified"] is True


class TestLinks:
    def test_confirm_and_reject_link(self, client, db):
        ev = _seed(db)
        link_id = ev.material_links[0].id
        r = client.patch(f"/api/v1/triage/links/{link_id}", json={"status": "confirmed"})
        assert r.json()["status"] == "confirmed"
        r = client.patch(f"/api/v1/triage/links/{link_id}", json={"status": "suggested"})
        assert r.json()["status"] == "suggested"

    def test_add_analyst_link(self, client, db):
        ev = _seed(db)
        m = Material(canonical_name="Lithium", category="metal")
        db.add(m); db.commit()
        r = client.post(f"/api/v1/triage/events/{ev.id}/links",
                        json={"material_id": m.id})
        assert r.status_code == 200
        assert r.json()["status"] == "confirmed"
        assert r.json()["match_reason"] == "analyst"


class TestSummaryAndFlags:
    def test_summary_counts(self, client, db):
        _seed(db)
        _seed(db, triage_status="display_only", content_hash="x8")
        r = client.get("/api/v1/triage/summary")
        body = r.json()
        assert body["pending_triage"] == 1
        assert body["display_only"] == 1

    def test_flag_event(self, client, db):
        ev = _seed(db)
        r = client.post(f"/api/v1/triage/events/{ev.id}/flags",
                        json={"note_text": "date looks wrong"})
        assert r.status_code == 200
        assert r.json()["flags_count"] == 1


class TestOperationalSubtypes:
    def test_ladder_served_without_positives(self, client):
        r = client.get("/api/v1/triage/operational-subtypes")
        assert r.status_code == 200
        rows = r.json()
        values = [x["value"] for x in rows]
        assert "labor_strike" in values
        strike = next(x for x in rows if x["value"] == "labor_strike")
        assert strike["default_severity"] == 0.55
        from app.constants import POSITIVE_EVENT_SUBTYPES
        assert not (set(values) & set(POSITIVE_EVENT_SUBTYPES))
