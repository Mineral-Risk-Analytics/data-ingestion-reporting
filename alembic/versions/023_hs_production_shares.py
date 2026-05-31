"""023 — create hs_code_production_shares; drop materials.primary_producing_countries.

hs_code_production_shares stores country-level production shares per HS mapping
node per year.  Unlike material_production_shares (migration 014, which stays as
a material-level fallback), this table tracks shares at the supply chain stage
level — e.g. separate rows for cobalt ore (2605) vs cobalt hydroxide (2822).

Seeded by the MCS PDF parser (Phase 2) and by seed-hs-mappings for any manual
data.  Used as the primary source for HHI computation in the new Level-0 scorer
(hs_node_scorer.py, Phase 3).

materials.primary_producing_countries is removed — country share data already
lives in material_production_shares at material level and will move to this
table at stage level.  The JSONB array carried no year, no share fractions, and
no stage context.

Revision ID: 023_hs_production_shares
Revises: 022_hs_expand
Create Date: 2026-05-02
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "023_hs_production_shares"
down_revision: Union[str, None] = "022_hs_expand"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "hs_code_production_shares",
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
            "production_share",
            sa.Float(),
            nullable=False,
            comment="Fraction of world total production for this HS node (0.0–1.0)",
        ),
        sa.Column(
            "production_volume",
            sa.Float(),
            nullable=True,
            comment="Raw production tonnage where available (unit in unit_of_measure)",
        ),
        sa.Column(
            "unit_of_measure",
            sa.String(64),
            nullable=True,
            comment="e.g. 'metric tons', 'kilograms'",
        ),
        sa.Column(
            "reference_year",
            sa.SmallInteger(),
            nullable=False,
            comment="Data reference year (e.g. 2025 for MCS 2025)",
        ),
        sa.Column(
            "market_scope",
            sa.String(8),
            nullable=False,
            server_default="global",
            comment=(
                "global = world mine production share; "
                "us = US import source share. "
                "Never mix in the same HHI calculation."
            ),
        ),
        sa.Column(
            "source",
            sa.String(32),
            nullable=False,
            server_default="usgs_mcs",
            comment="usgs_mcs | comtrade | manual",
        ),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "hs_mapping_id",
            "country_code",
            "reference_year",
            "market_scope",
            "source",
            name="uq_hs_production_share",
        ),
        sa.CheckConstraint(
            "production_share >= 0 AND production_share <= 1",
            name="ck_hs_production_share_range",
        ),
        sa.CheckConstraint(
            "market_scope IN ('global', 'us', 'eu')",
            name="ck_hs_prod_share_scope",
        ),
    )

    op.create_index(
        "idx_hs_prod_share_mapping_year",
        "hs_code_production_shares",
        ["hs_mapping_id", "reference_year"],
    )
    op.create_index(
        "idx_hs_prod_share_country_year",
        "hs_code_production_shares",
        ["country_code", "reference_year"],
    )

    # Drop the denormalized JSONB array from materials.
    # Country share data at the material level is kept in material_production_shares.
    # This column carried no year, no percentages, and no stage context.
    op.drop_column("materials", "primary_producing_countries")


def downgrade() -> None:
    op.add_column(
        "materials",
        sa.Column(
            "primary_producing_countries",
            sa.dialects.postgresql.JSONB(),
            nullable=True,
        ),
    )
    op.drop_index("idx_hs_prod_share_country_year", table_name="hs_code_production_shares")
    op.drop_index("idx_hs_prod_share_mapping_year", table_name="hs_code_production_shares")
    op.drop_table("hs_code_production_shares")
