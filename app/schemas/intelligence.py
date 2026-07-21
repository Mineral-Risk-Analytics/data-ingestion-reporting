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
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict

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


class InsightPostDetail(InsightPostListItem):
    """Single-post view — full Markdown body + free-form metadata."""

    body: Optional[str] = None
    metadata_json: Optional[dict[str, Any]] = None
    status: str
    created_at: datetime
    updated_at: datetime


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


class InsightPostUpdate(BaseModel):
    """Body for ``PATCH /intelligence/posts/{post_id}`` — all fields optional.

    Status transitions are NOT exposed here; use ``/publish``, ``/unpublish``,
    or ``/archive`` instead so each lifecycle change is an explicit verb.
    """

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
