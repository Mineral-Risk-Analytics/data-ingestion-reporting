"""022 — expand hs_code_material_mappings with stage, scope, and HHI columns.

Adds supply chain stage tracking, market scope, digit count, and an HHI cache
to the existing hs_code_material_mappings table.  Updates the unique constraint
to include market_scope so the same HS code prefix can coexist under 'global'
and 'us' scopes without collision.

Column additions:
  digit_count          — 4 | 6 | 8 | 10; all existing rows default to 4
  market_scope         — 'global' | 'us' | 'eu'; all existing rows default to 'global'
  supply_chain_stage   — nullable; backfilled by seed-hs-mappings --force
  stage_sequence       — nullable; 1=ore … 5=battery_grade; backfilled same way
  hhi_score            — cached Σ(share²) for this node; set by ingest pipeline
  hhi_reference_year   — year of the hhi_score cache
  hhi_source           — 'usgs_mcs' | 'comtrade' | 'manual'

Constraint changes:
  DROP uq_hs_material (hs_code_prefix, material_id)
  ADD  uq_hs_material_scope (hs_code_prefix, material_id, market_scope)
  ADD  idx_hs_material_scope on (material_id, market_scope, supply_chain_stage)

Revision ID: 022_hs_expand
Revises: 021_drop_legacy_scores
Create Date: 2026-05-02
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "022_hs_expand"
down_revision: Union[str, None] = "021_drop_legacy_scores"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- Add new columns -------------------------------------------------------
    # server_default handles backfill of existing rows atomically in Postgres 11+
    op.add_column(
        "hs_code_material_mappings",
        sa.Column(
            "digit_count",
            sa.SmallInteger(),
            nullable=False,
            server_default="4",
            comment="Number of significant digits in hs_code_prefix: 4 | 6 | 8 | 10",
        ),
    )
    op.add_column(
        "hs_code_material_mappings",
        sa.Column(
            "market_scope",
            sa.String(8),
            nullable=False,
            server_default="global",
            comment="global (WCO/Comtrade) | us (US HTS) | eu (EU CN)",
        ),
    )
    op.add_column(
        "hs_code_material_mappings",
        sa.Column(
            "supply_chain_stage",
            sa.String(16),
            nullable=True,
            comment=(
                "ore | concentrate | intermediate | refined | "
                "battery_grade | fabricated | scrap"
            ),
        ),
    )
    op.add_column(
        "hs_code_material_mappings",
        sa.Column(
            "stage_sequence",
            sa.SmallInteger(),
            nullable=True,
            comment="Ordering integer: 1=ore, 2=concentrate, 3=intermediate, "
                    "4=refined, 5=battery_grade, 6=fabricated, 7=scrap",
        ),
    )
    op.add_column(
        "hs_code_material_mappings",
        sa.Column(
            "hhi_score",
            sa.Float(),
            nullable=True,
            comment="Cached Σ(production_share²) for this HS node. Updated by ingest pipeline.",
        ),
    )
    op.add_column(
        "hs_code_material_mappings",
        sa.Column(
            "hhi_reference_year",
            sa.SmallInteger(),
            nullable=True,
            comment="Reference year for hhi_score cache",
        ),
    )
    op.add_column(
        "hs_code_material_mappings",
        sa.Column(
            "hhi_source",
            sa.String(32),
            nullable=True,
            comment="Data source for hhi_score: usgs_mcs | comtrade | manual",
        ),
    )

    # --- Check constraints -----------------------------------------------------
    op.create_check_constraint(
        "ck_hs_digit_count",
        "hs_code_material_mappings",
        "digit_count IN (4, 6, 8, 10)",
    )
    op.create_check_constraint(
        "ck_hs_market_scope",
        "hs_code_material_mappings",
        "market_scope IN ('global', 'us', 'eu')",
    )
    op.create_check_constraint(
        "ck_hs_supply_chain_stage",
        "hs_code_material_mappings",
        "supply_chain_stage IN ("
        "'ore', 'concentrate', 'intermediate', 'refined', "
        "'battery_grade', 'fabricated', 'scrap')",
    )

    # --- Unique constraint update ----------------------------------------------
    # Drop the old 2-column constraint; add new 3-column one.
    # All existing rows have market_scope='global' so no uniqueness violations.
    op.drop_constraint("uq_hs_material", "hs_code_material_mappings", type_="unique")
    op.create_unique_constraint(
        "uq_hs_material_scope",
        "hs_code_material_mappings",
        ["hs_code_prefix", "material_id", "market_scope"],
    )

    # --- Covering index for common query pattern -------------------------------
    op.create_index(
        "idx_hs_material_scope",
        "hs_code_material_mappings",
        ["material_id", "market_scope", "supply_chain_stage"],
    )


def downgrade() -> None:
    op.drop_index("idx_hs_material_scope", table_name="hs_code_material_mappings")
    op.drop_constraint("uq_hs_material_scope", "hs_code_material_mappings", type_="unique")
    op.create_unique_constraint(
        "uq_hs_material", "hs_code_material_mappings", ["hs_code_prefix", "material_id"]
    )
    op.drop_constraint("ck_hs_supply_chain_stage", "hs_code_material_mappings", type_="check")
    op.drop_constraint("ck_hs_market_scope", "hs_code_material_mappings", type_="check")
    op.drop_constraint("ck_hs_digit_count", "hs_code_material_mappings", type_="check")
    op.drop_column("hs_code_material_mappings", "hhi_source")
    op.drop_column("hs_code_material_mappings", "hhi_reference_year")
    op.drop_column("hs_code_material_mappings", "hhi_score")
    op.drop_column("hs_code_material_mappings", "stage_sequence")
    op.drop_column("hs_code_material_mappings", "supply_chain_stage")
    op.drop_column("hs_code_material_mappings", "market_scope")
    op.drop_column("hs_code_material_mappings", "digit_count")
