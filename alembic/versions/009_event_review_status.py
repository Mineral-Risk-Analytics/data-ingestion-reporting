"""Add review_status and review_note to risk_event_companies.

Revision ID: 009_event_review_status
Revises: 008_verified_flags
Create Date: 2026-04-22

review_status drives the analyst triage workflow:
  pending   — not yet reviewed; counts toward score (default for all existing rows)
  confirmed — reviewed and validated as relevant; counts toward score
  excluded  — reviewed and rejected; does NOT count toward score

The scoring engine filters WHERE review_status != 'excluded' so existing
un-triaged events continue contributing until explicitly excluded.

review_note is an optional free-text field the analyst can fill in when
excluding or confirming an event (e.g. "indirect geography only, not a
named-company match").
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "009_event_review_status"
down_revision = "008_verified_flags"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "risk_event_companies",
        sa.Column(
            "review_status",
            sa.String(16),
            nullable=False,
            server_default="pending",
        ),
    )
    op.add_column(
        "risk_event_companies",
        sa.Column("review_note", sa.Text(), nullable=True),
    )
    # Index for the common scoring filter (WHERE review_status != 'excluded')
    op.create_index(
        "ix_risk_event_companies_review_status",
        "risk_event_companies",
        ["review_status"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_risk_event_companies_review_status",
        table_name="risk_event_companies",
    )
    op.drop_column("risk_event_companies", "review_note")
    op.drop_column("risk_event_companies", "review_status")
