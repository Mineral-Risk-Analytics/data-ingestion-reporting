"""Event triage status model — machine proposes, human disposes.

Revision ID: 066
Revises: 065

Triage plan Phase 2 (docs/design/event_triage_pipeline_plan.md, decided
2026-07-31 with Nicole). Formalises the four-state vocabulary that until now
existed only in code (``event_dedupe._event_status``) as real columns, and
gives suggestions a place to live so ingesters can stop writing
authoritative categorizations.

``risk_events`` gains:

  triage_status       pending_triage | scoring | display_only | rejected.
                      The event's actual state. ``rejected`` is the soft
                      dismiss: row retained (dedupe needs the corpse),
                      hidden from every surface.
  suggested_category  The machine's proposed pillar. ``primary_category``
                      (migration 062) remains the HUMAN-confirmed pillar and
                      is no longer autofilled at insert — pillar queries
                      filter on primary_category, so an unconfirmed event
                      structurally cannot score.
  direction           restrictive | supportive | neutral — the supply-risk
                      direction from a buyer's perspective. Added because
                      55% of the GTA corpus is subsidies marked "Red"
                      (harmful to foreign *competitors*) that mostly ADD
                      supply; see docs/design/gta_categorization_audit.md
                      finding F1.
  triaged_by /        Who confirmed/dismissed, and when. NULL until a human
  triaged_at          acts.

``risk_event_materials`` gains:

  status              suggested | confirmed | rejected. Machine-written
                      links are suggestions; scoring's confirmed-links
                      filter arrives with the Phase 6 flip.

Backfill (grandfather rule, per Nicole's "keep old scores" decision):
existing events keep working exactly as before — rows that score today
(primary_category NOT NULL) become ``scoring``; rows that were already
display-only (primary_category NULL) become ``display_only``. Existing
material links become ``confirmed``. Nothing about current scoring output
changes at migration time; the delete-and-reingest reset (Phase 4) is what
repopulates events through the suggestion pipeline as ``pending_triage``.
"""

from alembic import op
import sqlalchemy as sa

revision = "066"
down_revision = "065"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "risk_events",
        sa.Column(
            "triage_status",
            sa.String(16),
            nullable=False,
            server_default="pending_triage",
            comment=(
                "pending_triage | scoring | display_only | rejected. "
                "rejected = soft dismiss (row retained for dedupe, hidden "
                "from all surfaces)."
            ),
        ),
    )
    op.add_column(
        "risk_events",
        sa.Column(
            "suggested_category",
            sa.String(32),
            nullable=True,
            comment=(
                "Machine-suggested pillar (RiskCategory value). "
                "primary_category stays the human-confirmed pillar."
            ),
        ),
    )
    op.add_column(
        "risk_events",
        sa.Column(
            "direction",
            sa.String(16),
            nullable=True,
            comment=(
                "restrictive | supportive | neutral — supply-risk direction "
                "from a buyer's perspective (GTA audit F1)."
            ),
        ),
    )
    op.add_column(
        "risk_events",
        sa.Column("triaged_by", sa.String(128), nullable=True),
    )
    op.add_column(
        "risk_events",
        sa.Column("triaged_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_risk_events_triage_status", "risk_events", ["triage_status"]
    )

    op.add_column(
        "risk_event_materials",
        sa.Column(
            "status",
            sa.String(16),
            nullable=False,
            server_default="suggested",
            comment="suggested | confirmed | rejected",
        ),
    )

    # ── Grandfather backfill ────────────────────────────────────────────
    # Existing rows keep their current effective behaviour: scoring rows
    # stay scoring, display-only rows stay display-only, links that have
    # been feeding scores are treated as confirmed.
    op.execute(
        """
        UPDATE risk_events SET triage_status = CASE
            WHEN primary_category IS NOT NULL THEN 'scoring'
            ELSE 'display_only'
        END
        """
    )
    op.execute("UPDATE risk_event_materials SET status = 'confirmed'")


def downgrade() -> None:
    op.drop_column("risk_event_materials", "status")
    op.drop_index("ix_risk_events_triage_status", table_name="risk_events")
    op.drop_column("risk_events", "triaged_at")
    op.drop_column("risk_events", "triaged_by")
    op.drop_column("risk_events", "direction")
    op.drop_column("risk_events", "suggested_category")
    op.drop_column("risk_events", "triage_status")
