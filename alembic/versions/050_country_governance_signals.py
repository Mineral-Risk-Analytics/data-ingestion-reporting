"""Add ``country_governance_signals`` reference table for Step 3 WGI overlay.

Background
----------
Step 1 (HHI cliff mapping) and Step 2A/B (sparse-coverage bridges)
addressed the *magnitude* of the Material Concentration signal but said
nothing about *who* controls the supply.  A 75% production share in DRC
carries materially different risk than the same 75% share in Australia
— same HHI, very different governance, expropriation, and operational
risk.  Today's scoring treats them identically.

The JRC critical raw materials methodology resolves this by overlaying
World Bank Worldwide Governance Indicators (WGI) on the concentration
math.  In their published formula:

    Supply Risk = HHI × Σ_c [ share_c × (1 - WGI_c_normalised) ]

Our scoring is per-(material × geography), not the global supply-risk
aggregate JRC publishes, so we adapt: each country's
``country_concentration`` sub-input in the Geopolitical pillar is
discounted by the country's own governance quality:

    country_concentration_adjusted =
        country_concentration_raw × (1 − α × WGI_c_normalised)

with ``α = 0.5`` (the JRC effective 50/50 multiplicative split).

Source
------
World Bank Worldwide Governance Indicators, annual.  Six dimensions:

  voice_accountability
  political_stability
  government_effectiveness
  regulatory_quality
  rule_of_law
  control_of_corruption

WGI publishes two scales per (country, year, dimension): a raw
"estimate" in ≈ [-2.5, +2.5] and a "percentile_rank" in [0, 100].  The
percentile rank is what JRC uses, what most policy literature uses,
and what the rest of our scoring will consume.  Raw estimates are not
stored — if we ever need them we'll add a second column.

``composite_pct`` is the mean of the six percentile ranks.  Stored as
a denormalised cache so consumers don't recompute it on every read
(stable for a given country-year row — only changes on re-ingest).

Schema design choices
---------------------
* ``country_code`` is ISO-3166-1 alpha-2.  WGI publishes ISO-3 ("USA")
  internally; ingester normalises to alpha-2.  Joins to
  ``MaterialProductionShare.country_code`` are direct after that.
* ``reference_year`` is the year the data covers (e.g. 2023), NOT the
  publish year (WGI 2024 release covers 2023 data).
* ``source`` is included in the unique key so a future second source
  (Fraser Institute Mining Investment Attractiveness Index, etc.)
  could coexist with WGI rows.
* Annual ingestion accumulates rows — no DELETE-then-INSERT on re-run.
  ``--force`` UPDATEs existing rows in place.  Historical trend
  analysis (Step 3.x future) reads any year ≤ the as-of date.

Revision ID: 050
Revises:     049
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "050"
down_revision: Union[str, None] = "049"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "country_governance_signals",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "country_code",
            sa.String(2),
            nullable=False,
            index=True,
            comment=(
                "ISO 3166-1 alpha-2.  Ingester normalises from WGI's "
                "ISO-3 internal codes."
            ),
        ),
        sa.Column(
            "reference_year",
            sa.SmallInteger(),
            nullable=False,
            comment=(
                "Year the data covers (e.g. 2023).  WGI publishes ~Q4 "
                "of the following year, so WGI 2024 release covers 2023."
            ),
        ),
        sa.Column(
            "source",
            sa.String(32),
            nullable=False,
            server_default="worldbank_wgi",
            comment=(
                "Provenance.  'worldbank_wgi' for the standard WGI feed.  "
                "Future sources (Fraser Institute MIA, ICRG, etc.) can "
                "coexist via the unique key including this column."
            ),
        ),
        # ── Six WGI dimensions (percentile rank 0-100) ────────────────────
        sa.Column(
            "voice_accountability_pct",
            sa.Float(),
            nullable=True,
            comment="WGI percentile rank 0-100, higher = better governance.",
        ),
        sa.Column(
            "political_stability_pct",
            sa.Float(),
            nullable=True,
        ),
        sa.Column(
            "government_effectiveness_pct",
            sa.Float(),
            nullable=True,
        ),
        sa.Column(
            "regulatory_quality_pct",
            sa.Float(),
            nullable=True,
        ),
        sa.Column(
            "rule_of_law_pct",
            sa.Float(),
            nullable=True,
        ),
        sa.Column(
            "control_of_corruption_pct",
            sa.Float(),
            nullable=True,
        ),
        sa.Column(
            "composite_pct",
            sa.Float(),
            nullable=True,
            comment=(
                "Denormalised mean of the six dimensions (NULL-safe — "
                "computed over the non-NULL subset).  This is the value "
                "Step 3's apply_wgi_governance_overlay reads.  Stored "
                "rather than re-derived per-query because it doesn't "
                "change between ingests of the same country-year row."
            ),
        ),
        sa.Column(
            "n_dimensions_present",
            sa.SmallInteger(),
            nullable=False,
            server_default="0",
            comment=(
                "How many of the six dimensions had data.  Drives a "
                "data-coverage gate in the overlay helper — countries "
                "with too few dimensions present (e.g. small "
                "jurisdictions, contested territories) shouldn't have "
                "their concentration discounted by an unreliable "
                "composite."
            ),
        ),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "country_code", "reference_year", "source",
            name="uq_country_governance_signal",
        ),
    )


def downgrade() -> None:
    op.drop_table("country_governance_signals")
