"""insight_posts.risk_band — editorial risk severity tag on articles.

Admin content v1 (2026-07-21, Nicole): the partner can tag a post
low | med | high | crit. Editorial judgment, NOT derived from the scoring
engine — kept as its own column (not metadata_json) so the public feed
can filter on it later. Tokens match the platform band CSS levels.

Revision ID: 061
Revises: 060
"""

from alembic import op
import sqlalchemy as sa

revision = "061"
down_revision = "060"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "insight_posts",
        sa.Column(
            "risk_band",
            sa.String(8),
            nullable=True,
            comment="Editorial risk tag: low | med | high | crit (NULL = untagged)",
        ),
    )


def downgrade() -> None:
    op.drop_column("insight_posts", "risk_band")
