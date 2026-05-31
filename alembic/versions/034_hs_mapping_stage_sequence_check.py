"""Add CHECK constraint enforcing supply_chain_stage / stage_sequence alignment.

Revision ID: 034
Revises: 031
Create Date: 2026-05-02

Problem
-------
``hs_code_material_mappings`` has both ``supply_chain_stage`` (varchar enum)
and ``stage_sequence`` (integer 1–6).  Nothing previously prevented a row
from having ``supply_chain_stage='refined'`` with ``stage_sequence=2``
(intermediate), which would silently produce wrong rollup weights in
``market_aggregator.STAGE_ROLLUP_WEIGHTS``.

Fix
---
A single CHECK constraint enforces the bijection.  Both columns must either
be NULL together (legacy rows predating migration 022) or carry a consistent
pair.  ``fabricated`` (6) and ``scrap`` (7) are included for completeness
even though ``STAGE_ROLLUP_WEIGHTS`` does not currently weight them.

Rollback
--------
ALTER TABLE hs_code_material_mappings
    DROP CONSTRAINT ck_hsmapping_stage_sequence;
"""

from __future__ import annotations

from alembic import op

revision = "034"
down_revision = "031"
branch_labels = None
depends_on = None

# Bijection: (supply_chain_stage, stage_sequence)
_CONSTRAINT_EXPR = """
    (supply_chain_stage IS NULL AND stage_sequence IS NULL)
    OR (supply_chain_stage = 'ore'           AND stage_sequence = 1)
    OR (supply_chain_stage = 'concentrate'   AND stage_sequence = 2)
    OR (supply_chain_stage = 'intermediate'  AND stage_sequence = 3)
    OR (supply_chain_stage = 'refined'       AND stage_sequence = 4)
    OR (supply_chain_stage = 'battery_grade' AND stage_sequence = 5)
    OR (supply_chain_stage = 'fabricated'    AND stage_sequence = 6)
    OR (supply_chain_stage = 'scrap'         AND stage_sequence = 7)
""".strip()


def upgrade() -> None:
    op.create_check_constraint(
        "ck_hsmapping_stage_sequence",
        "hs_code_material_mappings",
        _CONSTRAINT_EXPR,
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_hsmapping_stage_sequence",
        "hs_code_material_mappings",
        type_="check",
    )
