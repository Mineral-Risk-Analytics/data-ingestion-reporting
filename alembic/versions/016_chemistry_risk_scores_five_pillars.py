"""Add three pillar columns to chemistry_risk_scores.

The original chemistry scorer only computed material_concentration_score and
geopolitical_score (two pillars). The rollup-based scorer (methodology_version
2.0) derives all five pillars from MaterialGlobalRiskScore and populates the
three new columns here. Rows written by the old scorer have NULL in these columns.

Revision ID: 016_chemistry_risk_scores_five_pillars
Revises:     015_material_global_risk_scores
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "016_chem_five_pillars"
down_revision: Union[str, None] = "015_material_global_risk_scores"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "chemistry_risk_scores",
        sa.Column(
            "regulatory_compliance_score",
            sa.Float(),
            nullable=True,
            comment="0–100 intensity-weighted; NULL for rows written by methodology_version < 2.0",
        ),
    )
    op.add_column(
        "chemistry_risk_scores",
        sa.Column(
            "operational_score",
            sa.Float(),
            nullable=True,
            comment="0–100 intensity-weighted; NULL for rows written by methodology_version < 2.0",
        ),
    )
    op.add_column(
        "chemistry_risk_scores",
        sa.Column(
            "financial_pressure_score",
            sa.Float(),
            nullable=True,
            comment="0–100 intensity-weighted; NULL for rows written by methodology_version < 2.0",
        ),
    )


def downgrade() -> None:
    op.drop_column("chemistry_risk_scores", "financial_pressure_score")
    op.drop_column("chemistry_risk_scores", "operational_score")
    op.drop_column("chemistry_risk_scores", "regulatory_compliance_score")
