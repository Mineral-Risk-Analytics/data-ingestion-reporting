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


class MaterialRiskBar(BaseModel):
    """One row of the public sidebar's risk indicator strip.

    ``top_geography`` is the geography with the highest ``overall_risk_score``
    for the material as of ``RiskSummary.as_of_date``. Materials with no
    ``MaterialGeographyRiskScore`` rows are excluded from the parent
    ``RiskSummary`` entirely.
    """

    material_id: int
    material_name: str
    overall_risk_score: Optional[float] = None
    top_geography: Optional[str] = None  # ISO2 code
    top_geography_score: Optional[float] = None


class RiskSummary(BaseModel):
    """Payload for ``GET /intelligence/risk-summary`` — sidebar feed.

    ``as_of_date`` is the most recent ``MaterialGeographyRiskScore.as_of_date``
    across all materials. Bars are ordered by ``overall_risk_score DESC``.
    """

    as_of_date: Optional[date] = None
    materials: list[MaterialRiskBar]
