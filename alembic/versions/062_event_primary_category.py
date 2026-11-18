"""risk_events.primary_category — each event scores in exactly ONE pillar.

Build 1 of the regulatory/event-model scope (2026-07-24, see
docs/design/regulatory_pillar_and_event_model_scope.md). Fixes the
cross-pillar double-count: pillar queries previously used
``risk_categories_json.contains([category])``, so a multi-tagged event
(89×2, 16×3, 2×4 = 107 events) scored in every tagged pillar, violating
spec Principle 3 ("each fact scores once"). ``risk_categories_json``
stays multi-valued for display/filtering.

NULL = display-only (never selected by pillar queries). Two streams stay
NULL by decision: orphan SEC filing signals (no links, nothing consumes
them — stream paused) and trade-signal derived statistics (sourceless
GEOPOLITICAL_TRADE rows — statistics, not government/operator actions).

Backfill precedence (most-specific wins; sanctions geo+reg → geopolitical
per spec §4): operational > geopolitical_trade > regulatory_compliance >
financial_pressure > material_concentration.

Revision ID: 062
Revises: 061
"""

from alembic import op
import sqlalchemy as sa

revision = "062"
down_revision = "061"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "risk_events",
        sa.Column(
            "primary_category",
            sa.String(32),
            nullable=True,
            comment=(
                "The ONE pillar this event scores in (RiskCategory value). "
                "NULL = display-only. risk_categories_json remains the "
                "multi-value display/filter tagging."
            ),
        ),
    )
    op.create_index(
        "ix_risk_events_primary_category", "risk_events", ["primary_category"]
    )
    op.execute("""
        UPDATE risk_events SET primary_category = CASE
            WHEN risk_categories_json @> '["operational"]'::jsonb
                THEN 'operational'
            WHEN risk_categories_json @> '["geopolitical_trade"]'::jsonb
                THEN 'geopolitical_trade'
            WHEN risk_categories_json @> '["regulatory_compliance"]'::jsonb
                THEN 'regulatory_compliance'
            WHEN risk_categories_json @> '["financial_pressure"]'::jsonb
                THEN 'financial_pressure'
            WHEN risk_categories_json @> '["material_concentration"]'::jsonb
                THEN 'material_concentration'
            ELSE NULL
        END
        WHERE NOT (
            event_type = 'sec_filing_signal'
            OR (source_document_id IS NULL AND event_type = 'GEOPOLITICAL_TRADE')
        )
    """)


def downgrade() -> None:
    op.drop_index("ix_risk_events_primary_category", table_name="risk_events")
    op.drop_column("risk_events", "primary_category")
