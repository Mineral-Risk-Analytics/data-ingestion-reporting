"""Add ``filing_body_sections`` table for SEC Workstream B.

Background
----------
Pre-2026-06 the SEC EDGAR ingester only consumed the submissions JSON
endpoint — filing metadata only, not filing body text.  Every
``ParsedFiling`` landed with ``is_narrative_placeholder=True`` and a
hardcoded ``narrative_excerpt`` of the form ``"<FORM> filing for <name>
(CIK <cik>)"``.  This gated off material attribution (the Haiku
classifier would have burned API budget on stub text) and blocked the
downstream facility extractor (Workstream C) entirely.

Workstream B fetches the real filing body via ``primary_document_url``
(already constructed by ``filing_parser._index_filing``), parses out
narrative sections, and stores them here so:

  * material attribution can run against Item 1A "Risk Factors" for 10-K
    and Item 3.D for 20-F (the canonical source of material mentions
    tied to risk language);
  * the planned facility extractor (Workstream C) can run against
    Item 2 "Properties" for 10-K and Item 4.D for 20-F (where facility
    name + location + capacity disclosures live);
  * the financial pressure scoring path can read Item 7 "MD&A" for
    severity calibration context.

Storage design — separate table, not a column on source_documents
-----------------------------------------------------------------
Section text is 50–500 KB per filing per section.  Putting it on
``source_documents`` would:

  * bloat every existing ``SourceDocument`` query (the scoring engine
    reads SourceDocument rows constantly and most don't need body text);
  * mix three sources of body content (regulations, news articles, SEC
    filings) under one schema even though the parsing flows differ.

A sibling table keyed by ``source_document_id`` keeps the bodies queryable
without touching the hot scoring read path.

Section code convention
-----------------------
``section_code`` is a string in the format ``<form>.<item_slug>``:

  10-K.item_1a   — Risk Factors
  10-K.item_2    — Properties (facility disclosures)
  10-K.item_7    — Management's Discussion & Analysis
  20-F.item_3d   — Risk Factors
  20-F.item_4d   — Property, Plants and Equipment
  20-F.item_5    — Operating and Financial Review
  8-K.body       — full body (deferred to v1.1)

Reserved as a hierarchical key so future extractors can target a single
form code with a LIKE pattern and add new sections without a migration.

Parser provenance
-----------------
``parser_method`` records how the section was extracted so we can
re-process when extractors improve:

  edgartools   — primary path; the ``edgartools`` Python library's
                 SEC HTML/iXBRL aware section selector
  regex        — fallback for filings edgartools couldn't parse
  llm_locator  — Haiku-as-section-finder for the long tail where both
                 heuristic paths fail (logged + flagged for review)

``parser_version`` is a free-text version string we bump when the
extractor changes meaningfully, so a backfill can target only stale
parser versions: ``WHERE parser_version != 'edgartools-2026-06-15'``.

Why an explicit ``fetched_at`` instead of relying on ``created_at``
-------------------------------------------------------------------
A re-parse of an existing fetched body should bump ``fetched_at`` only
when we actually re-hit the SEC; ``created_at`` is for row creation.
Important so the SEC rate-limit-friendly fetcher can ask "have we
already pulled this filing in the last N days?" without an extra column.

Revision ID: 049
Revises:     048
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "049"
down_revision: Union[str, None] = "048"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "filing_body_sections",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "source_document_id",
            sa.Integer(),
            sa.ForeignKey("source_documents.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
            comment=(
                "FK to source_documents.  Multiple sections per filing are "
                "expected (Item 1A + Item 2 + Item 7 for 10-K).  Cascade "
                "delete so dropping a SourceDocument cleans up bodies."
            ),
        ),
        sa.Column(
            "section_code",
            sa.String(32),
            nullable=False,
            index=True,
            comment=(
                "Form + section identifier in `<form>.<item_slug>` form.  "
                "Examples: '10-K.item_1a', '10-K.item_2', '10-K.item_7', "
                "'20-F.item_3d', '20-F.item_4d', '20-F.item_5'.  Indexed so "
                "filter queries like WHERE section_code = '10-K.item_2' for "
                "the facility extractor are fast."
            ),
        ),
        sa.Column(
            "section_text",
            sa.Text(),
            nullable=False,
            comment=(
                "Parsed section text content.  Typical size 5KB-500KB.  "
                "Whitespace-normalised but not stripped of HTML entities — "
                "downstream consumers should expect inline markers like "
                "'&nbsp;' may persist depending on parser path."
            ),
        ),
        sa.Column(
            "char_count",
            sa.Integer(),
            nullable=False,
            comment=(
                "len(section_text) at parse time.  Stored to enable size "
                "diagnostics without scanning the text column."
            ),
        ),
        sa.Column(
            "parser_method",
            sa.String(32),
            nullable=False,
            comment=(
                "Which extractor produced this row.  One of: "
                "'edgartools' (primary HTML/iXBRL-aware path), "
                "'regex' (heuristic fallback for plain HTML), "
                "'llm_locator' (Haiku-driven section finder for the "
                "long tail).  Drives re-processing scope when an "
                "extractor improves."
            ),
        ),
        sa.Column(
            "parser_version",
            sa.String(64),
            nullable=False,
            comment=(
                "Free-text version of the extractor, bumped manually on "
                "meaningful changes.  Backfills can target stale versions "
                "via WHERE parser_version != '<current>'."
            ),
        ),
        sa.Column(
            "fetched_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment=(
                "When the underlying primary_document_url was last fetched "
                "from SEC.  Distinct from ``created_at`` so re-parse passes "
                "(no fetch) don't shift it.  Drives the fetcher's "
                "'skip if fetched in last N days' check."
            ),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "source_document_id",
            "section_code",
            name="uq_filing_body_section",
        ),
    )


def downgrade() -> None:
    op.drop_table("filing_body_sections")
