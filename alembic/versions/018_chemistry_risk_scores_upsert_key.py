"""Add unique key for idempotent chemistry rescoring.

Enforces one row per (battery_chemistry_id, as_of_date, methodology_version)
so rerunning the same scoring job on the same day updates the prior result
instead of appending duplicates.

Before creating the constraint, this migration deduplicates any existing rows
by keeping the most recently computed row per logical key.

Revision ID: 018_chemistry_risk_scores_upsert_key
Revises:     017_reg_geo_weights
"""

from typing import Sequence, Union

from alembic import op

revision: str = "018_chem_risk_upsert_key"
down_revision: Union[str, None] = "017_reg_geo_weights"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Keep the newest row for each logical chemistry score key.
    op.execute(
        """
        WITH ranked AS (
            SELECT
                id,
                ROW_NUMBER() OVER (
                    PARTITION BY battery_chemistry_id, as_of_date, methodology_version
                    ORDER BY computed_at DESC NULLS LAST, created_at DESC NULLS LAST, id DESC
                ) AS rn
            FROM chemistry_risk_scores
        )
        DELETE FROM chemistry_risk_scores c
        USING ranked r
        WHERE c.id = r.id
          AND r.rn > 1
        """
    )

    op.create_unique_constraint(
        "uq_chemistry_risk_score_key",
        "chemistry_risk_scores",
        ["battery_chemistry_id", "as_of_date", "methodology_version"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_chemistry_risk_score_key",
        "chemistry_risk_scores",
        type_="unique",
    )
