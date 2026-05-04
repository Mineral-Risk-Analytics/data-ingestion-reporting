"""027 — add hs_mapping_id FK to trade_flows.

Stores the resolved hs_code_material_mappings row ID on each TradeFlow at
Comtrade ingest time.  This is the fix for the stage attribution gap: the
Comtrade ingest already resolves hs_code → material via hs_code_material_mappings,
but previously discarded the mapping ID after writing material_id.  Without it,
trade_signal_builder.py can only group flows by material_id and loses all stage
specificity.

With this column populated, trade_signal_builder.py can:
  1. Group by (material_id, hs_mapping_id, reporter_country, period)
  2. Populate risk_event_hs_mappings (stage-level junction) when creating
     trade signal risk events, in addition to risk_event_materials

Existing rows: hs_mapping_id is NULL for all historical trade flows.
This is intentional and acceptable — historical rows produce only
risk_event_materials links (existing behavior, no regression).
New Comtrade ingest runs will populate both material_id and hs_mapping_id.

A single backfill query can retroactively populate hs_mapping_id for historical
rows without re-hitting the Comtrade API:

    UPDATE trade_flows tf
    SET    hs_mapping_id = m.id,
           material_id   = COALESCE(tf.material_id, m.material_id)
    FROM   hs_code_material_mappings m
    WHERE  m.hs_code_prefix  = tf.hs_code
      AND  m.market_scope     = 'global'
      AND  tf.hs_mapping_id  IS NULL;

Run this after migration 022 has been applied and seed-hs-mappings --force
has been run to populate 6-digit mappings.

Note: migration 026 (risk_event_hs_mappings and keywords column) is Phase 1.5
and intentionally interleaved here as 027 to keep trade_flow changes with the
Phase 1 schema batch.

Revision ID: 027_trade_flows_hs_mapping
Revises: 025_material_geo_rollup_annotations
Create Date: 2026-05-02
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "027_trade_flows_hs_mapping"
down_revision: Union[str, None] = "025_geo_rollup_annotations"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "trade_flows",
        sa.Column(
            "hs_mapping_id",
            sa.Integer(),
            sa.ForeignKey("hs_code_material_mappings.id", ondelete="SET NULL"),
            nullable=True,
            comment=(
                "FK to hs_code_material_mappings.  Set at Comtrade ingest time "
                "alongside material_id.  NULL for historical rows ingested before "
                "this migration.  Required for stage-level risk event attribution "
                "in trade_signal_builder.py."
            ),
        ),
    )
    op.create_index(
        "idx_trade_flows_hs_mapping",
        "trade_flows",
        ["hs_mapping_id"],
        postgresql_where=sa.text("hs_mapping_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("idx_trade_flows_hs_mapping", table_name="trade_flows")
    op.drop_column("trade_flows", "hs_mapping_id")
