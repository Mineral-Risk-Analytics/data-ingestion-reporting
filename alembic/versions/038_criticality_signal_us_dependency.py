"""Lift US-dependency signals from metadata_json to typed columns.

Adds two typed columns to ``material_criticality_signals``:

  us_net_import_reliance_pct  — partner-curated USGS NIR (0–100, integer
                                or midpoint of bounded estimate).  Sourced
                                from MCS Salient Statistics "Net import
                                reliance as a percentage of apparent
                                consumption" rows.  Was previously stored
                                in ``metadata_json[us_net_import_reliance_pct]``.

  us_apparent_consumption     — US apparent consumption volume (latest
                                year, in the source's reporting unit).
                                Was previously stored in
                                ``metadata_json[us_apparent_consumption]``.

Why typed columns
-----------------
Both fields are queried often enough (Material Concentration uplift,
IRA dependency views, frontend Overview signal strip) that keeping them
behind JSONB extraction adds friction and prevents efficient indexing.
The metadata_json blob continues to carry less-structured extras (e.g.
``fig10_source_rows``, ``mcs_publication_year``).

Backfill strategy
-----------------
The migration includes a single UPDATE that copies values out of
``metadata_json`` for rows where the typed column is NULL.  Idempotent
on re-run (no-op if values are already populated).

Revision ID: 038
Revises:     037
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "038"
down_revision: Union[str, None] = "037"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "material_criticality_signals",
        sa.Column(
            "us_net_import_reliance_pct",
            sa.Float(),
            nullable=True,
            comment=(
                "USGS Net Import Reliance as a percentage of apparent "
                "consumption (0–100). Bounded estimates ('<50', '>50') "
                "are stored as midpoint values (25, 75). Sourced from "
                "MCS Salient Statistics."
            ),
        ),
    )
    op.add_column(
        "material_criticality_signals",
        sa.Column(
            "us_apparent_consumption",
            sa.Float(),
            nullable=True,
            comment=(
                "US apparent consumption volume for the latest year in "
                "the source's reporting unit (typically metric tons). "
                "Sourced from MCS Salient Statistics 'Consumption, "
                "apparent' rows."
            ),
        ),
    )

    # ── Backfill from existing metadata_json blob ──────────────────────
    # Idempotent: only overwrites NULL values.
    op.execute(
        """
        UPDATE material_criticality_signals
        SET us_net_import_reliance_pct = (metadata_json->>'us_net_import_reliance_pct')::float
        WHERE us_net_import_reliance_pct IS NULL
          AND metadata_json ? 'us_net_import_reliance_pct'
        """
    )
    op.execute(
        """
        UPDATE material_criticality_signals
        SET us_apparent_consumption = (metadata_json->>'us_apparent_consumption')::float
        WHERE us_apparent_consumption IS NULL
          AND metadata_json ? 'us_apparent_consumption'
        """
    )


def downgrade() -> None:
    # No data preservation on downgrade — typed columns drop, the
    # metadata_json blob still has the values (the upgrade backfill
    # is read-only on metadata_json, never deleted).
    op.drop_column("material_criticality_signals", "us_apparent_consumption")
    op.drop_column("material_criticality_signals", "us_net_import_reliance_pct")
