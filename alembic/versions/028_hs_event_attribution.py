"""028 — risk_event_hs_mappings junction + keywords column on hs_code_material_mappings.

Phase 1.5 schema changes (applied as 028 because 027 was already deployed when
this work was completed — see notes below).

Deployment context
------------------
The content of this migration was originally designed as 026, to be inserted
between 025 and 027 in the chain.  However, migration 027 had already been
applied to the database before this work was ready, so 026 was never executed.
This migration contains identical schema operations to what 026 described, but
with down_revision = 027 so that existing databases at revision 027 will pick it
up correctly on the next ``alembic upgrade head`` run.

Fresh databases that never applied the old 026 file (which has been removed from
the codebase) will follow the chain 025 → 027 → 028 and end up in the same final
state as if 026 had existed between 025 and 027.

Schema changes
--------------
1. Adds ``keywords`` (JSONB, nullable) to ``hs_code_material_mappings``.
   Replaces the hard-coded ``_MATERIAL_ALIASES`` dict in
   ``ingest_federal_register.py``.  Each row holds an array of lower-cased
   keyword strings that the Federal Register ingester uses to detect a
   material mention in document text.  Example:
       ["cobalt", "cobalt sulfate", "co sulfate"]

   Managed via ``seed_hs_mappings.py`` (or direct SQL for corrections).
   NULL = use the material canonical name only as the keyword.

2. Creates ``risk_event_hs_mappings`` — stage-level junction table that
   links a risk event to the specific HS mapping node (hs_code + stage)
   rather than just the material.  Complement to ``risk_event_materials``:
   both rows are written when an event has HS attribution; only
   ``risk_event_materials`` is written for events without HS attribution
   (pre-redesign historical data and keyword-only matches).

   Unique constraint is on (risk_event_id, hs_mapping_id) — one row per
   (event, hs_node) pair.  A single event may link to multiple hs_mapping
   rows if multiple stages are detected.

   relevance_score mirrors the convention on risk_event_materials:
     1.00 — direct HS code match
     0.85 — keyword match with stage disambiguation

Revision ID: 028_hs_event_attribution
Revises: 027_trade_flows_hs_mapping
Create Date: 2026-05-02
"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from alembic import op

revision: str = "028_hs_event_attribution"
down_revision: Union[str, None] = "027_trade_flows_hs_mapping"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- keywords column on hs_code_material_mappings -----------------------
    op.add_column(
        "hs_code_material_mappings",
        sa.Column(
            "keywords",
            JSONB,
            nullable=True,
            comment=(
                "JSON array of lower-cased keyword strings used by the Federal "
                "Register ingester for material detection.  Replaces the hard-coded "
                "_MATERIAL_ALIASES dict.  NULL = fall back to canonical material name."
            ),
        ),
    )

    # --- risk_event_hs_mappings junction table ------------------------------
    op.create_table(
        "risk_event_hs_mappings",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "risk_event_id",
            sa.Integer(),
            sa.ForeignKey("risk_events.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "hs_mapping_id",
            sa.Integer(),
            sa.ForeignKey("hs_code_material_mappings.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "relevance_score",
            sa.Float(),
            nullable=False,
            server_default="1.0",
            comment=(
                "Confidence that this event affects this specific HS stage node.  "
                "1.00 = direct HS code match; 0.85 = keyword match with stage "
                "disambiguation.  Mirrors the convention on risk_event_materials."
            ),
        ),
        sa.Column(
            "match_reason",
            sa.String(64),
            nullable=True,
            comment="hs_code_match | keyword_match | trade_flow_match",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "risk_event_id", "hs_mapping_id", name="uq_risk_event_hs_mapping"
        ),
    )


def downgrade() -> None:
    op.drop_table("risk_event_hs_mappings")
    op.drop_column("hs_code_material_mappings", "keywords")
