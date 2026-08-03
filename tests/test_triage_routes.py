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
from app.models.country import Country
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
        # Shared with operational_triage.py's hand-attach path — see
        # tests/ingestion/test_operational_triage.py, which asserts the same
        # code. The UI's provenance hint map keys off it.
        assert r.json()["match_reason"] == "triage_attach"
        assert r.json()["kind"] == "material"
        assert r.json()["is_direct"] is True


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


class TestDerivedRowFields:
    """Fields added 2026-08-02 so the drawer stops inferring them client-side.

    ``is_positive`` in particular: the frontend previously carried its own
    copy of POSITIVE_EVENT_SUBTYPES, which had drifted (it listed
    ``restart``/``expansion``/``guidance_raise``, none of which are in the
    backend set, and omitted POSITIVE_DEVELOPMENT).
    """

    def test_pillars_geography_and_positive_flag(self, client, db):
        _seed(
            db,
            risk_categories_json=["geopolitical_trade", "material_concentration"],
            geography_json={"primary": "CN", "secondary": ["RU", "CN"]},
        )
        row = client.get("/api/v1/triage/events").json()["items"][0]
        assert row["risk_categories"] == [
            "geopolitical_trade",
            "material_concentration",
        ]
        # Primary first, duplicates dropped in order.
        assert row["geography_codes"] == ["CN", "RU"]
        assert row["is_positive"] is False
        assert row["links"][0]["kind"] == "material"

    def test_geography_names_resolve_and_omit_unknown_codes(self, client, db):
        """Names come from ``countries``, not from the browser's Intl tables.

        The codes in geography_json are whatever ``countries.iso2`` holds, and
        that column admits non-ISO entries (bloc identifiers, territory rows).
        A code with no matching row must be absent from the map rather than
        defaulted to itself, so the UI can tell "unknown to the reference
        table" apart from "name happens to equal the code" and fall back to
        rendering the bare code.
        """
        db.add(Country(iso2="CN", name="China"))
        db.add(Country(iso2="EU", name="European Union"))
        db.commit()
        _seed(db, geography_json={"primary": "CN", "secondary": ["EU", "ZZ"]})

        row = client.get("/api/v1/triage/events").json()["items"][0]
        assert row["geography_codes"] == ["CN", "EU", "ZZ"]
        assert row["geography_names"] == {"CN": "China", "EU": "European Union"}

    def test_geography_names_served_on_write_responses_too(self, client, db):
        """Every write endpoint returns the mutated event through the same
        builder, so a drawer left open after a status change keeps its names.
        """
        db.add(Country(iso2="CN", name="China"))
        db.commit()
        ev = _seed(db, geography_json={"primary": "CN"})

        r = client.patch(
            f"/api/v1/triage/events/{ev.id}/status",
            json={"status": "display_only"},
        )
        assert r.status_code == 200
        assert r.json()["geography_names"] == {"CN": "China"}

    def test_positive_subtype_marked(self, client, db):
        from app.constants import POSITIVE_EVENT_SUBTYPES

        subtype = sorted(POSITIVE_EVENT_SUBTYPES)[0]
        _seed(db, event_subtype=subtype)
        row = client.get("/api/v1/triage/events").json()["items"][0]
        assert row["is_positive"] is True

    def test_pillars_tolerate_dict_shape(self, client, db):
        _seed(db, risk_categories_json={"operational": 0.4})
        row = client.get("/api/v1/triage/events").json()["items"][0]
        assert row["risk_categories"] == ["operational"]

    def test_flags_are_readable_not_just_countable(self, client, db):
        ev = _seed(db)
        client.post(
            f"/api/v1/triage/events/{ev.id}/flags",
            json={"note_text": "date looks wrong", "note_type": "date"},
        )
        row = client.get(f"/api/v1/triage/events/{ev.id}").json()
        assert row["flags_count"] == 1
        assert len(row["flags"]) == 1
        note = row["flags"][0]
        assert note["note_text"] == "date looks wrong"
        assert note["note_type"] == "date"
        assert note["author"] == "nicole@test"
        assert note["created_at"]


class TestListFacetFilters:
    def test_flagged_only(self, client, db):
        ev = _seed(db)
        _seed(db, title="clean one", content_hash="c2")
        client.post(
            f"/api/v1/triage/events/{ev.id}/flags", json={"note_text": "bad date"}
        )
        body = client.get("/api/v1/triage/events", params={"flagged": True}).json()
        assert body["total"] == 1
        assert body["items"][0]["id"] == ev.id

    def test_has_defects_only(self, client, db):
        _seed(db, title="defective", summary="x" * 500, content_hash="d1")
        _seed(db, title="clean", content_hash="d2")
        body = client.get(
            "/api/v1/triage/events", params={"has_defects": True}
        ).json()
        titles = [i["title"] for i in body["items"]]
        assert "defective" in titles
        assert "clean" not in titles

    def test_sort_dir_reverses_order(self, client, db):
        _seed(db, title="low", severity_score=0.1, content_hash="s1")
        _seed(db, title="high", severity_score=0.9, content_hash="s2")
        desc = client.get(
            "/api/v1/triage/events", params={"sort": "severity_score"}
        ).json()["items"]
        asc = client.get(
            "/api/v1/triage/events",
            params={"sort": "severity_score", "sort_dir": "asc"},
        ).json()["items"]
        assert desc[0]["title"] == "high"
        assert asc[0]["title"] == "low"


class TestCoverage:
    """GET /triage/coverage — the material coverage tracker's data.

    The counting rules are the tracker's whole value, so each exclusion is
    pinned individually: a row that slips through any of them silently
    inflates "coverage" with signal scoring will never see.
    """

    @staticmethod
    def _recent() -> datetime:
        from datetime import timedelta

        return datetime.now(timezone.utc) - timedelta(days=10)

    def _link(self, db, ev, **updates):
        link = db.query(RiskEventMaterial).filter_by(risk_event_id=ev.id).one()
        for k, v in updates.items():
            setattr(link, k, v)
        db.commit()
        return link

    def test_split_and_exclusions(self, client, db):
        from datetime import timedelta

        now = datetime.now(timezone.utc)
        # Counted as confirmed.
        e1 = _seed(db, event_date=self._recent(), content_hash="c1")
        self._link(db, e1, status="confirmed")
        # Counted as pending (suggested is the _seed default).
        _seed(db, event_date=self._recent(), content_hash="c2")
        # Excluded: outside the 90-day window (the _seed default date is old,
        # but pin it explicitly so this test does not rot as time passes).
        _seed(db, event_date=now - timedelta(days=120), content_hash="c3")
        # Excluded: no event date at all — windowing is unanswerable.
        _seed(db, event_date=None, content_hash="c4")
        # Excluded: rejected event; its links are not "pending", an analyst
        # already said no.
        _seed(db, event_date=self._recent(), triage_status="rejected", content_hash="c5")
        # Excluded: confirmed duplicate (055).
        e6 = _seed(db, event_date=self._recent(), content_hash="c6")
        e6.duplicate_of_id = e1.id
        db.commit()
        # Excluded: inherited link (056), even though confirmed.
        e7 = _seed(db, event_date=self._recent(), content_hash="c7")
        self._link(db, e7, status="confirmed", is_direct=False)
        # Excluded from both buckets: rejected link on a live event.
        e8 = _seed(db, event_date=self._recent(), content_hash="c8")
        self._link(db, e8, status="rejected")

        body = client.get("/api/v1/triage/coverage").json()
        assert body["window_days"] == 90
        assert body["target"] == 5
        nickel = next(i for i in body["items"] if i["name"] == "Nickel")
        assert nickel["confirmed"] == 1
        assert nickel["pending"] == 1
        assert nickel["is_launch_list"] is True

    def test_future_dated_events_count(self, client, db):
        from datetime import timedelta

        # Future-dated is a data defect flagged elsewhere, but it is signal —
        # the mockup pins this too ("inside the window by definition").
        ev = _seed(
            db,
            event_date=datetime.now(timezone.utc) + timedelta(days=30),
            content_hash="f1",
        )
        self._link(db, ev, status="confirmed")
        nickel = next(
            i
            for i in client.get("/api/v1/triage/coverage").json()["items"]
            if i["name"] == "Nickel"
        )
        assert nickel["confirmed"] == 1

    def test_zero_signal_materials_are_served(self, client, db):
        """"No signal at all" is the tracker's most important state; it cannot
        be rendered from a payload that omits silent materials."""
        db.add(Material(canonical_name="Scandium", category="metal"))
        db.commit()
        _seed(db, event_date=self._recent(), content_hash="z1")

        items = client.get("/api/v1/triage/coverage").json()["items"]
        scandium = next(i for i in items if i["name"] == "Scandium")
        assert scandium == {
            "material_id": scandium["material_id"],
            "name": "Scandium",
            "is_launch_list": False,
            "confirmed": 0,
            "pending": 0,
        }
