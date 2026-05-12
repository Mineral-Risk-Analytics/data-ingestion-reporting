"""Add ``risk_events.updated_at`` for parameter-stable UPSERT semantics.

Background
----------
A handful of risk-event categories are conceptually states rather than
point-in-time occurrences.  TRADE_CONCENTRATION says "country X is
currently 67% of material Y's exports for year 2024."  OpenSanctions
geo events say "Russia has 9,732 sanctioned entities right now."
Their current ``content_hash`` is computed from titles that encode
those mutable values (the percentage, the count), so re-running with
slightly different inputs creates a *new* row alongside the prior one
instead of updating it in place.

The 2026-05-11 ticket switches these ingesters to parameter-stable
``content_hash`` derived from the conceptual identity of the signal
(``{event_subtype}|{material_id}|{country}|{year}`` for trade signals;
``opensanctions_company|{company_id}`` / ``opensanctions_geo|{country}``
for sanctions).  When a row already exists by stable hash, the
ingester UPDATEs title / severity / summary / metadata in place instead
of inserting a duplicate.

Without an ``updated_at`` column, those in-place updates are silent —
there's no audit trail of when a row was last touched.  This migration
adds the column with ``server_default=NOW()`` so future writes
auto-populate it, and backfills existing rows to ``created_at`` so
queries like "rows updated since X" don't break for pre-migration data.

Why no ``onupdate`` at the DB level
-----------------------------------
Postgres doesn't have native ``ON UPDATE`` triggers for a timestamp the
way MySQL does.  Adding a trigger here would require manual creation
and is overkill for a single column on one table.  The Python ORM
sets ``onupdate=func.now()`` on the model column instead, which means
every ORM-mediated UPDATE refreshes the timestamp.  Raw SQL UPDATEs
that bypass the ORM (rare for this table) will not refresh it.

Revision ID: 041
Revises:     040
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "041"
down_revision: Union[str, None] = "040"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "risk_events",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
            comment=(
                "Last time this row was written or updated. "
                "Set to created_at on insert, refreshed by the ORM on "
                "every UPDATE via onupdate=func.now() on the model column. "
                "Use to query for recently-changed rows after switching "
                "TRADE_CONCENTRATION / OpenSanctions geo + company events "
                "to parameter-stable content_hash + UPSERT semantics "
                "(2026-05-11)."
            ),
        ),
    )

    # Backfill existing rows so updated_at = created_at.  Without this,
    # the server_default=NOW() above would stamp every existing row
    # with the migration timestamp, which would look like everything was
    # just modified.
    op.execute(
        """
        UPDATE risk_events
        SET updated_at = created_at
        WHERE updated_at > created_at
        """
    )


def downgrade() -> None:
    op.drop_column("risk_events", "updated_at")
