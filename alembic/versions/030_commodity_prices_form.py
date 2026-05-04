"""Add hs_mapping_id and price_form to commodity_prices; widen unique constraint.

Revision ID: 030
Revises: 029
Create Date: 2026-05-02

Changes
-------
1. ``hs_mapping_id`` INTEGER NULL — FK to ``hs_code_material_mappings``.
   Scopes a price record to a specific supply chain stage node.  NULL for
   USGS annual averages and any record where the traded form is unknown.

2. ``price_form`` VARCHAR(128) NULL — free-text benchmark descriptor for
   display and filtering.  Examples: "LiOH·H2O 56.5% min",
   "spodumene 6% Li2O SC", "LME cobalt", "cobalt hydroxide 20.5% Co".
   Not a controlled vocabulary; used for UI filtering only.

3. Unique constraint ``uq_commodity_price`` is extended from
   (material_id, price_date, source) to
   (material_id, price_date, source, hs_mapping_id, price_form).

   PostgreSQL treats NULL as distinct for unique constraints, so historical
   rows with NULL in either new column do not conflict with each other —
   this is the correct behavior.

Index
-----
- ``idx_commodity_price_hs_mapping`` — partial index on hs_mapping_id WHERE
  NOT NULL; supports price lookups scoped to a specific stage node.

Rollback
--------
DROP INDEX idx_commodity_price_hs_mapping;
ALTER TABLE commodity_prices
    DROP CONSTRAINT uq_commodity_price;
ALTER TABLE commodity_prices
    ADD CONSTRAINT uq_commodity_price UNIQUE (material_id, price_date, source);
ALTER TABLE commodity_prices
    DROP COLUMN price_form,
    DROP COLUMN hs_mapping_id;
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "030"
down_revision = "029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "commodity_prices",
        sa.Column(
            "hs_mapping_id",
            sa.Integer,
            sa.ForeignKey(
                "hs_code_material_mappings.id",
                name="fk_commodity_price_hs_mapping",
                ondelete="SET NULL",
            ),
            nullable=True,
            comment=(
                "FK to hs_code_material_mappings. NULL for USGS annual averages "
                "and any price record where the specific traded form is not known. "
                "When set, scopes this price to a specific supply chain stage node."
            ),
        ),
    )
    op.add_column(
        "commodity_prices",
        sa.Column(
            "price_form",
            sa.String(128),
            nullable=True,
            comment=(
                "Free-text benchmark descriptor, e.g. 'LiOH·H2O 56.5% min', "
                "'spodumene 6% Li2O', 'LME cobalt', 'cobalt hydroxide 20.5% Co'. "
                "For display and filtering; not a controlled vocabulary."
            ),
        ),
    )

    # Widen the unique constraint to include the two new nullable columns
    op.drop_constraint("uq_commodity_price", "commodity_prices", type_="unique")
    op.create_unique_constraint(
        "uq_commodity_price",
        "commodity_prices",
        ["material_id", "price_date", "source", "hs_mapping_id", "price_form"],
    )

    # Partial index for stage-scoped price lookups
    op.create_index(
        "idx_commodity_price_hs_mapping",
        "commodity_prices",
        ["hs_mapping_id"],
        postgresql_where=sa.text("hs_mapping_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("idx_commodity_price_hs_mapping", table_name="commodity_prices")
    op.drop_constraint("uq_commodity_price", "commodity_prices", type_="unique")
    op.create_unique_constraint(
        "uq_commodity_price",
        "commodity_prices",
        ["material_id", "price_date", "source"],
    )
    op.drop_column("commodity_prices", "price_form")
    op.drop_column("commodity_prices", "hs_mapping_id")
