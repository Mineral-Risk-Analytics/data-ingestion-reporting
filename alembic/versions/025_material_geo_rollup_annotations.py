"""025 — add stage rollup annotation columns to material_geography_risk_scores.

Adds two columns that record how the Material Concentration pillar was computed
when the new Level-0 stage scores are available:

  stage_rollup_count   — number of HS stage nodes that contributed to the rollup
  stage_rollup_method  — 'stage_weighted' | 'material_fallback'

'stage_weighted' means the pillar was computed from hs_code_geography_risk_scores
using STAGE_ROLLUP_WEIGHTS.  'material_fallback' means fewer than 2 stage nodes
were available and the scorer fell back to MaterialCriticalitySignal.hhi_score.

Both columns are nullable — existing rows and rows produced before the Phase-3
scoring engine update will have NULL here.

Revision ID: 025_material_geo_rollup_annotations
Revises: 024_hs_geo_scores
Create Date: 2026-05-02
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "025_geo_rollup_annotations"
down_revision: Union[str, None] = "024_hs_geo_scores"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "material_geography_risk_scores",
        sa.Column(
            "stage_rollup_count",
            sa.SmallInteger(),
            nullable=True,
            comment=(
                "Number of HS stage nodes that contributed to the "
                "Material Concentration pillar rollup.  NULL for pre-redesign rows."
            ),
        ),
    )
    op.add_column(
        "material_geography_risk_scores",
        sa.Column(
            "stage_rollup_method",
            sa.String(32),
            nullable=True,
            comment=(
                "'stage_weighted' = computed from hs_code_geography_risk_scores; "
                "'material_fallback' = fewer than 2 stage nodes available, fell back "
                "to MaterialCriticalitySignal.hhi_score.  NULL for pre-redesign rows."
            ),
        ),
    )
    op.create_check_constraint(
        "ck_stage_rollup_method",
        "material_geography_risk_scores",
        "stage_rollup_method IN ('stage_weighted', 'material_fallback')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_stage_rollup_method", "material_geography_risk_scores", type_="check"
    )
    op.drop_column("material_geography_risk_scores", "stage_rollup_method")
    op.drop_column("material_geography_risk_scores", "stage_rollup_count")
