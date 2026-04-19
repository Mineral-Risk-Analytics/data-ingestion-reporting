"""Company vehicle models + per-model battery chemistry mix.

Two new tables that give us product-level grain on chemistry exposure
(``Tesla.Model 3 Standard = LFP`` vs ``Tesla.Model 3 Long Range = NMC``).
The scoring pipeline rolls these up into a company-level chemistry mix that
re-weights the material pillar so a 60% LFP / 40% NMC OEM gets material risk
dominated by the materials it actually consumes — not by a flat sum across all
``CompanyMaterialExposure`` rows.

- ``company_vehicle_models``: one row per (company, model_name, model_year_start).
  Production volume is nullable; uniform weighting is applied when missing.
- ``vehicle_model_chemistries``: time-windowed share per chemistry per model.
  Within a model, shares MUST sum to 1.0 over the active window.

No ingestion job is shipped with this migration; rows are hand-seeded from the
top-OEM fixture.

Apply via:
  alembic -x direct=true upgrade head
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "007_company_vehicle_models"
down_revision: Union[str, None] = "006_supply_chain_rollup"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "company_vehicle_models",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column(
            "company_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("model_name", sa.Text(), nullable=False),
        sa.Column("model_year_start", sa.Integer(), nullable=True),
        sa.Column("model_year_end", sa.Integer(), nullable=True),
        sa.Column("production_volume_units", sa.Integer(), nullable=True),
        sa.Column("production_volume_year", sa.Integer(), nullable=True),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column("data_source", sa.String(128), nullable=True),
        sa.Column("metadata_json", postgresql.JSONB(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["company_id"], ["companies.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "company_id",
            "model_name",
            "model_year_start",
            name="uq_company_model_year",
        ),
    )
    op.create_index(
        "ix_company_vehicle_models_company",
        "company_vehicle_models",
        ["company_id"],
    )

    op.create_table(
        "vehicle_model_chemistries",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("vehicle_model_id", sa.Integer(), nullable=False),
        sa.Column("battery_chemistry_id", sa.Integer(), nullable=False),
        sa.Column("share_pct", sa.Float(), nullable=False),
        sa.Column("valid_from", sa.Date(), nullable=False),
        sa.Column("valid_to", sa.Date(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["vehicle_model_id"],
            ["company_vehicle_models.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["battery_chemistry_id"],
            ["battery_chemistries.id"],
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "share_pct > 0 AND share_pct <= 1",
            name="ck_vehicle_model_share_range",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "vehicle_model_id",
            "battery_chemistry_id",
            "valid_from",
            name="uq_vehicle_model_chem_window",
        ),
    )
    op.create_index(
        "ix_vehicle_model_chemistries_model",
        "vehicle_model_chemistries",
        ["vehicle_model_id"],
    )
    op.create_index(
        "ix_vehicle_model_chemistries_chem",
        "vehicle_model_chemistries",
        ["battery_chemistry_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_vehicle_model_chemistries_chem",
        table_name="vehicle_model_chemistries",
    )
    op.drop_index(
        "ix_vehicle_model_chemistries_model",
        table_name="vehicle_model_chemistries",
    )
    op.drop_table("vehicle_model_chemistries")
    op.drop_index(
        "ix_company_vehicle_models_company",
        table_name="company_vehicle_models",
    )
    op.drop_table("company_vehicle_models")
