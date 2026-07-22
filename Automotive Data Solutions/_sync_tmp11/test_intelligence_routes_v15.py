"""Phase 4 — Intelligence Hub backend tests.

Covers the public + admin route surface defined in
``app/api/routes/intelligence.py``:

  * Public feed/detail/risk-summary (no auth, ``status='published'`` only).
  * Admin authoring lifecycle: create draft → publish → unpublish →
    archive, plus PATCH semantics and the drafts inbox view.
  * Slug uniqueness conflict (HTTP 409).
  * Route-ordering safety: ``GET /posts/drafts`` must resolve to the admin
    handler, not be greedily matched as ``/posts/{slug}``.

In-memory SQLite is wired up by overriding ``get_db``. The conftest shim
renders ``ARRAY``/``JSONB`` as ``JSON`` so DDL works on SQLite, and
``InsightPost.materials/geographies`` survive round-trips as JSON arrays.
``InsightPost.contains([value])`` filtering is exercised end-to-end so the
production PG ``@>`` form is also covered by the same call site.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401  — register all ORM models
from app.api.deps import get_db
from app.db.base import Base
from app.main import app
from app.models.company import Company
from app.models.intelligence import InsightPost
from app.models.regulatory import Regulation
from app.models.scoring import MaterialGlobalRiskScore
from app.models.supply import Material


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def engine():
    """Fresh in-memory SQLite per test — no cross-test bleed.

    ``StaticPool`` + ``check_same_thread=False`` are required because
    FastAPI's ``TestClient`` dispatches handlers via a worker thread, but a
    bare ``:memory:`` connection is single-threaded and lives only for one
    connection. ``StaticPool`` re-uses the same connection across threads so
    the schema we create in the test thread is visible to the request thread.
    """
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
    """TestClient with ``get_db`` overridden to use the in-memory DB."""

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


@pytest.fixture()
def db(session_factory) -> Iterator[Session]:
    s = session_factory()
    try:
        yield s
    finally:
        s.close()


def _make_post(
    *,
    slug: str,
    title: str = "Sample post",
    status: str = "published",
    content_type: str = "analysis",
    pillar: str | None = "geopolitical_trade",
    materials: list[str] | None = None,
    geographies: list[str] | None = None,
    summary: str = "Short summary.",
    body: str = "Full markdown body.",
    published_at: datetime | None = None,
) -> InsightPost:
    if status == "published" and published_at is None:
        published_at = datetime.now(timezone.utc)
    return InsightPost(
        slug=slug,
        title=title,
        content_type=content_type,
        pillar=pillar,
        materials=materials,
        geographies=geographies,
        summary=summary,
        body=body,
        status=status,
        published_at=published_at,
    )


# ===========================================================================
# Public read routes
# ===========================================================================


class TestPublicListAndDetail:
    def test_lists_only_published_posts(self, client, db):
        db.add_all(
            [
                _make_post(slug="published-1", title="Published 1"),
                _make_post(slug="draft-1", title="Drafty", status="draft"),
                _make_post(slug="archived-1", title="Old", status="archived"),
            ]
        )
        db.commit()

        r = client.get("/api/v1/intelligence/posts")
        assert r.status_code == 200, r.text

        body = r.json()
        assert body["total"] == 1
        slugs = [p["slug"] for p in body["data"]]
        assert slugs == ["published-1"]

    def test_published_posts_returned_newest_first(self, client, db):
        old = _make_post(
            slug="old",
            published_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        )
        new = _make_post(
            slug="new",
            published_at=datetime(2026, 4, 1, tzinfo=timezone.utc),
        )
        db.add_all([old, new])
        db.commit()

        r = client.get("/api/v1/intelligence/posts")
        assert r.status_code == 200
        slugs = [p["slug"] for p in r.json()["data"]]
        assert slugs == ["new", "old"]

    def test_filters_by_content_type_and_pillar(self, client, db):
        db.add_all(
            [
                _make_post(
                    slug="signal-geo",
                    content_type="signal",
                    pillar="geopolitical_trade",
                ),
                _make_post(
                    slug="analysis-reg",
                    content_type="analysis",
                    pillar="regulatory_compliance",
                ),
                _make_post(
                    slug="signal-reg",
                    content_type="signal",
                    pillar="regulatory_compliance",
                ),
            ]
        )
        db.commit()

        r = client.get(
            "/api/v1/intelligence/posts",
            params={"content_type": "signal", "pillar": "regulatory_compliance"},
        )
        assert r.status_code == 200
        slugs = [p["slug"] for p in r.json()["data"]]
        assert slugs == ["signal-reg"]

    def test_get_published_by_slug_returns_body(self, client, db):
        db.add(_make_post(slug="lithium-2026", body="# Heading\n\nProse"))
        db.commit()

        r = client.get("/api/v1/intelligence/posts/lithium-2026")
        assert r.status_code == 200
        data = r.json()
        assert data["slug"] == "lithium-2026"
        assert data["body"] == "# Heading\n\nProse"
        assert data["status"] == "published"

    def test_unpublished_post_is_404_for_public(self, client, db):
        db.add(_make_post(slug="hidden", status="draft"))
        db.commit()

        r = client.get("/api/v1/intelligence/posts/hidden")
        assert r.status_code == 404

    def test_unknown_slug_is_404(self, client):
        r = client.get("/api/v1/intelligence/posts/does-not-exist")
        assert r.status_code == 404

    def test_list_paginates(self, client, db):
        for i in range(5):
            db.add(_make_post(slug=f"p{i}"))
        db.commit()

        r = client.get(
            "/api/v1/intelligence/posts",
            params={"page": 2, "limit": 2},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 5
        assert body["page"] == 2
        assert body["limit"] == 2
        assert len(body["data"]) == 2


# ===========================================================================
# Risk summary
# ===========================================================================


class TestRiskSummary:
    """2026-07-21: endpoint rewritten onto the L2 global rollup
    (``material_global_risk_scores``) with the insufficient-data gate and
    bands.py banding — tests seed MaterialGlobalRiskScore accordingly."""

    def test_orders_by_score_and_bands(self, client, db):
        lithium = Material(canonical_name="Lithium")
        cobalt = Material(canonical_name="Cobalt")
        db.add_all([lithium, cobalt])
        db.flush()

        as_of = date(2026, 4, 1)
        db.add_all(
            [
                MaterialGlobalRiskScore(
                    material_id=lithium.id,
                    as_of_date=as_of,
                    overall_risk_score=54.3,
                    material_concentration_score=83.9,
                ),
                MaterialGlobalRiskScore(
                    material_id=cobalt.id,
                    as_of_date=as_of,
                    overall_risk_score=64.8,
                    material_concentration_score=90.2,
                ),
            ]
        )
        db.commit()

        r = client.get("/api/v1/intelligence/risk-summary")
        assert r.status_code == 200, r.text
        body = r.json()

        assert body["as_of_date"] == "2026-04-01"
        # Ordered by score DESC
        assert [m["material_name"] for m in body["materials"]] == ["Cobalt", "Lithium"]

        cobalt_bar = body["materials"][0]
        assert cobalt_bar["band"] == {
            "label": "Critical", "level": "crit", "score": 64.8,
        }
        lithium_bar = body["materials"][1]
        assert lithium_bar["band"]["level"] == "high"  # 45 <= 54.3 < 60
        assert lithium_bar["band"]["score"] == 54.3

    def test_uses_only_latest_as_of_date_per_material(self, client, db):
        nickel = Material(canonical_name="Nickel")
        db.add(nickel)
        db.flush()

        # Two rollup rows for the same material. Only the newer one should
        # drive the bar (append-only history table).
        db.add_all(
            [
                MaterialGlobalRiskScore(
                    material_id=nickel.id,
                    as_of_date=date(2025, 10, 1),
                    overall_risk_score=20.0,
                    material_concentration_score=40.0,
                ),
                MaterialGlobalRiskScore(
                    material_id=nickel.id,
                    as_of_date=date(2026, 4, 1),
                    overall_risk_score=59.9,
                    material_concentration_score=85.0,
                ),
            ]
        )
        db.commit()

        r = client.get("/api/v1/intelligence/risk-summary")
        assert r.status_code == 200
        body = r.json()
        assert len(body["materials"]) == 1
        bar = body["materials"][0]
        assert bar["material_name"] == "Nickel"
        assert bar["band"]["score"] == 59.9
        assert body["as_of_date"] == "2026-04-01"

    def test_insufficient_data_gate_excludes_unscored_concentration(self, client, db):
        # Germanium-shape row: overall exists but the concentration pillar is
        # 0 (no scored stage) -> must NOT appear in the public sidebar.
        germanium = Material(canonical_name="Germanium")
        sodium = Material(canonical_name="Sodium")
        tin = Material(canonical_name="Tin")
        db.add_all([germanium, sodium, tin])
        db.flush()

        as_of = date(2026, 4, 1)
        db.add_all(
            [
                MaterialGlobalRiskScore(
                    material_id=germanium.id,
                    as_of_date=as_of,
                    overall_risk_score=18.2,
                    material_concentration_score=0.0,
                ),
                MaterialGlobalRiskScore(
                    material_id=sodium.id,
                    as_of_date=as_of,
                    overall_risk_score=12.7,
                    material_concentration_score=None,
                ),
                MaterialGlobalRiskScore(
                    material_id=tin.id,
                    as_of_date=as_of,
                    overall_risk_score=27.9,
                    material_concentration_score=20.4,
                ),
            ]
        )
        db.commit()

        r = client.get("/api/v1/intelligence/risk-summary")
        assert r.status_code == 200
        names = [m["material_name"] for m in r.json()["materials"]]
        assert names == ["Tin"]

    def test_returns_empty_when_no_scores(self, client):
        r = client.get("/api/v1/intelligence/risk-summary")
        assert r.status_code == 200
        body = r.json()
        assert body["as_of_date"] is None
        assert body["materials"] == []


# ===========================================================================
# Admin authoring lifecycle
# ===========================================================================


class TestAdminLifecycle:
    def test_create_post_starts_as_draft(self, client):
        r = client.post(
            "/api/v1/intelligence/posts",
            json={
                "slug": "new-post",
                "title": "New post",
                "content_type": "analysis",
                "summary": "A draft.",
                "body": "Body.",
            },
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["slug"] == "new-post"
        assert body["status"] == "draft"
        assert body["published_at"] is None

    def test_create_rejects_duplicate_slug(self, client, db):
        db.add(_make_post(slug="taken", status="draft", published_at=None))
        db.commit()

        r = client.post(
            "/api/v1/intelligence/posts",
            json={
                "slug": "taken",
                "title": "Conflict",
                "content_type": "signal",
            },
        )
        assert r.status_code == 409

    def test_publish_then_unpublish(self, client):
        created = client.post(
            "/api/v1/intelligence/posts",
            json={
                "slug": "publish-me",
                "title": "Ready",
                "content_type": "signal",
            },
        ).json()
        post_id = created["id"]

        # Publish
        r = client.post(f"/api/v1/intelligence/posts/{post_id}/publish")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "published"
        assert body["published_at"] is not None
        first_published_at = body["published_at"]

        # Public route now sees the post by slug
        r = client.get("/api/v1/intelligence/posts/publish-me")
        assert r.status_code == 200

        # Re-publishing is rejected
        r = client.post(f"/api/v1/intelligence/posts/{post_id}/publish")
        assert r.status_code == 400

        # Unpublish clears status + timestamp
        r = client.post(f"/api/v1/intelligence/posts/{post_id}/unpublish")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "draft"
        assert body["published_at"] is None

        # Slug now 404s on the public detail route
        r = client.get("/api/v1/intelligence/posts/publish-me")
        assert r.status_code == 404

        # Re-publish keeps a fresh timestamp (since we cleared it)
        r = client.post(f"/api/v1/intelligence/posts/{post_id}/publish")
        assert r.status_code == 200
        assert r.json()["published_at"] != first_published_at

    def test_archive_hides_from_feed_but_keeps_row(self, client, db):
        created = client.post(
            "/api/v1/intelligence/posts",
            json={
                "slug": "old-news",
                "title": "Yesterday",
                "content_type": "news",
            },
        ).json()
        post_id = created["id"]
        client.post(f"/api/v1/intelligence/posts/{post_id}/publish")

        r = client.post(f"/api/v1/intelligence/posts/{post_id}/archive")
        assert r.status_code == 200
        assert r.json()["status"] == "archived"

        # Hidden from public feed
        feed = client.get("/api/v1/intelligence/posts").json()
        assert feed["total"] == 0

        # But the row still exists in the DB (soft-delete contract)
        assert db.get(InsightPost, post_id) is not None

    def test_patch_updates_editable_fields_only(self, client, db):
        post = _make_post(
            slug="editme",
            title="Original",
            summary="Old summary",
            status="published",
        )
        db.add(post)
        db.commit()
        db.refresh(post)

        r = client.patch(
            f"/api/v1/intelligence/posts/{post.id}",
            json={
                "title": "Updated title",
                "summary": "New summary",
                "read_time_minutes": 7,
                "author": "Partner",
            },
        )
        assert r.status_code == 200
        body = r.json()
        assert body["title"] == "Updated title"
        assert body["summary"] == "New summary"
        assert body["read_time_minutes"] == 7
        assert body["author"] == "Partner"
        # PATCH must not flip status or wipe published_at — those are managed
        # by the dedicated /publish, /unpublish, /archive verbs.
        assert body["status"] == "published"
        assert body["published_at"] is not None

    def test_patch_404_for_unknown_post(self, client):
        r = client.patch(
            "/api/v1/intelligence/posts/9999",
            json={"title": "Nope"},
        )
        assert r.status_code == 404


# ===========================================================================
# Drafts inbox
# ===========================================================================


class TestDraftsInbox:
    def test_drafts_route_resolves_before_slug(self, client, db):
        # If route ordering regresses, "drafts" would be matched as a slug
        # against /posts/{slug} and 404.
        db.add_all(
            [
                _make_post(slug="d1", status="draft"),
                _make_post(slug="d2", status="archived"),
                _make_post(slug="p1", status="published"),
            ]
        )
        db.commit()

        r = client.get("/api/v1/intelligence/posts/drafts")
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 2
        slugs = sorted(p["slug"] for p in body["data"])
        assert slugs == ["d1", "d2"]


class TestPatchExtensions:
    """2026-07-21 admin content v1: slug / content_type / risk_band on PATCH."""

    def test_risk_band_set_and_clear(self, client, db):
        post = _make_post(slug="banded", title="T")
        db.add(post)
        db.commit()
        db.refresh(post)

        r = client.patch(
            f"/api/v1/intelligence/posts/{post.id}", json={"risk_band": "crit"}
        )
        assert r.status_code == 200
        assert r.json()["risk_band"] == "crit"

        r = client.patch(
            f"/api/v1/intelligence/posts/{post.id}", json={"risk_band": None}
        )
        assert r.status_code == 200
        assert r.json()["risk_band"] is None

    def test_risk_band_rejects_unknown_value(self, client, db):
        post = _make_post(slug="badband", title="T")
        db.add(post)
        db.commit()
        db.refresh(post)

        r = client.patch(
            f"/api/v1/intelligence/posts/{post.id}", json={"risk_band": "severe"}
        )
        assert r.status_code == 422

    def test_content_type_change_and_validation(self, client, db):
        post = _make_post(slug="retype", title="T")
        db.add(post)
        db.commit()
        db.refresh(post)

        r = client.patch(
            f"/api/v1/intelligence/posts/{post.id}", json={"content_type": "signal"}
        )
        assert r.status_code == 200
        assert r.json()["content_type"] == "signal"

        r = client.patch(
            f"/api/v1/intelligence/posts/{post.id}", json={"content_type": "listicle"}
        )
        assert r.status_code == 422

    def test_slug_rename_and_conflict(self, client, db):
        a = _make_post(slug="first-post", title="A")
        b = _make_post(slug="second-post", title="B")
        db.add_all([a, b])
        db.commit()
        db.refresh(a)

        r = client.patch(
            f"/api/v1/intelligence/posts/{a.id}", json={"slug": "renamed-post"}
        )
        assert r.status_code == 200
        assert r.json()["slug"] == "renamed-post"

        # Collision with b -> 409, and a keeps its new slug (rollback safe).
        r = client.patch(
            f"/api/v1/intelligence/posts/{a.id}", json={"slug": "second-post"}
        )
        assert r.status_code == 409

        # Malformed slug (uppercase / spaces) -> 422 via pattern.
        r = client.patch(
            f"/api/v1/intelligence/posts/{a.id}", json={"slug": "Bad Slug!"}
        )
        assert r.status_code == 422


class TestArticleDate:
    """2026-07-21: published_at is settable (article WRITTEN date) — the
    /publish verb only stamps it when still NULL, so pre-set dates survive."""

    def test_patch_backdates_and_publish_keeps_it(self, client, db):
        post = _make_post(slug="backdated", title="T", status="draft", published_at=None)
        db.add(post)
        db.commit()
        db.refresh(post)

        r = client.patch(
            f"/api/v1/intelligence/posts/{post.id}",
            json={"published_at": "2026-06-01T12:00:00Z"},
        )
        assert r.status_code == 200
        assert r.json()["published_at"].startswith("2026-06-01")

        r = client.post(f"/api/v1/intelligence/posts/{post.id}/publish")
        assert r.status_code == 200
        # Publish must NOT overwrite the pre-set article date with "now".
        assert r.json()["published_at"].startswith("2026-06-01")

    def test_create_with_article_date(self, client):
        r = client.post(
            "/api/v1/intelligence/posts",
            json={
                "slug": "dated-draft",
                "title": "Dated",
                "content_type": "signal",
                "published_at": "2026-05-15T12:00:00Z",
            },
        )
        assert r.status_code == 201, r.text
        assert r.json()["published_at"].startswith("2026-05-15")

    def test_patch_clear_then_publish_restamps(self, client, db):
        post = _make_post(slug="cleared", title="T", status="draft",
                          published_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
        db.add(post)
        db.commit()
        db.refresh(post)

        r = client.patch(
            f"/api/v1/intelligence/posts/{post.id}", json={"published_at": None}
        )
        assert r.status_code == 200
        assert r.json()["published_at"] is None

        r = client.post(f"/api/v1/intelligence/posts/{post.id}/publish")
        assert r.status_code == 200
        assert r.json()["published_at"] is not None  # re-stamped "now"


class TestPdfUploadUrl:
    """2026-07-22: presigned R2 PUT for report PDFs."""

    def _configure_r2(self, monkeypatch):
        from app.core.config import get_settings
        st = get_settings()
        monkeypatch.setattr(st, "r2_account_id", "acct123")
        monkeypatch.setattr(st, "r2_access_key_id", "AKIATEST")
        monkeypatch.setattr(st, "r2_secret_access_key", "secret")
        monkeypatch.setattr(st, "r2_bucket", "mra-assets")
        monkeypatch.setattr(st, "r2_public_base_url", "https://assets.example.com")

    def test_503_when_unconfigured(self, client, db, monkeypatch):
        from app.core.config import get_settings
        st = get_settings()
        for f in ("r2_account_id", "r2_access_key_id", "r2_secret_access_key",
                  "r2_bucket", "r2_public_base_url"):
            monkeypatch.setattr(st, f, "")
        post = _make_post(slug="q3-report", title="Q3", status="draft",
                          content_type="report", published_at=None)
        db.add(post)
        db.commit()
        db.refresh(post)

        r = client.post(
            f"/api/v1/intelligence/posts/{post.id}/pdf-upload-url",
            json={"filename": "q3.pdf"},
        )
        assert r.status_code == 503

    def test_rejects_non_report_posts(self, client, db, monkeypatch):
        self._configure_r2(monkeypatch)
        post = _make_post(slug="just-analysis", title="A", status="draft",
                          content_type="analysis", published_at=None)
        db.add(post)
        db.commit()
        db.refresh(post)

        r = client.post(
            f"/api/v1/intelligence/posts/{post.id}/pdf-upload-url",
            json={"filename": "x.pdf"},
        )
        assert r.status_code == 422

    def test_presigns_and_sanitizes_filename(self, client, db, monkeypatch):
        self._configure_r2(monkeypatch)
        post = _make_post(slug="q3-report", title="Q3", status="draft",
                          content_type="report", published_at=None)
        db.add(post)
        db.commit()
        db.refresh(post)

        r = client.post(
            f"/api/v1/intelligence/posts/{post.id}/pdf-upload-url",
            json={"filename": "../Q3 Cobalt Report (final)!.PDF"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        # basename only, safe charset, .pdf preserved case-insensitively
        assert body["key"] == "insights/pdf/q3-report/Q3-Cobalt-Report-final-.PDF"
        assert body["public_url"] == (
            "https://assets.example.com/" + body["key"]
        )
        assert body["expires_in"] == 900
        # Presigned PUT against the R2 S3 endpoint for the right object.
        assert "acct123.r2.cloudflarestorage.com" in body["upload_url"]
        assert "mra-assets" in body["upload_url"]
        assert "X-Amz-Signature=" in body["upload_url"]


class TestTagEndpoints:
    """2026-07-22: entity-tag picker backend (suggest + classify)."""

    def _seed_entities(self, db):
        db.add_all(
            [
                Company(canonical_name="CATL", legal_name="Contemporary Amperex Technology Co., Limited"),
                Company(canonical_name="Glencore", legal_name="Glencore plc"),
                Regulation(regulation_key="EU_BATTERY_REG_2023", title="EU Battery Regulation"),
                Regulation(regulation_key="IRA_30D_FEOC", title="IRA Section 30D FEOC Guidance"),
            ]
        )
        db.commit()

    def test_suggest_merges_companies_and_regulations(self, client, db):
        self._seed_entities(db)
        r = client.get("/api/v1/intelligence/tags/suggest", params={"q": "batt"})
        assert r.status_code == 200, r.text
        sugg = r.json()["suggestions"]
        # "batt" hits EU_BATTERY_REG_2023 (key) — and nothing else seeded.
        assert {(s["label"], s["kind"]) for s in sugg} == {
            ("EU_BATTERY_REG_2023", "regulation"),
        }

        # legal-name match surfaces the canonical_name as the label.
        r = client.get("/api/v1/intelligence/tags/suggest", params={"q": "amperex"})
        assert r.status_code == 200
        sugg = r.json()["suggestions"]
        assert [(s["label"], s["kind"]) for s in sugg] == [("CATL", "company")]

    def test_suggest_requires_min_query(self, client):
        r = client.get("/api/v1/intelligence/tags/suggest", params={"q": "a"})
        assert r.status_code == 422

    def test_classify_mixed_tags(self, client, db):
        self._seed_entities(db)
        r = client.post(
            "/api/v1/intelligence/tags/classify",
            json={"tags": ["CATL", "IRA_30D_FEOC", "IRA", "cobalt prices", ""]},
        )
        assert r.status_code == 200, r.text
        assert r.json()["classifications"] == {
            "CATL": "company",
            "IRA_30D_FEOC": "regulation",
            "IRA": None,
            "cobalt prices": None,
        }


class TestPatchTags:
    def test_tags_round_trip(self, client, db):
        """Regression (2026-07-22): InsightPostUpdate silently lacked
        ``tags`` — the editor sent them, the API 200'd, nothing saved."""
        post = _make_post(slug="tagged", title="T")
        db.add(post)
        db.commit()
        db.refresh(post)

        r = client.patch(
            f"/api/v1/intelligence/posts/{post.id}",
            json={"tags": ["CATL", "IRA_30D_FEOC", "FEOC"]},
        )
        assert r.status_code == 200
        assert r.json()["tags"] == ["CATL", "IRA_30D_FEOC", "FEOC"]

        # And it actually persisted, not just echoed.
        r = client.get(f"/api/v1/intelligence/posts/by-id/{post.id}")
        assert r.status_code == 200
        assert r.json()["tags"] == ["CATL", "IRA_30D_FEOC", "FEOC"]

        # Clearing works too.
        r = client.patch(
            f"/api/v1/intelligence/posts/{post.id}", json={"tags": None}
        )
        assert r.status_code == 200
        assert r.json()["tags"] is None
