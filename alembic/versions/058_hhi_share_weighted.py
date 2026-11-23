"""hs_code_geography_risk_scores.hhi_share_weighted + share_weighting.

Nicole 2026-07-17: the Fix B share-weighted HHI landed in the scoring
code (hs_node_scorer.py, ``hhi_share_weighted`` computed as
``hhi_at_stage × √production_share``) but the DB columns were never
added.  Every ``score_hs_node_geography`` call was raising
``UndefinedColumn`` inside the ``ON CONFLICT DO UPDATE`` clause and the
batch loop was catching it as a per-pair skip — hence the "0 scored,
91 skipped" that made it look like a data problem.

The two new columns record what actually fed the composite:

* ``hhi_share_weighted`` — the value used in the composite's hhi
  component (``hhi_at_stage × f(production_share)``).  NULL for
  event-only / operational_only rows where hhi is absent.
* ``share_weighting`` — the weighting function name, currently 'sqrt'
  per the partner decision documented in
  ``docs/design/concentration_share_weighting.md`` (linear was evaluated
  and rejected).  NULL when no hhi component is present.

Both are nullable — historical rows scored before Fix B are left as NULL,
consistent with methodology_version staying stable across the append-only
history (docs/hs-code-redesign.md § Decision 7).

Revision ID: 058
Revises: 057
"""

from alembic import op
import sqlalchemy as sa

revision = "058"
down_revision = "057"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "hs_code_geography_risk_scores",
        sa.Column("hhi_share_weighted", sa.Float(), nullable=True),
    )
    op.add_column(
        "hs_code_geography_risk_scores",
        sa.Column("share_weighting", sa.String(length=16), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("hs_code_geography_risk_scores", "share_weighting")
    op.drop_column("hs_code_geography_risk_scores", "hhi_share_weighted")
