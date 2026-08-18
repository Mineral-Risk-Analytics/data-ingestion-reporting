"""risk_events.duplicate_of_id — cross-source duplicate suppression.

Nicole 2026-07-15: the same real-world event arrives from multiple
ingesters and gets counted multiple times.  Live example: the Feb-2025
DRC cobalt export ban exists THREE times — IEA (event_date 2025-01-01,
severity 0.15, subtype NULL), manual walkthrough (2025-02-22, 0.75,
national_export_ban), GTA (2025-09-21, 0.90, EXPORT_RESTRICTION).
``content_hash`` only dedupes same-source re-ingests; nothing catches
cross-source duplicates, so recency-weighted severity averages and the
partner-facing geo_specific_event_count are distorted.

Mechanism (curation-first, per Nicole's "fewer, confident, significant
events" direction):

* ``duplicate_of_id`` — nullable self-FK.  NULL = canonical (the
  overwhelming default).  Non-NULL = this row is a confirmed duplicate
  of the referenced canonical event and is EXCLUDED from scoring and
  event counts.  The canonical row is the curated one — by convention
  the manual-walkthrough row wins when present (human-audited severity
  and dating), else the higher-confidence ingester.
* Rows are flagged ONLY by the human-confirmed apply flow
  (``mark-duplicate-events`` CLI reading the reviewed similarity
  report).  No automatic flagging at ingest.
* ondelete SET NULL: deleting a canonical row un-flags its duplicates
  rather than cascading — the duplicates become visible again for
  re-curation instead of silently vanishing.

Revision ID: 055
Revises: 054
"""

from alembic import op
import sqlalchemy as sa

revision = "055"
down_revision = "054"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "risk_events",
        sa.Column(
            "duplicate_of_id",
            sa.Integer(),
            sa.ForeignKey("risk_events.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_risk_events_duplicate_of_id",
        "risk_events",
        ["duplicate_of_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_risk_events_duplicate_of_id", table_name="risk_events")
    op.drop_column("risk_events", "duplicate_of_id")
