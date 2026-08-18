"""Add ``pinned`` + ``hero_image_url`` to insight_posts.

Editorial decisions (Nicole, 2026-07-08):
  * Featured Report slot = latest published ``report``-type post by
    convention — NO schema support needed (or wanted; one mechanism).
  * ``pinned`` gives the partner editorial top-of-feed placement for
    analysis / signal / news posts. Pinned posts render above the
    date-ordered feed (newest-first among themselves), show a "Pinned"
    chip, and appear in filtered views only when they match the filter.
    Reports are NOT pinnable (the featured slot is their mechanism) —
    enforced at the API layer, not by constraint, so the rule can evolve.
  * ``hero_image_url`` supports the Analysis layout shell's hero image
    (docx-extracted or manually set; served via /content-assets/ or R2).

Revision ID: 051
Revises: 050
"""

from __future__ import annotations

from typing import Union

import sqlalchemy as sa

from alembic import op

revision: str = "051"
down_revision: Union[str, None] = "050"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "insight_posts",
        sa.Column(
            "pinned",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
            comment=(
                "Editorial top-of-feed placement (analysis/signal/news only; "
                "reports use the latest-published featured convention)"
            ),
        ),
    )
    op.add_column(
        "insight_posts",
        sa.Column(
            "hero_image_url",
            sa.String(1024),
            nullable=True,
            comment="Hero image for the Analysis layout shell",
        ),
    )
    # Partial index: the feed query sorts pinned-first among published rows.
    op.create_index(
        "ix_insight_posts_pinned_published",
        "insight_posts",
        ["pinned"],
        postgresql_where=sa.text("status = 'published' AND pinned"),
    )


def downgrade() -> None:
    op.drop_index("ix_insight_posts_pinned_published", table_name="insight_posts")
    op.drop_column("insight_posts", "hero_image_url")
    op.drop_column("insight_posts", "pinned")
