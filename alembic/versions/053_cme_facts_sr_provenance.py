"""CME fact columns + SR provenance columns for workbook ingestion.

Nicole 2026-07-11: the partner company-seed workbook's ``Company Material
Exposure`` and ``Supply Relationships`` tabs are now more accurate than the
hardcoded seed dicts (walkthrough-enriched through Glencore + Ganfeng).
Loading them requires columns the original tables lacked.

company_material_exposures — the workbook carries FILING FACTS the table
had no home for. exposure_score stays the judgment column (hybrid rule:
curated seed dict where the (company, material) pair exists, derived from
revenue_share_pct at low confidence elsewhere — see
``score_derivation``); the new columns carry the auditable facts behind it:

  * production_tonnage / production_unit / production_year
        Annual production from 10-K Item 7 / 20-F operating results.
        Feeds Material Concentration HHI once the facility-derived shares
        ETL lands; until then it is provenance.
  * revenue_share_pct / revenue_year
        Fraction of total revenue from this material (segment tables).
        Weights the Financial Pressure pillar across materials, and anchors
        the derived exposure_score where no curated score exists.
  * battery_grade_relevance
        0–1 fraction of the exposure that is battery-relevant (steel-grade
        iron ore at Vale = 0.03; Class-1 nickel = 0.70). Weights Phase 4
        SEC body-text attribution.
  * source_url
        Filing URL on EDGAR / IR site.
  * score_derivation
        Where exposure_score came from:
          'curated_seed'          — from seed_material_exposures.py dict
          'derived_revenue_share' — 0.25 + 0.75 × revenue_share_pct,
                                    clamped to [0.25, 0.95], confidence 0.4
          'default_unscored'      — 0.5 engine default; no revenue share
                                    available, confidence 0.3
        Derived/default rows are the partner-review queue.

company_supply_relationships — the workbook SR tab carries agreement
detail the table lacked:

  * agreement_type       offtake | supply_agreement | joint_development |
                         equity_offtake | framework | spot | jv | unknown
  * contract_term_years  0 = spot
  * announced_date       public announcement date
  * source_url           press release / 8-K URL
  * notes                free-text terms and conditions

Both tables: plain nullable ADD COLUMNs, no backfill, no constraint
changes. Downgrade drops them.

Revision ID: 053
Revises: 052
"""

from __future__ import annotations

from typing import Union

import sqlalchemy as sa

from alembic import op

revision: str = "053"
down_revision: Union[str, None] = "052"
branch_labels = None
depends_on = None

_CME = "company_material_exposures"
_SR = "company_supply_relationships"


def upgrade() -> None:
    # ── company_material_exposures: filing facts ─────────────────────
    op.add_column(_CME, sa.Column(
        "production_tonnage", sa.Float(), nullable=True,
        comment=(
            "Annual production in production_unit, from 10-K Item 7 / 20-F "
            "operating results / 6-K quarterly production reports."
        ),
    ))
    op.add_column(_CME, sa.Column(
        "production_unit", sa.String(64), nullable=True,
        comment=(
            "Unit for production_tonnage as stated in the filing: 't', 'kt', "
            "'Mt', 'kt LCE', 't Au', ... Deliberately free-form (String) — "
            "commodity-specific bases (LCE, contained metal, wmt) must not "
            "be silently normalised."
        ),
    ))
    op.add_column(_CME, sa.Column(
        "production_year", sa.Integer(), nullable=True,
        comment="Reporting year (YYYY) for production_tonnage.",
    ))
    op.add_column(_CME, sa.Column(
        "revenue_share_pct", sa.Float(), nullable=True,
        comment=(
            "Fraction (0-1) of company's total revenue from this material in "
            "revenue_year. Source: segment revenue tables. Weights the "
            "Financial Pressure pillar; anchors derived exposure_score."
        ),
    ))
    op.add_column(_CME, sa.Column(
        "revenue_year", sa.Integer(), nullable=True,
        comment="Reporting year (YYYY) for revenue_share_pct.",
    ))
    op.add_column(_CME, sa.Column(
        "battery_grade_relevance", sa.Float(), nullable=True,
        comment=(
            "0-1 fraction of this exposure that is battery-grade-relevant. "
            "Weights Phase 4 SEC body-text attribution."
        ),
    ))
    op.add_column(_CME, sa.Column(
        "source_url", sa.Text(), nullable=True,
        comment="URL of the filing this row was extracted from.",
    ))
    op.add_column(_CME, sa.Column(
        "score_derivation", sa.String(32), nullable=True,
        comment=(
            "Provenance of exposure_score: curated_seed | "
            "derived_revenue_share | default_unscored. NULL = pre-053 row. "
            "derived_/default_ rows are the partner-review queue."
        ),
    ))

    # ── company_supply_relationships: agreement detail ────────────────
    op.add_column(_SR, sa.Column(
        "agreement_type", sa.String(32), nullable=True,
        comment=(
            "offtake | supply_agreement | joint_development | equity_offtake "
            "| framework | spot | jv | unknown. Complements "
            "relationship_type (which is evidential: direct | indirect | "
            "estimated | framework)."
        ),
    ))
    op.add_column(_SR, sa.Column(
        "contract_term_years", sa.Float(), nullable=True,
        comment="Agreement length in years; 0 = spot.",
    ))
    op.add_column(_SR, sa.Column(
        "announced_date", sa.Date(), nullable=True,
        comment="Date the agreement was publicly announced.",
    ))
    op.add_column(_SR, sa.Column(
        "source_url", sa.Text(), nullable=True,
        comment="Press release, 8-K, or other primary URL.",
    ))
    op.add_column(_SR, sa.Column(
        "notes", sa.Text(), nullable=True,
        comment="Free text — terms, conditions, related agreements.",
    ))


def downgrade() -> None:
    for col in ("notes", "source_url", "announced_date",
                "contract_term_years", "agreement_type"):
        op.drop_column(_SR, col)
    for col in ("score_derivation", "source_url", "battery_grade_relevance",
                "revenue_year", "revenue_share_pct", "production_year",
                "production_unit", "production_tonnage"):
        op.drop_column(_CME, col)
