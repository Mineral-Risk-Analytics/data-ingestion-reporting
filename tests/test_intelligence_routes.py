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
from app.models.intelligence import InsightPost
from app.models.scoring import MaterialGeographyRiskScore
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
    def test_picks_highest_geography_per_material(self, client, db):
        # Two materials, two geographies each. Highest score per material wins.
        lithium = Material(canonical_name="Lithium")
        cobalt = Material(canonical_name="Cobalt")
        db.add_all([lithium, cobalt])
        db.flush()

        as_of = date(2026, 4, 1)
        db.add_all(
            [
                MaterialGeographyRiskScore(
                    material_id=lithium.id,
                    geography_code="CL",
                    as_of_date=as_of,
                    overall_risk_score=42.0,
                ),
                MaterialGeographyRiskScore(
                    material_id=lithium.id,
                    geography_code="CN",
                    as_of_date=as_of,
                    overall_risk_score=78.0,
                ),
                MaterialGeographyRiskScore(
                    material_id=cobalt.id,
                    geography_code="CD",
                    as_of_date=as_of,
                    overall_risk_score=88.0,
                ),
                MaterialGeographyRiskScore(
                    material_id=cobalt.id,
                    geography_code="ID",
                    as_of_date=as_of,
                    overall_risk_score=55.0,
                ),
            ]
        )
        db.commit()

        r = client.get("/api/v1/intelligence/risk-summary")
        assert r.status_code == 200, r.text
        body = r.json()

        assert body["as_of_date"] == "2026-04-01"
        # Order is by overall_risk_score DESC
        assert [m["material_name"] for m in body["materials"]] == ["Cobalt", "Lithium"]

        cobalt_bar = body["materials"][0]
        assert cobalt_bar["top_geography"] == "CD"
        assert cobalt_bar["top_geography_score"] == 88.0

        lithium_bar = body["materials"][1]
        assert lithium_bar["top_geography"] == "CN"
        assert lithium_bar["top_geography_score"] == 78.0

    def test_uses_only_latest_as_of_date_per_pair(self, client, db):
        nickel = Material(canonical_name="Nickel")
        db.add(nickel)
        db.flush()

        # Two rows for the same (material, geography). Only the newer one
        # should drive the bar.
        db.add_all(
            [
                MaterialGeographyRiskScore(
                    material_id=nickel.id,
                    geography_code="ID",
                    as_of_date=date(2025, 10, 1),
                    overall_risk_score=20.0,
                ),
                MaterialGeographyRiskScore(
                    material_id=nickel.id,
                    geography_code="ID",
                    as_of_date=date(2026, 4, 1),
                    overall_risk_score=66.0,
                ),
            ]
        )
        db.commit()

        r = client.get("/api/v1/intelligence/risk-summary")
        assert r.status_code == 200
        bar = r.json()["materials"][0]
        assert bar["material_name"] == "Nickel"
        assert bar["top_geography_score"] == 66.0

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
