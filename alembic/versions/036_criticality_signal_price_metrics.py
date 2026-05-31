"""Two related changes shipped together because neither has been deployed yet.

1. Add price-trend columns to ``material_criticality_signals`` for the
   USGS MCS 2026 Fig 10 supplementary CSV (Price Growth Rates):
     - price_yoy_pct        — YoY price change as signed fraction
     - price_cagr_5yr_pct   — 5-year compound annual growth rate

2. Create ``material_source_aliases`` — a single reference table that
   translates the various names external sources use for a commodity
   (USGS MCS chapter heading, Fig 10 commodity row, USGS PDF chapter,
   T4 export-control commodity, etc.) to our canonical material name.
   Replaces the four ``_*_TO_MATERIAL`` Python dicts scattered across
   ingestion parsers.  Each row records (source_system, source_name,
   canonical_material_id) plus an explicit ``is_skipped`` flag for
   commodities we deliberately don't ingest (Iridium price rows,
   Asbestos, etc.) so the audit trail of "we considered this and chose
   not to" survives.

Revision ID: 036
Revises:     035
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "036"
down_revision: Union[str, None] = "035"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── 1. Price-trend columns on material_criticality_signals ────────────
    op.add_column(
        "material_criticality_signals",
        sa.Column(
            "price_yoy_pct",
            sa.Float(),
            nullable=True,
            comment=(
                "YoY price change as signed fraction (e.g. 1.44 = +144%). "
                "Sourced from USGS MCS Fig 10 Price Growth Rates. "
                "Positive = supply-side stress; negative = market easing."
            ),
        ),
    )
    op.add_column(
        "material_criticality_signals",
        sa.Column(
            "price_cagr_5yr_pct",
            sa.Float(),
            nullable=True,
            comment=(
                "5-year CAGR of price as signed fraction (e.g. 0.47 = +47% "
                "CAGR). Smoother trend signal than yoy."
            ),
        ),
    )

    # ── 2. material_source_aliases reference table ────────────────────────
    op.create_table(
        "material_source_aliases",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "source_system",
            sa.String(64),
            nullable=False,
            comment=(
                "Identifier for the external data source whose name we are "
                "translating. Examples: 'mcs_2026_csv', 'mcs_2025_csv', "
                "'mcs_pdf', 'fig10_prices', 't4_export_controls', "
                "'t6_end_use', 'comtrade'."
            ),
        ),
        sa.Column(
            "source_name",
            sa.String(255),
            nullable=False,
            comment=(
                "The raw commodity name as the source spells it. Stored "
                "verbatim (preserving case and punctuation) so partner "
                "review can compare against the source artifact directly. "
                "Lookups normalize via lower(btrim(source_name))."
            ),
        ),
        sa.Column(
            "canonical_material_id",
            sa.Integer(),
            sa.ForeignKey("materials.id", ondelete="CASCADE"),
            nullable=True,
            comment=(
                "FK to materials.id when the alias resolves to a tracked "
                "material. NULL when is_skipped=true."
            ),
        ),
        sa.Column(
            "is_skipped",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
            comment=(
                "Explicit decision flag: True means 'we considered this "
                "commodity and decided not to ingest it' (e.g. Iridium "
                "price, Asbestos, individual PGM prices that would average "
                "into a meaningless bundle). Distinct from a missing alias "
                "row which means 'we have not encountered this source name "
                "yet'. Resolver returns (None, True) for skipped vs "
                "(None, False) for unknown."
            ),
        ),
        sa.Column(
            "skip_reason",
            sa.Text(),
            nullable=True,
            comment=(
                "Free-text reason when is_skipped=True. Helps future "
                "engineers understand why a deliberate skip exists rather "
                "than treating it as oversight."
            ),
        ),
        sa.Column(
            "notes",
            sa.Text(),
            nullable=True,
            comment="Optional context for the alias row.",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(is_skipped = true AND canonical_material_id IS NULL) OR "
            "(is_skipped = false AND canonical_material_id IS NOT NULL)",
            name="ck_alias_skip_xor_material",
        ),
    )

    # Unique on (source_system, normalised source_name).  Matches the
    # resolver's lookup pattern (lower + trim).  Postgres expression
    # index keeps the UPSERT idempotent across whitespace / case quirks.
    op.execute(
        "CREATE UNIQUE INDEX uq_material_alias_source_name "
        "ON material_source_aliases (source_system, lower(btrim(source_name)))"
    )

    # Helper indexes for reverse lookup.
    op.create_index(
        "ix_material_alias_canonical",
        "material_source_aliases",
        ["canonical_material_id"],
    )
    op.create_index(
        "ix_material_alias_source_system",
        "material_source_aliases",
        ["source_system"],
    )


def downgrade() -> None:
    op.drop_index("ix_material_alias_source_system", "material_source_aliases")
    op.drop_index("ix_material_alias_canonical", "material_source_aliases")
    op.execute("DROP INDEX IF EXISTS uq_material_alias_source_name")
    op.drop_table("material_source_aliases")
    op.drop_column("material_criticality_signals", "price_cagr_5yr_pct")
    op.drop_column("material_criticality_signals", "price_yoy_pct")
