"""Add regulations.material_enforcement_weights — per-material enforcement.

Revision ID: 065
Revises: 064

The all-goods regulations (UFLPA, FLR, CSDDD, S-211) apply to every
material in law, but their ENFORCEMENT concentrates in specific sectors —
UFLPA detentions are dominated by polysilicon-adjacent goods, not rhenium.
The flat application created a ~35-point regulatory floor cluster across
twelve materials after the 4.3 rescore.

This JSONB mirrors geography_compliance_weights on the material axis:
keys are material canonical names plus optional "DEFAULT"; resolution is
exact name → "DEFAULT" → 1.0. The 1.0 fallback makes the column inert
until curated — no score changes ship with this migration; the workbook's
EnforcementWeights sheet is the curation surface. Decided 2026-07-27
(Nicole).
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "065"
down_revision = "064"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "regulations",
        sa.Column(
            "material_enforcement_weights", JSONB(), nullable=True,
            comment=(
                "Per-material enforcement weight (0-1) keyed by canonical "
                "material name; 'DEFAULT' fallback; NULL/missing = 1.0 "
                "(full points). Curated via the regulation workbook's "
                "EnforcementWeights sheet."
            ),
        ),
    )


def downgrade() -> None:
    op.drop_column("regulations", "material_enforcement_weights")
