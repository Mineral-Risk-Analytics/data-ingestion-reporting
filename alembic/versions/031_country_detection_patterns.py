"""Add detection_patterns and is_sanctions_risk to countries.

Revision ID: 031
Revises: 030
Create Date: 2026-04-30

Changes
-------
1. ``detection_patterns`` JSONB NULL — array of pattern objects used by
   GeographyCache to detect country references in free-form text.
   Structure: [{"pattern": "<lowercase string>", "context": "primary|mentioned"}]
   "primary"  → country is directly referenced (relevance 0.9)
   "mentioned" → adjectival/contextual reference (relevance 0.6)

2. ``is_sanctions_risk`` BOOLEAN NOT NULL DEFAULT FALSE — true when a country
   has a high concentration of sanctioned entities in the OpenSanctions dataset.
   Replaces the hardcoded ``_DEFAULT_HIGH_CONCENTRATION_GEOS`` list in
   opensanctions.py.

Populate via:
    bdi-ingest seed-countries   (re-run to pick up new columns)
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "031"
down_revision = "030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "countries",
        sa.Column(
            "detection_patterns",
            JSONB,
            nullable=True,
            comment=(
                "Array of {pattern, context} objects for free-text country detection. "
                "context is 'primary' (direct reference, relevance 0.9) or "
                "'mentioned' (adjectival/contextual, relevance 0.6)."
            ),
        ),
    )
    op.add_column(
        "countries",
        sa.Column(
            "is_sanctions_risk",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("false"),
            comment=(
                "True when this country has a high concentration of sanctioned entities "
                "in OpenSanctions. Replaces the hardcoded _DEFAULT_HIGH_CONCENTRATION_GEOS "
                "list in opensanctions.py."
            ),
        ),
    )


def downgrade() -> None:
    op.drop_column("countries", "is_sanctions_risk")
    op.drop_column("countries", "detection_patterns")
