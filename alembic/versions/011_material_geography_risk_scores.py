"""Add material_geography_risk_scores table.

This table is the primary output of the market intelligence layer — risk scores
at the material × geography intersection, computed without any company data.

Four active pillars stored per row (financial_pressure and
supply_chain_propagation are company-specific and excluded):
  - material_concentration_score
  - geopolitical_trade_score
  - regulatory_compliance_score
  - operational_score
  - overall_risk_score  (renormalised across the four above)

Unique constraint on (material_id, geography_code, as_of_date) ensures one
authoritative score per pair per run date. Append-only — historical rows
are preserved for trend analysis.

Revision ID: 011_material_geography_risk_scores
Revises:     010_company_facilities_junction
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "011_mat_geo_risk_scores"
down_revision: Union[str, None] = "010_company_facilities_junction"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "material_geography_risk_scores",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "material_id",
            sa.Integer(),
            sa.ForeignKey("materials.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "geography_code",
            sa.String(2),
            nullable=False,
            index=True,
            comment="ISO 3166-1 alpha-2, e.g. CN, CD, CL",
        ),
        sa.Column("as_of_date", sa.Date(), nullable=False, index=True),
        # Four active market-level pillars
        sa.Column(
            "material_concentration_score",
            sa.Float(),
            nullable=True,
            comment="0–100, material-level criticality × concentration sub-inputs",
        ),
        sa.Column(
            "geopolitical_trade_score",
            sa.Float(),
            nullable=True,
            comment="0–100, events + geography HCG status",
        ),
        sa.Column(
            "regulatory_compliance_score",
            sa.Float(),
            nullable=True,
            comment="0–100, regulations scoped to this material + geography",
        ),
        sa.Column(
            "operational_score",
            sa.Float(),
            nullable=True,
            comment="0–100, operational events for this material/geography pair",
        ),
        sa.Column(
            "financial_pressure_score",
            sa.Float(),
            nullable=True,
            comment=(
                "0–100, reframed for market level: commodity price volatility "
                "(base signal) + price spike events (leverage proxy) + producer "
                "stress events / price crash (liquidity proxy)"
            ),
        ),
        # Overall
        sa.Column(
            "overall_risk_score",
            sa.Float(),
            nullable=True,
            comment="0–100, renormalised weighted average of four active pillars",
        ),
        # Metadata
        sa.Column(
            "event_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
            comment="Total distinct events consumed in this scoring run",
        ),
        sa.Column(
            "rationale_json",
            JSONB(),
            nullable=True,
            comment="Sub-inputs, top evidence IDs, criticality signal source, weights used",
        ),
        sa.Column(
            "scoring_version",
            sa.String(32),
            nullable=False,
            server_default="3.0",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "material_id",
            "geography_code",
            "as_of_date",
            name="uq_mat_geo_risk_score",
        ),
    )


def downgrade() -> None:
    op.drop_table("material_geography_risk_scores")
