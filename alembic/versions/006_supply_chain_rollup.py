"""Supply chain rollup: risk_event_facilities + propagation columns on company_scores.

Adds the missing ``risk_event_facilities`` junction table (the structured link
from a ``RiskEvent`` to a specific ``Facility``, parallel to
``risk_event_geographies`` / ``risk_event_materials`` / ``risk_event_regulations``).
This unblocks facility-tagged scoring evidence flowing into the geopolitical and
operational pillars.

Also adds two columns to ``company_scores`` to back the new sixth pillar
introduced by SCORING_VERSION 3.0:

- ``supply_chain_propagation_score`` (float, nullable) — the weighted rollup of
  reachable suppliers' overall risk scores. NULL means "no propagation signal
  available for this run" (no suppliers in graph, or scoped run).
- ``propagation_depth_used`` (int, nullable) — the actual BFS depth that
  produced the score (so the UI can distinguish a depth-1 rollup from depth-2).

Apply via:
  alembic -x direct=true upgrade head
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "006_supply_chain_rollup"
down_revision: Union[str, None] = "005_seed_review_tables"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "risk_event_facilities",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("risk_event_id", sa.Integer(), nullable=False),
        sa.Column(
            "facility_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "relevance_score", sa.Float(), nullable=False, server_default="1.0"
        ),
        sa.Column("match_reason", sa.String(64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["risk_event_id"], ["risk_events.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["facility_id"], ["facilities.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "risk_event_id", "facility_id", name="uq_risk_event_facility"
        ),
    )
    op.create_index(
        "ix_risk_event_facilities_event",
        "risk_event_facilities",
        ["risk_event_id"],
    )
    op.create_index(
        "ix_risk_event_facilities_facility",
        "risk_event_facilities",
        ["facility_id"],
    )

    op.add_column(
        "company_scores",
        sa.Column(
            "supply_chain_propagation_score", sa.Float(), nullable=True
        ),
    )
    op.add_column(
        "company_scores",
        sa.Column("propagation_depth_used", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("company_scores", "propagation_depth_used")
    op.drop_column("company_scores", "supply_chain_propagation_score")
    op.drop_index(
        "ix_risk_event_facilities_facility", table_name="risk_event_facilities"
    )
    op.drop_index(
        "ix_risk_event_facilities_event", table_name="risk_event_facilities"
    )
    op.drop_table("risk_event_facilities")
