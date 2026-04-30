"""Add supply metrics columns to material_criticality_signals.

Promotes four signals that were previously discarded from the USGS MCS CSV
into structured, scorable columns on material_criticality_signals:

  reserve_hhi_score      — HHI of reserve distribution by country (0–1).
                           Independent forward-looking concentration signal.
                           Computed identically to hhi_score but on RESERVES_2024
                           country figures rather than PROD_2023.

  reserve_life_index     — World reserves / world annual production (years).
                           Low RLI = near-term scarcity risk. NULL when either
                           figure is absent from MCS.

  production_yoy_pct     — (PROD_EST_2024 - PROD_2023) / PROD_2023 as a
                           signed fraction (e.g. 0.08 = +8%, -0.05 = -5%).
                           Contraction signals supply stress; growth signals
                           easing. NULL when either year is absent.

  capacity_utilization   — World mine production / world mine capacity (0–1+).
                           Values > 1.0 can occur due to MCS rounding. Clamped
                           to [0, 1] in the scoring engine. NULL when capacity
                           data is absent (typical for smelter/refinery rows).

Revision ID: 019_criticality_signal_supply_metrics
Revises:     018_chem_risk_upsert_key
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "019_criticality_supply_metrics"
down_revision: Union[str, None] = "018_chem_risk_upsert_key"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "material_criticality_signals",
        sa.Column(
            "reserve_hhi_score",
            sa.Float(),
            nullable=True,
            comment=(
                "HHI of reserve distribution by country (0–1, Σ share_i² on "
                "RESERVES_2024 per country). Forward-looking concentration signal "
                "independent of production HHI."
            ),
        ),
    )
    op.add_column(
        "material_criticality_signals",
        sa.Column(
            "reserve_life_index",
            sa.Float(),
            nullable=True,
            comment=(
                "World reserves / world annual production (years). "
                "Lower = nearer-term scarcity risk. NULL when MCS lacks either figure."
            ),
        ),
    )
    op.add_column(
        "material_criticality_signals",
        sa.Column(
            "production_yoy_pct",
            sa.Float(),
            nullable=True,
            comment=(
                "Year-over-year change in world mine production as a signed fraction "
                "(PROD_EST_2024 - PROD_2023) / PROD_2023. "
                "Negative = contracting supply. NULL when either year is absent."
            ),
        ),
    )
    op.add_column(
        "material_criticality_signals",
        sa.Column(
            "capacity_utilization",
            sa.Float(),
            nullable=True,
            comment=(
                "World mine production / world mine capacity (0–1). "
                "High utilization = tight market with little buffer. "
                "NULL when MCS capacity data is absent."
            ),
        ),
    )


def downgrade() -> None:
    op.drop_column("material_criticality_signals", "capacity_utilization")
    op.drop_column("material_criticality_signals", "production_yoy_pct")
    op.drop_column("material_criticality_signals", "reserve_life_index")
    op.drop_column("material_criticality_signals", "reserve_hhi_score")
