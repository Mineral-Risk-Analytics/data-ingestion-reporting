"""Intelligence hub content models.

InsightPost is the manually authored content that populates the public-facing
intelligence hub (mineralriskanalytics.com). It is entirely separate from
ReportInsight (auto-generated content tied to a ReportRun) in reporting.py.

Content taxonomy mirrors the scoring engine dimensions exactly so that future
"related intelligence" surfacing alongside company and market scores is
structurally possible without schema changes.

Pillar values match RiskCategory enum:
  material_concentration | geopolitical_trade | regulatory_compliance |
  operational | financial_pressure

Content types:
  analysis  — long-form, data-backed analytical pieces (5–10 min)
  signal    — short-form alerts, 1–2 paragraphs
  report    — downloadable PDF, quarterly cadence
  news      — curated external coverage with commentary
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import Boolean, DateTime, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class InsightPost(Base):
    """
    A single piece of intelligence hub content authored by the partner.

    Append-friendly — status transitions from draft → published.
    Soft-delete is handled by setting status='archived' rather than
    dropping the row, so published URLs remain resolvable (301 redirect).

    materials and geographies are stored as plain text arrays rather than FKs
    so the partner can tag content before the referenced material or geography
    has been fully seeded, and to keep the content publishing flow simple.
    """

    __tablename__ = "insight_posts"
    __table_args__ = (
        UniqueConstraint("slug", name="uq_insight_post_slug"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # ---- identity ----
    slug: Mapped[str] = mapped_column(
        String(255), nullable=False, index=True,
        comment="URL-safe identifier, e.g. 'ira-feoc-rules-cobalt-2025'",
    )
    title: Mapped[str] = mapped_column(String(512), nullable=False)

    # ---- content taxonomy (mirrors scoring engine dimensions) ----
    content_type: Mapped[str] = mapped_column(
        String(16), nullable=False, index=True,
        comment="analysis | signal | report | news",
    )
    pillar: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True, index=True,
        comment=(
            "Primary scoring pillar this post relates to: "
            "material_concentration | geopolitical_trade | "
            "regulatory_compliance | operational | financial_pressure. "
            "NULL when post spans multiple pillars."
        ),
    )
    materials: Mapped[Optional[Any]] = mapped_column(
        ARRAY(String),
        nullable=True,
        comment="Array of material canonical names, e.g. ['Lithium', 'Cobalt']",
    )
    geographies: Mapped[Optional[Any]] = mapped_column(
        ARRAY(String),
        nullable=True,
        comment="Array of ISO2 country codes, e.g. ['CN', 'CD', 'CL']",
    )
    tags: Mapped[Optional[Any]] = mapped_column(
        ARRAY(String),
        nullable=True,
        comment="Free-form searchable topic tags, e.g. ['IRA', 'FEOC'] (052)",
    )
    risk_band: Mapped[Optional[str]] = mapped_column(
        String(8), nullable=True,
        comment=(
            "Editorial risk tag: low | med | high | crit (061). Author "
            "judgment about the situation the post covers — NOT derived "
            "from the scoring engine's computed bands."
        ),
    )

    # ---- content ----
    summary: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True,
        comment="Short description shown in the feed list (1–3 sentences)",
    )
    body: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True,
        comment="Full article content in Markdown",
    )
    pdf_url: Mapped[Optional[str]] = mapped_column(
        String(1024), nullable=True,
        comment="Cloudflare R2 URL for Report-type PDFs. NULL for other content types.",
    )
    read_time_minutes: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True,
        comment="Estimated read time, shown in feed. NULL for signal/news types.",
    )

    # ---- authorship + publishing ----
    author: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, index=True,
        server_default="draft",
        comment="draft | published | archived",
    )
    published_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True,
        comment="Set when status transitions to published. Used for feed ordering.",
    )

    # ---- metadata ----
    metadata_json: Mapped[Optional[Any]] = mapped_column(
        JSONB, nullable=True,
        comment="Freeform metadata: external_url for news type, featured flag, etc.",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    pinned: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false", index=False,
        comment=(
            "Editorial top-of-feed placement (analysis/signal/news only; "
            "reports use the latest-published featured convention). 051."
        ),
    )
    hero_image_url: Mapped[Optional[str]] = mapped_column(
        String(1024), nullable=True,
        comment="Hero image for the Analysis layout shell (051)",
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
