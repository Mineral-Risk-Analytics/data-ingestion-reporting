"""Pydantic schemas for the Intelligence Hub (public content site).

Phase 4 — backs ``mineralriskanalytics.com``. The Intelligence Hub is the
manually authored, public-facing content layer that sits next to the
auto-computed market scores. ``InsightPost`` rows drive the post feed and
``MaterialGeographyRiskScore`` rows drive the sidebar risk indicator bars.

Two reads × three writes:

  * Reads (public, no auth):
      - ``InsightPostListItem`` — feed-card view; omits ``body`` to keep the
        list payload small.
      - ``InsightPostDetail`` — single-post view; includes ``body`` Markdown
        and ``metadata_json``.
      - ``RiskSummary`` / ``MaterialRiskBar`` — sidebar bars derived from
        ``MaterialGeographyRiskScore`` (highest-scoring geography per
        material).

  * Writes (admin, auth required):
      - ``InsightPostCreate`` — POST body; status starts at ``draft``.
      - ``InsightPostUpdate`` — PATCH body; all fields optional. Does not
        change ``status`` (publish / unpublish / archive are dedicated verbs).

Note: ``status`` and ``published_at`` are intentionally NOT writable by
``InsightPostUpdate`` — clients use the ``/publish``, ``/unpublish``, and
``/archive`` action endpoints instead, so the lifecycle stays explicit.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.intelligence_entities import RiskBandOut


# ---------------------------------------------------------------------------
# InsightPost — read views
# ---------------------------------------------------------------------------


class InsightPostListItem(BaseModel):
    """Feed-card view. ``body`` is omitted to keep list payloads small."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    slug: str
    title: str
    content_type: str  # analysis | signal | report | news
    pillar: Optional[str] = None
    materials: Optional[list[str]] = None
    geographies: Optional[list[str]] = None
    summary: Optional[str] = None
    read_time_minutes: Optional[int] = None
    author: Optional[str] = None
    published_at: Optional[datetime] = None
    pdf_url: Optional[str] = None  # non-null for ``content_type == "report"``
    pinned: bool = False
    hero_image_url: Optional[str] = None
    tags: Optional[list[str]] = None
    # 2026-07-21 (admin content v1): lifecycle state, so the admin inbox can
    # tell drafts from archived (GET /posts/drafts returns both). Public
    # feed rows are always "published" — harmless there.
    status: str = "draft"
    # Editorial risk severity tag (061): low | med | high | crit. Author
    # judgment, not a scoring-engine output. NULL = untagged.
    risk_band: Optional[str] = None


class EntityLinkOut(BaseModel):
    """A resolved outbound link from an article to a public entity page —
    the article→entity direction of linking (2026-07-22). Computed from
    the post's tags: only entities that are publicly VISIBLE are included
    (company.is_published / regulation.verified), so the link never 404s.
    """

    kind: Literal["company", "regulation"]
    label: str          # tag string (canonical_name / regulation_key)
    url: str            # public hub path


class InsightPostDetail(InsightPostListItem):
    """Single-post view — full Markdown body + free-form metadata."""

    body: Optional[str] = None
    metadata_json: Optional[dict[str, Any]] = None
    # status inherited from InsightPostListItem (2026-07-21)
    created_at: datetime
    updated_at: datetime
    # Resolved outbound entity links (public slug route populates this;
    # empty on admin fetches — the editor doesn't need it).
    entity_links: list[EntityLinkOut] = []


# ---------------------------------------------------------------------------
# InsightPost — write payloads
# ---------------------------------------------------------------------------


class InsightPostCreate(BaseModel):
    """Body for ``POST /intelligence/posts``. Created post starts as ``draft``."""

    slug: str
    title: str
    content_type: str
    pillar: Optional[str] = None
    materials: Optional[list[str]] = None
    geographies: Optional[list[str]] = None
    summary: Optional[str] = None
    body: Optional[str] = None
    pdf_url: Optional[str] = None
    read_time_minutes: Optional[int] = None
    author: Optional[str] = None
    metadata_json: Optional[dict[str, Any]] = None
    tags: Optional[list[str]] = None
    # Article date (2026-07-21, Nicole): the piece's WRITTEN date — upload
    # date isn't always it. Pre-set here (or via PATCH), the /publish verb
    # keeps it (it only stamps published_at when still NULL).
    published_at: Optional[datetime] = None


# Literal types (not field_validators raising ValueError): the app's custom
# RequestValidationError handler JSON-serializes error ctx, and a raised
# ValueError object in ctx is not serializable — Literal mismatch errors are.
RiskBand = Literal["low", "med", "high", "crit"]
ContentType = Literal["analysis", "signal", "report", "news"]
_SLUG_RE = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"


class InsightPostUpdate(BaseModel):
    """Body for ``PATCH /intelligence/posts/{post_id}`` — all fields optional.

    Status transitions are NOT exposed here; use ``/publish``, ``/unpublish``,
    or ``/archive`` instead so each lifecycle change is an explicit verb.

    2026-07-21 additions: ``slug`` (frontend locks it after first publish
    and warns — the API itself allows the change; 409 on collision),
    ``content_type``, and ``risk_band`` (editorial low|med|high|crit).
    """

    slug: Optional[str] = Field(default=None, pattern=_SLUG_RE, max_length=255)
    content_type: Optional[ContentType] = None
    risk_band: Optional[RiskBand] = None
    title: Optional[str] = None
    pillar: Optional[str] = None
    materials: Optional[list[str]] = None
    geographies: Optional[list[str]] = None
    summary: Optional[str] = None
    body: Optional[str] = None
    pdf_url: Optional[str] = None
    read_time_minutes: Optional[int] = None
    author: Optional[str] = None
    metadata_json: Optional[dict[str, Any]] = None
    # tags was MISSING here until 2026-07-22 while the editor sent it on
    # every save — pydantic silently drops unknown fields, so tag edits
    # 200'd and vanished. (Create had it; Update didn't. Mirror-drift.)
    tags: Optional[list[str]] = None
    # Article date override — see InsightPostCreate.published_at. Sending
    # null clears it (a subsequent /publish re-stamps "now"). Also the
    # public feed's sort key, so backdating slots the post into history.
    published_at: Optional[datetime] = None


class FacetSuggestion(BaseModel):
    """One suggestion for a metadata picker (materials / geographies).

    ``value`` is the EXACT string stored in the column — canonical_name
    for materials, ISO2 for geographies — so the ?material=/?geography=
    filters and any future profile page match verbatim.
    """

    value: str
    label: str
    hint: Optional[str] = None


class FacetSuggestResponse(BaseModel):
    suggestions: list[FacetSuggestion]
    # (schema block; endpoints live in routes/intelligence.py)


class TagSuggestion(BaseModel):
    """One autocomplete suggestion for the editor's tag picker.

    ``label`` is the EXACT string the tag must carry for entity linking:
    company canonical_name / regulation_key (the linked_posts contract in
    intelligence_entities._tagged_posts matches verbatim).
    """

    label: str
    kind: Literal["company", "regulation"]
    hint: Optional[str] = None  # regulation title / company legal name


class TagSuggestResponse(BaseModel):
    suggestions: list[TagSuggestion]


class TagClassifyIn(BaseModel):
    tags: list[str] = Field(max_length=100)


class TagClassifyOut(BaseModel):
    """kind per tag: "company" | "regulation" | None (plain topic tag).
    Lets the editor mark which existing tags actually link somewhere."""

    classifications: dict[str, Optional[str]]


class PdfUploadUrlIn(BaseModel):
    """Body for ``POST /posts/{id}/pdf-upload-url`` (report PDFs → R2)."""

    filename: str = Field(min_length=1, max_length=255)


class PdfUploadUrlOut(BaseModel):
    upload_url: str   # presigned PUT — browser uploads directly to R2
    public_url: str   # what pdf_url should be set to after the PUT succeeds
    key: str          # object key, e.g. insights/pdf/{slug}/{filename}
    expires_in: int   # seconds the presigned URL stays valid


# ---------------------------------------------------------------------------
# Risk summary — sidebar risk indicator bars
# ---------------------------------------------------------------------------
    hero_image_url: Optional[str] = None
    tags: Optional[list[str]] = None


class MaterialRiskBar(BaseModel):
    """One row of the public sidebar's risk indicator strip.

    2026-07-21 rewrite: sourced from the latest ``MaterialGlobalRiskScore``
    (L2 rollup) per material — the SAME number the platform materials page
    shows — replacing the old max-geography-L1 shape (``top_geography`` /
    ``top_geography_score``), which predated the global rollup and publicly
    disagreed with the platform (cobalt: CD geo 90.2 vs rollup 64.8).

    ``band`` carries label ("Critical") / level token for CSS ("crit") /
    score (0-100, 1dp) from ``bands.py`` cuts — see RiskBandOut.
    """

    material_id: int
    material_name: str
    band: RiskBandOut


class RiskSummary(BaseModel):
    """Payload for ``GET /intelligence/risk-summary`` — sidebar feed.

    ``as_of_date`` is the most recent contributing rollup ``as_of_date``.
    Bars are ordered by score DESC (name ASC tiebreak); the frontend picks
    its display subset (hybrid: top-risk cluster + core battery set).
    Materials failing the insufficient-data gate (concentration pillar
    unscored) are excluded server-side — banding them would read
    false-low in public (e.g. Germanium).
    """

    as_of_date: Optional[date] = None
    materials: list[MaterialRiskBar]
