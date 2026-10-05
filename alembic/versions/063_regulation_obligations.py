"""DB-driven regulation obligations (Build 2, 2026-07-24).

Replaces regulatory_risk.COMPLIANCE_OBLIGATIONS (a hardcoded 8-key dict of
obligation base-points) with columns on ``regulations``, so curation can add
a new landmark regulation's obligation weight without a code change. Scoring
parity: ``coalesce(obligation_points, 0)`` reproduces ``dict.get(key, 0)``
exactly — non-obligation regs keep contributing 0 points while remaining in
scope diagnostics.

Backfill = the exact dict values (tier rationale preserved in the column
comment + scope doc). SEC_CLIMATE_2024 stays points-less on purpose (court-
stayed; it was deliberately excluded from the dict).

Revision ID: 063
Revises: 062
"""

from alembic import op
import sqlalchemy as sa

revision = "063"
down_revision = "062"
branch_labels = None
depends_on = None

_POINTS = {
    # Tier 1 — direct enforcement consequences for battery supply chains
    "UFLPA": 25,
    "EU_BATTERY_REG_2023": 20,
    "CRMA_2024": 15,
    "IRA_DOMESTIC": 15,
    # Tier 2 — significant but not yet fully effective / broader scope
    "EU_CSDDD": 10,
    "EU_REACH_COBALT": 8,
    # Tier 3 — indirect battery relevance
    "EU_CBAM": 5,
    "EU_CONFLICT_MINERALS": 3,
}


def upgrade() -> None:
    op.add_column(
        "regulations",
        sa.Column(
            "is_obligation",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
            comment=(
                "Hard legal obligation with enforcement consequences — "
                "contributes obligation_points × geo_weight to the "
                "regulatory pillar's 0-40 uplift."
            ),
        ),
    )
    op.add_column(
        "regulations",
        sa.Column(
            "obligation_points",
            sa.Integer(),
            nullable=True,
            comment=(
                "Obligation uplift base points (NULL/0 = no uplift). "
                "Calibration: a fully-weighted UFLPA reaches 25/100 from "
                "obligations alone; the pillar caps total uplift at 40."
            ),
        ),
    )
    for key, pts in _POINTS.items():
        op.execute(
            sa.text(
                "UPDATE regulations SET is_obligation = true, "
                "obligation_points = :p WHERE regulation_key = :k"
            ).bindparams(p=pts, k=key)
        )


def downgrade() -> None:
    op.drop_column("regulations", "obligation_points")
    op.drop_column("regulations", "is_obligation")
