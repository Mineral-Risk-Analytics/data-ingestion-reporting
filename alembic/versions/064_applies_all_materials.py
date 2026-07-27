"""Add regulations.applies_all_materials — explicit all-goods gate.

Revision ID: 064
Revises: 063

Material-agnostic regulations (UFLPA, EU FLR, CSDDD, S-211 — all-goods /
all-sector rules) previously entered material x geography scoring through
the RegulationGeographyScope OR-gate, which also leaked material-SCOPED
regulations (CRMA) into unlisted materials at their targeted countries.
This flag makes the distinction explicit: the scoring gate is now
"material in MaterialScopes OR applies_all_materials"; geography scope
rows are descriptive metadata only (market path; company path unchanged
until its V1 migration). Decided 2026-07-23 (Nicole).
"""

from alembic import op
import sqlalchemy as sa

revision = "064"
down_revision = "063"
branch_labels = None
depends_on = None

# All-goods rules in the curated set at migration time. FLR / S-211 arrive
# via the regulation workbook already carrying the flag.
_ALL_MATERIALS_KEYS = ("UFLPA", "EU_CSDDD")


def upgrade() -> None:
    op.add_column(
        "regulations",
        sa.Column(
            "applies_all_materials", sa.Boolean(), nullable=False,
            server_default=sa.text("false"),
            comment=(
                "All-goods/all-sector rule: enters scoring for every material "
                "(gate), instead of via regulation_material_scope rows."
            ),
        ),
    )
    keys = ", ".join("'{}'".format(k) for k in _ALL_MATERIALS_KEYS)
    op.execute(
        "UPDATE regulations SET applies_all_materials = true "
        "WHERE regulation_key IN ({})".format(keys)
    )


def downgrade() -> None:
    op.drop_column("regulations", "applies_all_materials")
