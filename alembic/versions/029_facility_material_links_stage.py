"""Add supply_chain_stage and hs_mapping_id to facility_material_links.

Revision ID: 029
Revises: 028_hs_event_attribution
Create Date: 2026-05-02

Changes
-------
1. ``supply_chain_stage`` VARCHAR(16) NULL — constrained to the seven-value
   stage enum (ore, concentrate, intermediate, refined, battery_grade,
   fabricated, scrap).  Populated by the MRDS/GEM ingester from facility_type
   at ingest time.  NULL for all pre-existing rows.

2. ``hs_mapping_id`` INTEGER NULL — FK to ``hs_code_material_mappings``.
   When set, directly links a facility's capacity to a specific HS stage node
   for use by Phase 3 ``hs_node_scorer.py``.  NULL until manually confirmed
   for any row where GEM does not supply enough detail to resolve a specific
   HS code.

Indexes
-------
- ``idx_fml_hs_mapping`` — partial index on hs_mapping_id WHERE NOT NULL;
  used by Phase 3 node scorer when joining facility capacity to HS nodes.
- ``idx_fml_stage`` — partial index on (material_id, supply_chain_stage)
  WHERE stage IS NOT NULL; used for stage-specific capacity rollups.

Both columns are nullable so the pre-existing unique constraint
``uq_facility_material_link`` on (facility_id, material_id) is unaffected.

Rollback
--------
DROP INDEX idx_fml_stage;
DROP INDEX idx_fml_hs_mapping;
ALTER TABLE facility_material_links
    DROP COLUMN hs_mapping_id,
    DROP COLUMN supply_chain_stage;
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "029"
down_revision = "028_hs_event_attribution"
branch_labels = None
depends_on = None

_VALID_STAGES = (
    "ore",
    "concentrate",
    "intermediate",
    "refined",
    "battery_grade",
    "fabricated",
    "scrap",
)


def upgrade() -> None:
    op.add_column(
        "facility_material_links",
        sa.Column(
            "supply_chain_stage",
            sa.String(16),
            nullable=True,
            comment=(
                "ore | concentrate | intermediate | refined | battery_grade | "
                "fabricated | scrap.  NULL for rows predating migration 029. "
                "Stage determines which hs_code_geography_risk_scores node this "
                "facility's capacity contributes to in the operational scoring pillar."
            ),
        ),
    )
    op.add_column(
        "facility_material_links",
        sa.Column(
            "hs_mapping_id",
            sa.Integer,
            sa.ForeignKey(
                "hs_code_material_mappings.id",
                name="fk_fml_hs_mapping",
                ondelete="SET NULL",
            ),
            nullable=True,
            comment=(
                "FK to hs_code_material_mappings. NULL for historical rows. "
                "When set, links this facility's capacity directly to a stage node "
                "for stage-weighted structural_dependency calculation."
            ),
        ),
    )

    # CHECK constraint on supply_chain_stage
    op.create_check_constraint(
        "ck_fml_supply_chain_stage",
        "facility_material_links",
        "supply_chain_stage IN ('ore','concentrate','intermediate','refined',"
        "'battery_grade','fabricated','scrap')",
    )

    # Partial index: hs_mapping joins in Phase 3
    op.create_index(
        "idx_fml_hs_mapping",
        "facility_material_links",
        ["hs_mapping_id"],
        postgresql_where=sa.text("hs_mapping_id IS NOT NULL"),
    )

    # Partial index: stage-specific capacity rollups
    op.create_index(
        "idx_fml_stage",
        "facility_material_links",
        ["material_id", "supply_chain_stage"],
        postgresql_where=sa.text("supply_chain_stage IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("idx_fml_stage", table_name="facility_material_links")
    op.drop_index("idx_fml_hs_mapping", table_name="facility_material_links")
    op.drop_constraint(
        "ck_fml_supply_chain_stage", "facility_material_links", type_="check"
    )
    op.drop_column("facility_material_links", "hs_mapping_id")
    op.drop_column("facility_material_links", "supply_chain_stage")
