"""Add ``country_material_relevance`` table — per-(country, material) producer/consumer roles.

Background
----------
The ``countries.is_major_producer`` / ``is_major_consumer`` flags are
binary and global — they apply uniformly across every material the
engine tracks.  This is a poor methodological fit because each material
has a very different set of relevant producing and consuming countries:
Australia is a top lithium producer but a minor cobalt producer; DRC
dominates cobalt but is a minor nickel producer; China is a major
refiner of cathode chemistry but only a moderate raw-ore producer.

The same limitation applies to the High-Concentration Geography (HCG)
constant in the scoring engine — a global set of {CN, CD, RU} that
flags a country as concentration-risky regardless of the material being
scored.  Cobalt scoring should treat DRC as HCG; aluminum scoring
should not.

Schema
------
``country_material_relevance`` carries one row per
(country, material, role) tuple.  ``role`` is either ``'producer'`` or
``'consumer'``; a country can have both rows for the same material.

Phase 1 (this migration): table only — no scoring change.  The seed
function populates producer rows automatically from
``material_production_shares`` using share-based thresholds (top/mid/
minor tier; HCG if share ≥ 40%), and seeds initial consumer placeholders
for each launch material × every is_major_consumer country.  Partner
curates from there.

Phase 2 (future migration): the scoring engine's HCG lookup switches
from the global set to a per-(country, material) query against this
table.  At that point ``countries.is_major_producer`` and
``is_major_consumer`` can be deprecated.

Phase 3 (future): the Comtrade ingester optionally filters queries by
material relevance to save API quota.

Revision ID: 046
Revises:     045
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "046"
down_revision: Union[str, None] = "045"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "country_material_relevance",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "material_id",
            sa.Integer(),
            sa.ForeignKey("materials.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "country_code",
            sa.String(2),
            nullable=False,
            index=True,
            comment="ISO-3166-1 alpha-2 country code.  Joins to countries.iso2.",
        ),
        sa.Column(
            "role",
            sa.String(16),
            nullable=False,
            comment="'producer' or 'consumer'.  A country can have both rows for the same material.",
        ),
        sa.Column(
            "tier",
            sa.String(16),
            nullable=True,
            comment=(
                "Relevance tier: 'top' (≥30% global share for producers; "
                "partner-curated for consumers), 'mid' (10-30%), 'minor' "
                "(1-10%), 'emerging' (announced but not yet producing)."
            ),
        ),
        sa.Column(
            "is_hcg",
            sa.Boolean(),
            nullable=False,
            server_default="false",
            comment=(
                "High-Concentration Geography flag, per-(material, country). "
                "Default threshold: share ≥ 40%.  Phase 2: scoring engine "
                "reads this column instead of the global HCG set."
            ),
        ),
        sa.Column(
            "source",
            sa.String(32),
            nullable=False,
            comment=(
                "'mcs_share' (auto-seeded from material_production_shares), "
                "'partner_curated' (manually added or edited), or "
                "'derived' (computed from other sources)."
            ),
        ),
        sa.Column(
            "derived_share",
            sa.Numeric(8, 6),
            nullable=True,
            comment=(
                "Production share snapshot at seed time (0.0–1.0).  Recorded "
                "so partner can see the basis for the tier assignment "
                "even if the underlying share changes in a later MCS vintage."
            ),
        ),
        sa.Column(
            "reference_year",
            sa.SmallInteger(),
            nullable=True,
            comment="Year of the derived_share snapshot.",
        ),
        sa.Column(
            "notes",
            sa.Text(),
            nullable=True,
            comment=(
                "Partner-curation notes.  Free text — methodology decisions, "
                "tier-override rationale, links to evidence."
            ),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            onupdate=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "role IN ('producer', 'consumer')",
            name="ck_country_material_relevance_role",
        ),
        sa.CheckConstraint(
            "tier IS NULL OR tier IN ('top', 'mid', 'minor', 'emerging')",
            name="ck_country_material_relevance_tier",
        ),
        sa.UniqueConstraint(
            "material_id",
            "country_code",
            "role",
            name="uq_country_material_relevance",
        ),
    )


def downgrade() -> None:
    op.drop_table("country_material_relevance")
