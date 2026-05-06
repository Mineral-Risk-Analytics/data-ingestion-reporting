"""Add writes_material_signals flag to material_source_aliases.

Some external chapters share a canonical material with a sibling
chapter (e.g. MCS 2026's BAUXITE AND ALUMINA chapter and ALUMINUM
chapter both map to canonical "Aluminum" — they describe different
supply-chain stages of the same material).  When that happens, both
chapters should resolve to the same Material row, but only ONE should
drive the material-level signals (criticality_score, HHI,
production_yoy_pct, etc.) that get written to
material_criticality_signals.  Otherwise the second-loaded chapter
silently overwrites the first.

This column flags the "non-primary" chapters.  Default true preserves
existing single-chapter aliases.  When false, the CLI:

  - still resolves the alias to the same canonical Material
  - still writes per-HS-prefix shares to hs_code_production_shares
    (because those route to distinct HS prefixes — bauxite to 2606,
    aluminum to 7601, no upsert collision)
  - SKIPS material_criticality_signals and material_production_shares
    upserts (those keys collide with the primary chapter)

The PRIMARY chapter (writes_material_signals=true) writes the
material-level signal that downstream scoring consumes.  This keeps
"Aluminum criticality_score" reflecting the refined-metal endpoint
risk that battery makers actually buy, while still capturing the
upstream bauxite ore concentration at the HS-prefix level.

Revision ID: 037
Revises:     036
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "037"
down_revision: Union[str, None] = "036"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "material_source_aliases",
        sa.Column(
            "writes_material_signals",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
            comment=(
                "When true (default), the CLI writes both material-level "
                "signals (criticality_signals, production_shares) AND "
                "per-HS-prefix shares for this alias.  When false, only "
                "per-HS-prefix shares are written — used for upstream-stage "
                "chapters that share a canonical with a sibling chapter "
                "(e.g. BAUXITE AND ALUMINA → Aluminum, where ALUMINUM is "
                "the primary chapter).  Avoids upsert collisions on "
                "(material_id, source, reference_year) keys."
            ),
        ),
    )


def downgrade() -> None:
    op.drop_column("material_source_aliases", "writes_material_signals")
