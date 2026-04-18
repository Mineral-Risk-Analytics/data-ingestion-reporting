"""Seed staleness review tables.

Creates two polymorphic tables that back the seed staleness review feature:

- ``seed_review_runs``: one row per invocation of the reviewer. Holds run-level
  parameters (``seed_types`` array, ``since_date``, ``parameters_json``) and
  summary counters set on completion.
- ``seed_review_findings``: one row per candidate the reviewer flagged. The
  subject is polymorphic on ``seed_type``; its typed identifier lives in
  ``seed_identifier_json`` (GIN-indexed) and its human-readable form in
  ``seed_key``. The evidence is a nullable FK to ``source_documents`` and/or
  ``risk_events`` (covers FR documents, risk-event junctions, and free-text
  mentions in SEC/news filings). Triage state is captured by ``status``,
  ``reviewed_at``, ``reviewed_by``, ``reviewer_note``.

Apply via:
  alembic -x direct=true upgrade head
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "005_seed_review_tables"
down_revision: Union[str, None] = "004_supply_relationship_volume"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "seed_review_runs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "seed_types",
            postgresql.JSONB(),
            nullable=False,
        ),
        sa.Column("since_date", sa.Date(), nullable=True),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "total_seeds_checked",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "total_findings",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("parameters_json", postgresql.JSONB(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", name="uq_seed_review_run_id"),
    )

    op.create_table(
        "seed_review_findings",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        # Subject
        sa.Column("seed_type", sa.String(64), nullable=False),
        sa.Column("seed_key", sa.String(512), nullable=False),
        sa.Column("seed_display_name", sa.Text(), nullable=True),
        sa.Column("seed_identifier_json", postgresql.JSONB(), nullable=False),
        # Evidence
        sa.Column("evidence_type", sa.String(64), nullable=False),
        sa.Column("source_document_id", sa.Integer(), nullable=True),
        sa.Column("risk_event_id", sa.Integer(), nullable=True),
        sa.Column("evidence_json", postgresql.JSONB(), nullable=False),
        # Scoring and triage
        sa.Column("relevance", sa.String(16), nullable=False),
        sa.Column("match_signals_json", postgresql.JSONB(), nullable=False),
        sa.Column(
            "status",
            sa.String(32),
            nullable=False,
            server_default="open",
        ),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reviewed_by", sa.String(256), nullable=True),
        sa.Column("reviewer_note", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["seed_review_runs.run_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_document_id"],
            ["source_documents.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["risk_event_id"],
            ["risk_events.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_index(
        "ix_seed_review_findings_run_id",
        "seed_review_findings",
        ["run_id"],
    )
    op.create_index(
        "ix_seed_review_findings_seed",
        "seed_review_findings",
        ["seed_type", "seed_key"],
    )
    op.create_index(
        "ix_seed_review_findings_status",
        "seed_review_findings",
        ["status"],
    )
    op.create_index(
        "ix_seed_review_findings_ident",
        "seed_review_findings",
        ["seed_identifier_json"],
        postgresql_using="gin",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_seed_review_findings_ident",
        table_name="seed_review_findings",
    )
    op.drop_index(
        "ix_seed_review_findings_status",
        table_name="seed_review_findings",
    )
    op.drop_index(
        "ix_seed_review_findings_seed",
        table_name="seed_review_findings",
    )
    op.drop_index(
        "ix_seed_review_findings_run_id",
        table_name="seed_review_findings",
    )
    op.drop_table("seed_review_findings")
    op.drop_table("seed_review_runs")
