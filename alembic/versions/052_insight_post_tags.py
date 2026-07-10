"""Add free-form ``tags`` array to insight_posts.

Nicole 2026-07-08: posts need searchable keywords beyond the existing
structured dimensions. Taxonomy after this migration:

  * materials[]   — canonical material names   (existing, chip-rendered)
  * geographies[] — ISO2 codes                  (existing, chip-rendered)
  * pillar        — one scoring pillar          (existing)
  * tags[]        — free-form topics: "IRA", "FEOC", "CRMA", "Section 232",
                    "DRC quota"... anything editorial that isn't a material
                    or geography. Plain strings, partner-curated; no FK so
                    tagging never blocks on seeding.

Revision ID: 052
Revises: 051
"""

from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "052"
down_revision: Union[str, None] = "051"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "insight_posts",
        sa.Column(
            "tags",
            postgresql.ARRAY(sa.String()),
            nullable=True,
            comment="Free-form searchable topic tags (e.g. IRA, FEOC, CRMA)",
        ),
    )
    # GIN index so tag filtering stays fast as content grows.
    op.create_index(
        "ix_insight_posts_tags",
        "insight_posts",
        ["tags"],
        postgresql_using="gin",
    )


def downgrade() -> None:
    op.drop_index("ix_insight_posts_tags", table_name="insight_posts")
    op.drop_column("insight_posts", "tags")
