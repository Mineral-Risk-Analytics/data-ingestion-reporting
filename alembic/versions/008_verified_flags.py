"""Add verified boolean flag to relationship and reference-data tables.

Each table gets a non-nullable ``verified`` column (DEFAULT false) so that:
  - Relationship tables (under company detail) can be marked verified row-by-row.
  - Reference/global tables (viewed in data pages) can be marked verified entity-by-entity.

Tables updated:
  Relationship (company-scoped):
    company_material_exposures, company_supply_relationships,
    company_regulation_exposure, company_vehicle_models

  Reference/global:
    companies, materials, regulations, facilities,
    battery_chemistries, risk_events

Revision ID: 008_verified_flags
Revises:     007_company_vehicle_models
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "008_verified_flags"
down_revision: Union[str, None] = "007_company_vehicle_models"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLES = [
    # Relationship tables
    "company_material_exposures",
    "company_supply_relationships",
    "company_regulation_exposure",
    "company_vehicle_models",
    # Reference / global tables
    "companies",
    "materials",
    "regulations",
    "facilities",
    "battery_chemistries",
    "risk_events",
]


def upgrade() -> None:
    for table in _TABLES:
        op.add_column(
            table,
            sa.Column(
                "verified",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            ),
        )


def downgrade() -> None:
    for table in reversed(_TABLES):
        op.drop_column(table, "verified")
