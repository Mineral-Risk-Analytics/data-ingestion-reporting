"""020 — add countries reference table.

Revision ID: 020_countries
Revises: 019_criticality_supply_metrics
Create Date: 2026-04-30

Adds a ``countries`` table that serves as the single source of truth for
ISO 3166-1 alpha-2 country codes, Comtrade numeric reporter codes, and
common-name aliases used by ingesters.

Replaces:
  - ``REPORTER_COUNTRIES`` / ``CONSUMER_COUNTRIES`` hardcoded dicts in
    ``app/services/ingestion/comtrade.py`` (query by is_major_producer /
    is_major_consumer instead).
  - ``GTA_COUNTRY_MAP`` in ``app/services/ingestion/gta.py`` (resolve
    full country names via common_names JSONB array instead).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "020_countries"
down_revision = "019_criticality_supply_metrics"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "countries",
        sa.Column("iso2", sa.String(2), primary_key=True,
                  comment="ISO 3166-1 alpha-2 code, upper-case. Bloc identifiers like 'EU' allowed."),
        sa.Column("name", sa.String(128), nullable=False,
                  comment="Official or canonical display name."),
        sa.Column("iso3", sa.String(3), nullable=True,
                  comment="ISO 3166-1 alpha-3 code. NULL for bloc identifiers."),
        sa.Column("region", sa.String(64), nullable=True,
                  comment="Broad geographic region (e.g. 'Asia', 'Africa')."),
        sa.Column("comtrade_code", sa.Integer(), nullable=True, index=True,
                  comment="UN Comtrade M49 numeric reporter code."),
        sa.Column("common_names", postgresql.JSONB(), nullable=True,
                  comment="Array of name variants used by external sources for name→ISO2 resolution."),
        sa.Column("is_major_producer", sa.Boolean(), nullable=False, server_default="false",
                  comment="Tracked as export reporter in Comtrade ingestion."),
        sa.Column("is_major_consumer", sa.Boolean(), nullable=False, server_default="false",
                  comment="Tracked as import reporter in Comtrade ingestion."),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), onupdate=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("countries")
