"""Create material_production_shares table.

Stores country-level production volume and share (fraction of world total)
for each material per reference year. Populated by ``bdi-ingest ingest-usgs``
from the USGS Mineral Commodity Summaries world data CSV.

Used by the global material score rollup to weight geography-level risk scores
by each country's share of world production, producing a trade-flow-weighted
composite risk score for the material as a whole.

Revision ID: 014_material_production_shares
Revises:     013_facility_material_links
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "014_material_production_shares"
down_revision: Union[str, None] = "013_facility_material_links"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "material_production_shares",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "material_id",
            sa.Integer(),
            sa.ForeignKey("materials.id", ondelete="CASCADE"),
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
            "reference_year",
            sa.Integer(),
            nullable=False,
            comment="MCS publication year (e.g. 2025 for MCS 2025)",
        ),
        sa.Column(
            "production_volume",
            sa.Float(),
            nullable=True,
            comment="Raw production tonnage from MCS (units in unit_of_measure)",
        ),
        sa.Column(
            "production_share",
            sa.Float(),
            nullable=False,
            comment="Fraction of world total production (0.0–1.0)",
        ),
        sa.Column(
            "unit_of_measure",
            sa.String(64),
            nullable=True,
            comment="Unit from MCS UNIT_MEAS column, e.g. 'metric tons'",
        ),
        sa.Column(
            "data_source",
            sa.String(64),
            nullable=False,
            server_default="usgs_mcs",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "material_id", "country_code", "reference_year",
            name="uq_material_production_share",
        ),
    )


def downgrade() -> None:
    op.drop_table("material_production_shares")
