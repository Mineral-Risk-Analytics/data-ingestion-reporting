"""Add ``regulation_aliases`` reference table.

Mirrors ``material_source_aliases`` (migration 036) for regulations.  Maps
external identifiers (CELEX numbers from EUR-Lex, Federal Register document
numbers, Public Law / US Code citations, etc.) to canonical regulation rows
keyed by ``regulation_key`` (e.g. ``EU_BATTERY_REG_2023``, ``UFLPA``).

Why a separate table instead of a JSONB column on ``regulations``
-----------------------------------------------------------------
Same reasoning as ``material_source_aliases``:

* Distinct rows for distinct external IDs are queryable / indexable.
* Partner-review workflows can mark an external ID ``is_skipped=true``
  so the audit trail of "we considered this CELEX / FR doc and decided
  it's not a tracked regulation" survives instead of being silent.
* Adding new source systems (FR, US Code, OFAC SDN, GTA case ID, etc.)
  is data-only; no further schema changes.

Lookup convention
-----------------
``(source_system, lower(btrim(source_key)))`` is unique.  The Postgres
expression index handles whitespace / case normalisation so a CELEX
written as ``" 32023R1542 "`` resolves the same as ``"32023r1542"``.
The resolver helper applies the same normalisation in Python.

Backfill
--------
None.  ``regulations`` was cleared and re-seeded; no orphan external
IDs exist.  Future re-seeds populate via ``seed_regulation_aliases``.

Revision ID: 039
Revises:     038
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "039"
down_revision: Union[str, None] = "038"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "regulation_aliases",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "source_system",
            sa.String(64),
            nullable=False,
            comment=(
                "Identifier for the external data source whose ID we are "
                "translating. Examples: 'eurlex_celex', 'federal_register', "
                "'us_code', 'public_law', 'ofac_sdn', 'gta_case'."
            ),
        ),
        sa.Column(
            "source_key",
            sa.String(255),
            nullable=False,
            comment=(
                "The raw external identifier as the source publishes it. "
                "Stored verbatim (preserving case / punctuation) so partner "
                "review can compare against the source artifact directly. "
                "Lookups normalize via lower(btrim(source_key))."
            ),
        ),
        sa.Column(
            "regulation_id",
            sa.Integer(),
            sa.ForeignKey("regulations.id", ondelete="CASCADE"),
            nullable=True,
            comment=(
                "FK to regulations.id when the alias resolves to a tracked "
                "regulation. NULL when is_skipped=true."
            ),
        ),
        sa.Column(
            "is_skipped",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
            comment=(
                "Explicit decision flag: True means 'we considered this "
                "external ID and decided it does not map to a tracked "
                "regulation' (e.g. an EUR-Lex CELEX for an unrelated "
                "agricultural directive that surfaced in a co-published "
                "bundle, or a Federal Register notice that's not a "
                "regulation we score). Distinct from a missing alias row "
                "which means 'we have not encountered this source key yet'. "
                "Resolver returns 'skipped' vs 'unknown' accordingly."
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
            "(is_skipped = true AND regulation_id IS NULL) OR "
            "(is_skipped = false AND regulation_id IS NOT NULL)",
            name="ck_regulation_alias_skip_xor_id",
        ),
    )

    # Unique on (source_system, normalised source_key).  Matches the
    # resolver's lookup pattern (lower + trim).  PG expression index keeps
    # the UPSERT idempotent across whitespace / case quirks.
    op.execute(
        "CREATE UNIQUE INDEX uq_regulation_alias_source_key "
        "ON regulation_aliases (source_system, lower(btrim(source_key)))"
    )

    # Helper indexes for reverse lookup ("which CELEX numbers map to this
    # regulation?") and partition lookups by source.
    op.create_index(
        "ix_regulation_alias_regulation",
        "regulation_aliases",
        ["regulation_id"],
    )
    op.create_index(
        "ix_regulation_alias_source_system",
        "regulation_aliases",
        ["source_system"],
    )


def downgrade() -> None:
    op.drop_index("ix_regulation_alias_source_system", "regulation_aliases")
    op.drop_index("ix_regulation_alias_regulation", "regulation_aliases")
    op.execute("DROP INDEX IF EXISTS uq_regulation_alias_source_key")
    op.drop_table("regulation_aliases")
