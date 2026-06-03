"""Add ``material_capacity_shares`` reference table.

Background
----------
USGS Mineral Commodity Summaries 2026 publishes 220 ``Statistics='Capacity'``
rows across 8 tracked chapters (ALUMINUM, BISMUTH, GALLIUM, INDIUM,
MAGNESIUM METAL, SELENIUM, TELLURIUM, TITANIUM AND TITANIUM DIOXIDE).
Pre-2026-05-31 the ``mcs2026_parser`` silently dropped these on the
floor — its main extractor (``_extract_world_production_per_country``)
classified rows as production OR reserves, and Capacity fell through.

Why this matters for scoring
----------------------------
Capacity = theoretical maximum a facility could produce; production =
actual tonnes delivered last year.  The ratio enables:

  * Spare-capacity signal — where could supply scale up if demand spikes?
  * HCG capacity overhang — what fraction of global *capacity* sits in
    China / DRC / Russia (vs. production share today)?
  * Per-country utilization — capacity-share minus production-share
    surfaces underused producers.

Pure production HHI can't express any of these.  This migration adds
the storage layer so the new ``capacity_shares`` field on the
``parse_mcs2026_csv`` output has somewhere to land.

Design choice — distinct table, not extension of MaterialProductionShare
------------------------------------------------------------------------
Capacity rows in MCS:

  * Live in their own Statistics='Capacity' rows, distinct from
    Statistics='Production' rows — not always a 1:1 country pairing.
  * Can have multiple ``Statistics_detail`` values per chapter that
    track different products (TITANIUM publishes "Titanium sponge metal
    Capacity" AND "TiO2 Pigment Capacity" — distinct capacity streams
    for the same canonical material).

A sibling table with a ``detail_type`` discriminator preserves the per-
type breakdown without polluting the production-share queries that
existing scoring code uses.  The unique constraint includes
``detail_type`` so TITANIUM's two capacity streams remain separable.

Revision ID: 045
Revises:     044
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "045"
down_revision: Union[str, None] = "044"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "material_capacity_shares",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "material_id",
            sa.Integer(),
            sa.ForeignKey("materials.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "country_code",
            sa.String(2),
            nullable=False,
            index=True,
            comment="ISO-3166-1 alpha-2 country code.",
        ),
        sa.Column(
            "reference_year",
            sa.Integer(),
            nullable=False,
            comment="Year the capacity figure applies to (e.g. 2025).",
        ),
        sa.Column(
            "detail_type",
            sa.String(128),
            nullable=False,
            comment=(
                "Verbatim Statistics_detail string from MCS, e.g. "
                "'Smelter capacity', 'Refinery capacity', 'Titanium "
                "sponge metal Capacity', 'TiO2 Pigment Capacity'. "
                "Preserves the breakdown for chapters with multiple "
                "capacity streams under one canonical material."
            ),
        ),
        sa.Column(
            "capacity_volume",
            sa.Float(),
            nullable=True,
            comment="Raw capacity volume from MCS (unit in unit_of_measure).",
        ),
        sa.Column(
            "capacity_share",
            sa.Float(),
            nullable=False,
            comment=(
                "Fraction of world capacity within this (material, year, "
                "detail_type) bucket (0.0–1.0).  Computed per bucket "
                "rather than across all detail_types because TITANIUM-style "
                "multi-stream chapters would otherwise produce shares > 1.0."
            ),
        ),
        sa.Column(
            "unit_of_measure",
            sa.String(64),
            nullable=True,
            comment="Unit from MCS Unit column, e.g. 'metric tons', 'kilograms'.",
        ),
        sa.Column(
            "data_source",
            sa.String(64),
            nullable=False,
            server_default="usgs_mcs",
            comment="Source system identifier (always 'usgs_mcs' for now).",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            onupdate=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "material_id",
            "country_code",
            "reference_year",
            "detail_type",
            name="uq_material_capacity_share",
        ),
    )


def downgrade() -> None:
    op.drop_table("material_capacity_shares")
