"""Create material_global_risk_scores table.

Trade-flow-weighted rollup of MaterialGeographyRiskScore rows into a single
global risk view per material per scoring date.

Each row aggregates all (material, geography) pairs that have a scored
MaterialGeographyRiskScore, weighted by the geography's export trade value for
that material (falling back to MaterialProductionShare when TradeFlow has no
coverage, and to equal weighting when neither source has data).

Sits between the geo-level scores and the chemistry-level scores:

    material_geography_risk_scores  (per-geo, five pillars)
              ↓  trade-flow weighted avg across geographies
    material_global_risk_scores     (this table)
              ↓  intensity-weighted avg across minerals
    chemistry_risk_scores           (per chemistry, five pillars)

Revision ID: 015_material_global_risk_scores
Revises:     014_material_production_shares
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "015_material_global_risk_scores"
down_revision: Union[str, None] = "014_material_production_shares"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "material_global_risk_scores",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "material_id",
            sa.Integer(),
            sa.ForeignKey("materials.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("as_of_date", sa.Date(), nullable=False, index=True),
        # ── five active pillars ──────────────────────────────────────────────
        sa.Column(
            "material_concentration_score",
            sa.Float(),
            nullable=True,
            comment="0–100, trade-flow-weighted avg across geographies",
        ),
        sa.Column(
            "geopolitical_trade_score",
            sa.Float(),
            nullable=True,
            comment="0–100, trade-flow-weighted avg across geographies",
        ),
        sa.Column(
            "regulatory_compliance_score",
            sa.Float(),
            nullable=True,
            comment="0–100, trade-flow-weighted avg across geographies",
        ),
        sa.Column(
            "operational_score",
            sa.Float(),
            nullable=True,
            comment="0–100, trade-flow-weighted avg across geographies",
        ),
        sa.Column(
            "financial_pressure_score",
            sa.Float(),
            nullable=True,
            comment="0–100, trade-flow-weighted avg across geographies",
        ),
        # ── overall ─────────────────────────────────────────────────────────
        sa.Column(
            "overall_risk_score",
            sa.Float(),
            nullable=True,
            comment="0–100, weighted average of five pillars using MARKET_PILLAR_WEIGHTS",
        ),
        # ── weight auditability ──────────────────────────────────────────────
        sa.Column(
            "trade_weighted_geo_count",
            sa.Integer(),
            nullable=False,
            comment="Number of geographies that contributed to the weighted average",
        ),
        sa.Column(
            "total_trade_value_usd",
            sa.Float(),
            nullable=True,
            comment=(
                "Sum of trade_value_usd used as denominator for the weighted average. "
                "NULL when weights came entirely from production shares or equal weighting."
            ),
        ),
        # ── metadata ────────────────────────────────────────────────────────
        sa.Column(
            "rationale_json",
            JSONB(),
            nullable=True,
            comment="Per-geography weights, pillar sub-inputs, weight source used",
        ),
        sa.Column(
            "scoring_version",
            sa.String(32),
            nullable=False,
            server_default="1.0",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "material_id", "as_of_date",
            name="uq_material_global_risk_score",
        ),
    )


def downgrade() -> None:
    op.drop_table("material_global_risk_scores")
