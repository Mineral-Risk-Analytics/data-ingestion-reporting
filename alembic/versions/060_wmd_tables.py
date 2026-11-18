"""World Mining Data tables (wmd_commodities / wmd_production / wmd_group_production).

WMD (Austrian BMF, annual, free Excel) = independent cross-check layer for
ore-stage production + the stability/bloc group series.  USGS stays the
canonical scoring input (single-source-per-stage rule).  See
ADS/wmd_usgs_comparison_and_ingest_plan.md.

Revision ID: 060
Revises: 059
"""

from alembic import op
import sqlalchemy as sa

revision = "060"
down_revision = "059"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "wmd_commodities",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("wmd_name", sa.String(64), nullable=False, unique=True),
        sa.Column("commodity_group", sa.String(32)),
        sa.Column("unit", sa.String(32)),
        sa.Column("content_basis", sa.String(32)),
        sa.Column(
            "material_id",
            sa.Integer(),
            sa.ForeignKey("materials.id", ondelete="SET NULL"),
        ),
        sa.Column("notes", sa.Text()),
    )
    op.create_index("ix_wmd_commodities_material_id", "wmd_commodities", ["material_id"])

    op.create_table(
        "wmd_production",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "commodity_id",
            sa.Integer(),
            sa.ForeignKey("wmd_commodities.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("country_code", sa.String(2), nullable=False),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("volume", sa.Float(), nullable=False),
        sa.Column("source_code", sa.String(2)),
        sa.Column("edition", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("commodity_id", "country_code", "year", name="uq_wmd_prod_row"),
    )
    op.create_index("ix_wmd_production_commodity_id", "wmd_production", ["commodity_id"])
    op.create_index("ix_wmd_production_country_code", "wmd_production", ["country_code"])
    op.create_index("ix_wmd_production_year", "wmd_production", ["year"])

    op.create_table(
        "wmd_group_production",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "commodity_id",
            sa.Integer(),
            sa.ForeignKey("wmd_commodities.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("group_type", sa.String(16), nullable=False),
        sa.Column("group_key", sa.String(32), nullable=False),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("volume", sa.Float(), nullable=False),
        sa.Column("edition", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint(
            "commodity_id", "group_type", "group_key", "year", name="uq_wmd_group_row"
        ),
    )
    op.create_index(
        "ix_wmd_group_production_commodity_id", "wmd_group_production", ["commodity_id"]
    )


def downgrade() -> None:
    op.drop_table("wmd_group_production")
    op.drop_table("wmd_production")
    op.drop_table("wmd_commodities")
