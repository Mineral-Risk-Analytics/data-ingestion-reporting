"""024 — create hs_code_geography_risk_scores (Level-0 scoring table).

This is the new bottom of the scoring stack.  One row per
(hs_mapping_id, country_code, as_of_date, market_scope), storing stage-specific
risk sub-scores that roll up into material_geography_risk_scores (Level 1).

Sub-scores stored per node:
  production_share    — this country's share of world production for this HS stage
  hhi_at_stage        — Σ(share²) across all countries for this HS node/year
  tariff_exposure     — 0–1, from tariff event scoring scoped to this HS code
  export_restriction  — 0–1, from regulatory event scoring scoped to this HS code
  composite_node_score — 0–100, weighted combination of sub-scores

Scoring chain after this migration:
  hs_code_geography_risk_scores   ← NEW (Level 0)
          ↓ stage-weighted rollup (STAGE_ROLLUP_WEIGHTS in market_aggregator.py)
  material_geography_risk_scores  (Level 1)
          ↓ trade-flow weighted avg
  material_global_risk_scores     (Level 2)
          ↓ intensity-weighted avg
  chemistry_risk_scores           (Level 3)
          ↓ pillar-weighted avg
  company_scores                  (Level 4)

Revision ID: 024_hs_geo_scores
Revises: 023_hs_production_shares
Create Date: 2026-05-02
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "024_hs_geo_scores"
down_revision: Union[str, None] = "023_hs_production_shares"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "hs_code_geography_risk_scores",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "hs_mapping_id",
            sa.Integer(),
            sa.ForeignKey("hs_code_material_mappings.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "country_code",
            sa.String(2),
            nullable=False,
            index=True,
            comment="ISO 3166-1 alpha-2 country code",
        ),
        sa.Column(
            "as_of_date",
            sa.Date(),
            nullable=False,
            index=True,
        ),
        # Core sub-scores (0.0–1.0 raw; composite_node_score is 0–100)
        sa.Column(
            "production_share",
            sa.Float(),
            nullable=True,
            comment="This country's share of world production for this HS stage node",
        ),
        sa.Column(
            "hhi_at_stage",
            sa.Float(),
            nullable=True,
            comment="Σ(production_share²) across all countries for this HS node and year",
        ),
        sa.Column(
            "tariff_exposure",
            sa.Float(),
            nullable=True,
            comment="0–1, derived from tariff risk events scoped to this HS code",
        ),
        sa.Column(
            "export_restriction",
            sa.Float(),
            nullable=True,
            comment="0–1, derived from export restriction events scoped to this HS code",
        ),
        sa.Column(
            "composite_node_score",
            sa.Float(),
            nullable=True,
            comment="0–100, weighted combination of sub-scores for this node",
        ),
        sa.Column(
            "market_scope",
            sa.String(8),
            nullable=False,
            server_default="global",
            comment="Inherited from hs_code_material_mappings.market_scope",
        ),
        sa.Column(
            "methodology_version",
            sa.String(8),
            nullable=False,
            server_default="1.0",
            comment="Scoring methodology version — must be stable across persisted rows",
        ),
        sa.Column(
            "metadata_json",
            sa.dialects.postgresql.JSONB(),
            nullable=True,
            comment="Event IDs consumed, weight breakdown, data coverage notes",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "hs_mapping_id",
            "country_code",
            "as_of_date",
            "market_scope",
            name="uq_hs_geo_score",
        ),
        sa.CheckConstraint(
            "market_scope IN ('global', 'us', 'eu')",
            name="ck_hs_geo_score_scope",
        ),
    )

    op.create_index(
        "idx_hs_geo_score_mapping_date",
        "hs_code_geography_risk_scores",
        ["hs_mapping_id", "as_of_date"],
        postgresql_using="btree",
    )
    op.create_index(
        "idx_hs_geo_score_country_date",
        "hs_code_geography_risk_scores",
        ["country_code", "as_of_date"],
        postgresql_using="btree",
    )


def downgrade() -> None:
    op.drop_index("idx_hs_geo_score_country_date", table_name="hs_code_geography_risk_scores")
    op.drop_index("idx_hs_geo_score_mapping_date", table_name="hs_code_geography_risk_scores")
    op.drop_table("hs_code_geography_risk_scores")
