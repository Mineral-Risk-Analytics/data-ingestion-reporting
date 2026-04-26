"""Add insight_posts table for the intelligence hub content layer.

InsightPost is manually authored content published by the partner on the
public-facing intelligence hub (mineralriskanalytics.com). It is distinct
from report_insights (auto-generated content in report_runs).

Content taxonomy mirrors the scoring engine exactly:
  - content_type: analysis | signal | report | news
  - pillar:       material_concentration | geopolitical_trade |
                  regulatory_compliance | operational | financial_pressure
  - materials:    TEXT[] of material canonical names
  - geographies:  TEXT[] of ISO2 country codes

materials and geographies are stored as plain text arrays (not FKs) so
the partner can tag posts before the referenced material/geography is
fully seeded, and to keep the publishing workflow simple.

pdf_url is a Cloudflare R2 URL. NULL for non-Report content types.

status: draft → published → archived (no hard deletes — published URLs
stay resolvable).

Revision ID: 012_insight_posts
Revises:     011_material_geography_risk_scores
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

revision: str = "012_insight_posts"
down_revision: Union[str, None] = "011_mat_geo_risk_scores"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "insight_posts",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),

        # identity
        sa.Column(
            "slug",
            sa.String(255),
            nullable=False,
            unique=True,
            index=True,
            comment="URL-safe identifier, e.g. 'ira-feoc-rules-cobalt-2025'",
        ),
        sa.Column("title", sa.String(512), nullable=False),

        # content taxonomy
        sa.Column(
            "content_type",
            sa.String(16),
            nullable=False,
            index=True,
            comment="analysis | signal | report | news",
        ),
        sa.Column(
            "pillar",
            sa.String(64),
            nullable=True,
            index=True,
            comment=(
                "Primary scoring pillar: material_concentration | "
                "geopolitical_trade | regulatory_compliance | "
                "operational | financial_pressure. NULL when multi-pillar."
            ),
        ),
        sa.Column(
            "materials",
            ARRAY(sa.String()),
            nullable=True,
            comment="Material canonical names, e.g. ['Lithium', 'Cobalt']",
        ),
        sa.Column(
            "geographies",
            ARRAY(sa.String()),
            nullable=True,
            comment="ISO2 country codes, e.g. ['CN', 'CD', 'CL']",
        ),

        # content
        sa.Column(
            "summary",
            sa.Text(),
            nullable=True,
            comment="Short description shown in feed (1-3 sentences)",
        ),
        sa.Column(
            "body",
            sa.Text(),
            nullable=True,
            comment="Full article content in Markdown",
        ),
        sa.Column(
            "pdf_url",
            sa.String(1024),
            nullable=True,
            comment="Cloudflare R2 URL for Report-type PDFs. NULL otherwise.",
        ),
        sa.Column(
            "read_time_minutes",
            sa.Integer(),
            nullable=True,
            comment="Estimated read time shown in feed. NULL for signal/news.",
        ),

        # authorship + publishing
        sa.Column("author", sa.String(255), nullable=True),
        sa.Column(
            "status",
            sa.String(16),
            nullable=False,
            server_default="draft",
            index=True,
            comment="draft | published | archived",
        ),
        sa.Column(
            "published_at",
            sa.DateTime(timezone=True),
            nullable=True,
            index=True,
            comment="Set when status transitions to published. Feed ordering key.",
        ),

        # metadata
        sa.Column(
            "metadata_json",
            JSONB(),
            nullable=True,
            comment="Freeform: external_url for news type, featured flag, etc.",
        ),

        # timestamps
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )

    # GIN index on materials array for fast tag filtering
    # e.g. WHERE 'Lithium' = ANY(materials)
    op.create_index(
        "ix_insight_posts_materials_gin",
        "insight_posts",
        ["materials"],
        postgresql_using="gin",
    )

    # GIN index on geographies array
    op.create_index(
        "ix_insight_posts_geographies_gin",
        "insight_posts",
        ["geographies"],
        postgresql_using="gin",
    )


def downgrade() -> None:
    op.drop_index("ix_insight_posts_geographies_gin", table_name="insight_posts")
    op.drop_index("ix_insight_posts_materials_gin", table_name="insight_posts")
    op.drop_table("insight_posts")
