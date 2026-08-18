"""V1 scoring (4.0): widen ck_stage_rollup_method for the stage-max path.

Spec: docs/design/scoring_v1_spec.md.  The concentration pillar no longer
averages Level-0 node composites; it takes the max per-stage structural
sub-score (stage_concentration.py).  New method labels:

    stage_max      — geography produces at >=1 fresh stage; score is the
                     max sub-score, stage_rollup_count = fresh stages the
                     geography holds a share in
    no_share_data  — geography holds no share at any fresh stage;
                     concentration = 0 (V1: non-producers score zero,
                     no material-level fallback)

Legacy labels ('stage_weighted', 'material_fallback') stay valid so 3.x
rows remain readable until the post-rescore cleanup deletes them.

Revision ID: 059
Revises: 058
"""

from alembic import op

revision = "059"
down_revision = "058"
branch_labels = None
depends_on = None

_TABLE = "material_geography_risk_scores"
_NAME = "ck_stage_rollup_method"

_V1_CHECK = (
    "stage_rollup_method IN "
    "('stage_weighted', 'material_fallback', 'stage_max', 'no_share_data')"
)
_LEGACY_CHECK = (
    "stage_rollup_method IN ('stage_weighted', 'material_fallback')"
)


def upgrade() -> None:
    op.drop_constraint(_NAME, _TABLE, type_="check")
    op.create_check_constraint(_NAME, _TABLE, _V1_CHECK)


def downgrade() -> None:
    op.drop_constraint(_NAME, _TABLE, type_="check")
    op.create_check_constraint(_NAME, _TABLE, _LEGACY_CHECK)
