"""Add volume_share_pct to company_supply_relationships and fix NULL
material_id uniqueness gap.

volume_share_pct: fraction of the buyer's demand for this material/cell
supplied by this specific supplier (0.0–1.0, nullable). NULL means the
relationship is confirmed but share is unknown.

The existing uq_supply_relationship constraint is UNIQUE(buyer_id,
supplier_id, material_id). PostgreSQL treats NULL != NULL, so multiple
rows with material_id IS NULL for the same buyer/supplier pair are not
prevented. This migration adds a partial unique index to close that gap.

# Apply via direct connection (DDL requires non-pooled URL):
# alembic -x direct=true upgrade head
#
# Or via hatch:
# hatch run migrate
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "004_supply_relationship_volume"
down_revision: Union[str, None] = "003_hs_mappings"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Add volume_share_pct column
    op.add_column(
        "company_supply_relationships",
        sa.Column("volume_share_pct", sa.Float(), nullable=True),
    )

    # 2. Add CHECK constraint: value must be 0.0–1.0 when not NULL
    op.create_check_constraint(
        "ck_supply_rel_volume_share_range",
        "company_supply_relationships",
        "volume_share_pct IS NULL OR (volume_share_pct >= 0.0 AND volume_share_pct <= 1.0)",
    )

    # 3. Partial unique index: prevent duplicate (buyer, supplier) rows
    #    where material_id IS NULL
    op.create_index(
        "uq_supply_rel_no_material",
        "company_supply_relationships",
        ["buyer_id", "supplier_id"],
        unique=True,
        postgresql_where=sa.text("material_id IS NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_supply_rel_no_material",
        table_name="company_supply_relationships",
    )
    op.drop_constraint(
        "ck_supply_rel_volume_share_range",
        "company_supply_relationships",
    )
    op.drop_column("company_supply_relationships", "volume_share_pct")
