"""Drop hhi_score cache columns from hs_code_material_mappings.

Revision ID: 035
Revises: 034
Create Date: 2026-05-03

Why
---
``hs_code_material_mappings.hhi_score``, ``hhi_reference_year``, and
``hhi_source`` were added by migration 022 with the intent that the MCS PDF
parser would cache HHI per HS node and the runtime scorer would read it.

The runtime never adopted the cache. ``hs_node_scorer.py`` recomputes HHI
from ``hs_code_production_shares`` on every scoring run and writes the
result to ``hs_code_geography_risk_scores.hhi_at_stage`` (the per-node ×
country × date row).  A grep across ``app/`` confirms zero reads of any of
the three cache columns.  They are written-only.

Effect of dropping
------------------
None on scoring output.  ``hs_code_geography_risk_scores.hhi_at_stage`` —
the column the scorer actually produces and rollups consume — is unchanged
and continues to be computed from production shares per scoring run.

Companion changes (in the same PR)
----------------------------------
- ORM: removed from ``HsCodeMaterialMapping`` in ``app/models/supply.py``.
- Parser: removed the UPDATE block in
  ``app/services/ingestion/mcs_pdf_parser.py`` that previously wrote these
  columns after extracting production shares.

Rollback
--------
The downgrade re-adds the columns as nullable.  Any rows that existed
before the upgrade are not restored — they are not load-bearing, so this
is acceptable.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "035"
down_revision = "034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("hs_code_material_mappings") as batch:
        batch.drop_column("hhi_score")
        batch.drop_column("hhi_reference_year")
        batch.drop_column("hhi_source")


def downgrade() -> None:
    # Best-effort restore of the column definitions only.  Data is not
    # recoverable; pre-drop values are not retained.
    with op.batch_alter_table("hs_code_material_mappings") as batch:
        batch.add_column(
            sa.Column(
                "hhi_score",
                sa.Float(),
                nullable=True,
                comment="(deprecated) cached HHI; runtime computes from "
                        "hs_code_production_shares instead.",
            )
        )
        batch.add_column(
            sa.Column(
                "hhi_reference_year",
                sa.SmallInteger(),
                nullable=True,
                comment="(deprecated) reference year for hhi_score cache",
            )
        )
        batch.add_column(
            sa.Column(
                "hhi_source",
                sa.String(length=32),
                nullable=True,
                comment="(deprecated) usgs_mcs | comtrade | manual",
            )
        )
