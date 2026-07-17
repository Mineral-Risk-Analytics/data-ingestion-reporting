"""Public-profile columns on companies: slug + is_published.

Nicole 2026-07-14: the Intelligence Hub gains public Companies pages
(browse + profile, mocked in the Mineral Risk Analytics design system).
Two gaps in the companies table blocked them:

``slug``
    URL-safe public identifier ('ganfeng-lithium').  canonical_name is
    the internal identity; exposing it in URLs means percent-encoding
    and renames breaking links.  Backfilled here from canonical_name
    (lowercase, non-alphanumeric runs → '-'); collisions get '-<id8>'
    appended (first 8 hex of the uuid) — deterministic and unique.

``is_published``
    THE public-visibility gate.  The companies table contains auto-
    created facility operators, JV vehicles, and unreviewed shells that
    must never appear on the public site.  Semantics:

      * default FALSE for every existing and future row
      * seed_company_workbook.py sets TRUE for workbook rows with
        publish=TRUE (the partner-review boundary — same flag that
        gates ingestion depth)
      * the loader NEVER sets it back to FALSE — unpublishing is a
        deliberate manual/admin action, not a side effect of a
        workbook re-run

    Public endpoints filter on it; internal/admin endpoints ignore it.

Revision ID: 054
Revises: 053
"""

from __future__ import annotations

from typing import Union

import sqlalchemy as sa

from alembic import op

revision: str = "054"
down_revision: Union[str, None] = "053"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("companies", sa.Column(
        "slug", sa.String(160), nullable=True,
        comment="URL-safe public identifier, backfilled from canonical_name.",
    ))
    op.add_column("companies", sa.Column(
        "is_published", sa.Boolean(), nullable=False, server_default="false",
        comment=(
            "Public-site visibility gate. TRUE only for partner-reviewed "
            "companies (workbook publish flag). Loader never un-sets it."
        ),
    ))

    # Backfill slugs: lowercase, strip accents-ish via ascii fold is not
    # available without unaccent; keep it simple — non-alphanumeric runs
    # become '-', trimmed.  Collisions (rare: short names) get a uuid8
    # suffix in a second pass.
    op.execute(
        """
        UPDATE companies
        SET slug = trim(both '-' from
            regexp_replace(lower(canonical_name), '[^a-z0-9]+', '-', 'g'))
        WHERE slug IS NULL
        """
    )
    op.execute(
        """
        UPDATE companies c
        SET slug = c.slug || '-' || substr(replace(c.id::text, '-', ''), 1, 8)
        WHERE EXISTS (
            SELECT 1 FROM companies o
            WHERE o.slug = c.slug AND o.id <> c.id
        )
        AND c.id <> (
            SELECT min(o2.id::text)::uuid FROM companies o2 WHERE o2.slug = c.slug
        )
        """
    )
    op.create_index("ix_companies_slug", "companies", ["slug"], unique=True)
    op.create_index("ix_companies_is_published", "companies", ["is_published"])


def downgrade() -> None:
    op.drop_index("ix_companies_is_published", table_name="companies")
    op.drop_index("ix_companies_slug", table_name="companies")
    op.drop_column("companies", "is_published")
    op.drop_column("companies", "slug")
