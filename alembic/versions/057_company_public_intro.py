"""companies.public_intro — dedicated public profile copy.

Nicole 2026-07-15: the public company profile needs an intro paragraph,
and the question was whether to reuse ``notes``.  Decision: NO — notes
is internal working commentary and the intelligence_entities contract
deliberately never exposes it; one accidental paste of internal notes
on the public site is one too many.  ``public_intro`` is a separate
column that ONLY the partner-authored workbook (or a future admin flow)
writes, and is the ONLY free-text company field the public API returns.

NULL = no copy authored yet; the frontend renders a generated one-liner
or omits the lede block.

Revision ID: 057
Revises: 056
"""

from alembic import op
import sqlalchemy as sa

revision = "057"
down_revision = "056"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("companies", sa.Column("public_intro", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("companies", "public_intro")
