"""Add MRDS columns to facilities and create facility_material_links table.

``mrds_dep_id`` is the USGS Mineral Resources Data System deposit record ID.
Used as the deduplication key when upserting MRDS-sourced facilities so that
re-runs update existing rows rather than inserting duplicates.

``name`` stores the human-readable site name from MRDS ``site_name``.
NULL for facilities seeded manually (cell factories, pack plants, recycling).

``facility_material_links`` maps a facility to the specific minerals it
produces, along with known annual capacity. This is the bridge that lets
the operational scoring pillar query "how much lithium mine capacity is
operating vs. mothballed in geography X" without iterating all facilities.

Revision ID: 013_facility_material_links
Revises:     012_insight_posts
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "013_facility_material_links"
down_revision: Union[str, None] = "012_insight_posts"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add MRDS external ID to facilities for dedup on re-ingestion.
    op.add_column(
        "facilities",
        sa.Column(
            "mrds_dep_id",
            sa.String(64),
            nullable=True,
            comment="USGS MRDS deposit record ID (dep_id); dedup key for upserts",
        ),
    )
    op.create_index(
        "ix_facilities_mrds_dep_id",
        "facilities",
        ["mrds_dep_id"],
        unique=True,
        postgresql_where=sa.text("mrds_dep_id IS NOT NULL"),
    )
    op.add_column(
        "facilities",
        sa.Column(
            "name",
            sa.String(256),
            nullable=True,
            comment="Human-readable site name (MRDS site_name); NULL for seeded facilities",
        ),
    )

    # Facility ↔ material link: which minerals does each facility produce?
    op.create_table(
        "facility_material_links",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "facility_id",
            UUID(as_uuid=True),
            sa.ForeignKey("facilities.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "material_id",
            sa.Integer(),
            sa.ForeignKey("materials.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "annual_capacity_tpy",
            sa.Float(),
            nullable=True,
            comment="Annual production capacity in tonnes per year (null = unknown)",
        ),
        sa.Column(
            "capacity_unit",
            sa.String(32),
            nullable=False,
            server_default="t/yr",
            comment="Unit for annual_capacity_tpy — normally 't/yr', sometimes 'kt/yr'",
        ),
        sa.Column(
            "is_primary_product",
            sa.Boolean(),
            nullable=False,
            server_default="true",
            comment="True when this is the facility's primary output; false for by-products",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "facility_id",
            "material_id",
            name="uq_facility_material_link",
        ),
    )


def downgrade() -> None:
    op.drop_table("facility_material_links")
    op.drop_column("facilities", "name")
    op.drop_index("ix_facilities_mrds_dep_id", table_name="facilities")
    op.drop_column("facilities", "mrds_dep_id")
