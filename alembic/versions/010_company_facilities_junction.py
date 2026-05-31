"""Replace facilities.company_id FK with company_facilities junction table.

Rationale: a physical facility (e.g. BlueOvalSK, Ultium Cells) can be a JV
owned by multiple companies. The old one-to-many relationship via
``facilities.company_id`` cannot represent this. The new design is:

  facilities   (1) --- (M) company_facilities (M) --- (1) companies

Each junction row carries:
  - ownership_type  e.g. "operator" | "jv_partner" | "lessee"
  - ownership_pct   optional numeric stake (0.0–1.0)
  - verified        analyst has confirmed this company-facility link

The global ``facilities.verified`` flag (added in migration 008) is kept as-is
and continues to mean "this facility physically exists / is real".

Revision ID: 010_company_facilities_junction
Revises:     009_event_review_status
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "010_company_facilities_junction"
down_revision: Union[str, None] = "009_event_review_status"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Clear seeded data so there are no FK violations during column drop.
    op.execute("DELETE FROM facilities")

    # 2. Drop the old company_id column (and its index / FK constraint).
    #    The index name follows the SQLAlchemy default naming convention.
    op.drop_index("ix_facilities_company_id", table_name="facilities")
    op.drop_constraint(
        "facilities_company_id_fkey", "facilities", type_="foreignkey"
    )
    op.drop_column("facilities", "company_id")

    # 3. Create the junction table.
    op.create_table(
        "company_facilities",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "company_id",
            UUID(as_uuid=True),
            sa.ForeignKey("companies.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "facility_id",
            UUID(as_uuid=True),
            sa.ForeignKey("facilities.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "ownership_type",
            sa.String(32),
            nullable=False,
            server_default="operator",
        ),
        # operator | jv_partner | lessee | minority_stake | other
        sa.Column("ownership_pct", sa.Float(), nullable=True),
        sa.Column(
            "verified",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("company_id", "facility_id", name="uq_company_facility"),
    )


def downgrade() -> None:
    op.drop_table("company_facilities")

    op.add_column(
        "facilities",
        sa.Column(
            "company_id",
            UUID(as_uuid=True),
            sa.ForeignKey("companies.id", ondelete="CASCADE"),
            nullable=False,
        ),
    )
    op.create_index("ix_facilities_company_id", "facilities", ["company_id"])
