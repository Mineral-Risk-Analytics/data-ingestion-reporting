"""021 — drop legacy material_scores and geography_scores tables.

These tables were the v1.0/v2.0 era scoring outputs. They have been superseded by:
  - material_geography_risk_scores  (MaterialGeographyRiskScore — per geo, five pillars)
  - material_global_risk_scores     (MaterialGlobalRiskScore — trade-weighted rollup)
  - chemistry_risk_scores           (ChemistryRiskScore — per chemistry)

No live code path writes to or reads from material_scores or geography_scores as of
the v3.0 scoring refactor. See docs/deprecation-audit.md §A1, §A2.

Revision ID: 021_drop_legacy_scores
Revises: 020_countries
Create Date: 2026-04-30
"""

from alembic import op

revision = "021_drop_legacy_scores"
down_revision = "020_countries"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_table("geography_scores")
    op.drop_table("material_scores")


def downgrade() -> None:
    # Restore material_scores
    op.execute(
        """
        CREATE TABLE material_scores (
            id                          SERIAL PRIMARY KEY,
            material_id                 INTEGER NOT NULL REFERENCES materials(id) ON DELETE CASCADE,
            as_of_date                  DATE NOT NULL,
            material_concentration_score DOUBLE PRECISION,
            geopolitical_trade_score    DOUBLE PRECISION,
            regulatory_compliance_score DOUBLE PRECISION,
            operational_score           DOUBLE PRECISION,
            financial_pressure_score    DOUBLE PRECISION,
            overall_risk_score          DOUBLE PRECISION,
            company_count               INTEGER NOT NULL DEFAULT 0,
            event_count                 INTEGER NOT NULL DEFAULT 0,
            rationale_json              JSONB,
            scoring_version             VARCHAR(32) NOT NULL DEFAULT '2.0',
            created_at                  TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX ix_material_scores_material_id ON material_scores (material_id)")
    op.execute("CREATE INDEX ix_material_scores_as_of_date  ON material_scores (as_of_date)")

    # Restore geography_scores
    op.execute(
        """
        CREATE TABLE geography_scores (
            id                          SERIAL PRIMARY KEY,
            geography_code              VARCHAR(2) NOT NULL,
            as_of_date                  DATE NOT NULL,
            geopolitical_trade_score    DOUBLE PRECISION,
            regulatory_compliance_score DOUBLE PRECISION,
            operational_score           DOUBLE PRECISION,
            overall_risk_score          DOUBLE PRECISION,
            company_count               INTEGER NOT NULL DEFAULT 0,
            event_count                 INTEGER NOT NULL DEFAULT 0,
            rationale_json              JSONB,
            scoring_version             VARCHAR(32) NOT NULL DEFAULT '2.0',
            created_at                  TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX ix_geography_scores_geography_code ON geography_scores (geography_code)")
    op.execute("CREATE INDEX ix_geography_scores_as_of_date     ON geography_scores (as_of_date)")
