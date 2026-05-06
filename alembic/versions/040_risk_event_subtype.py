"""Promote ``event_subtype`` from ``metadata_json`` to a typed column.

Background
----------
``RiskEvent.event_type`` was meant to carry the precise classification used
by ``hs_node_scorer`` filters (``"tariff_increase"``, ``"export_restriction"``,
…), but every ingester puts that classification into
``metadata_json["event_subtype"]`` instead and writes a generic, ingester-
specific string in ``event_type`` (``"GEOPOLITICAL_TRADE"``,
``"federal_register_notice"``, ``"sanctions_listing"``, the literal GTA
intervention name, etc.).

Result before this migration: ``hs_node_scorer``'s ``RiskEvent.event_type
.in_({"tariff", "tariff_increase", ...})`` filter never matched anything,
so ``tariff_exposure`` and ``export_restriction`` sub-scores were
permanently 0 for every HS node.  See ``docs/scoring-audit-2026-05-addendum.md``
G3 for the full diagnosis.

This migration
--------------
Adds ``risk_events.event_subtype VARCHAR(64) NULL`` and backfills it from
``metadata_json->>'event_subtype'`` for existing rows.  A partial index on
the column (WHERE NOT NULL) supports the scorer's ``IN (...)`` filter.

Canonical taxonomy currently emitted by ingesters
-------------------------------------------------
    TARIFF                — duties / tariff increases / threats
    EXPORT_RESTRICTION    — export bans, quotas, licensing requirements,
                            export taxes
    IMPORT_DISRUPTION     — import tariffs, quotas, bans (demand-side)
    TRADE_CONCENTRATION   — derived from trade flow concentration
    REGULATORY_COMPLIANCE — compliance / due-diligence regulations
    TRADE_POLICY          — broader trade policy events without a specific
                            measure (USTR notices, etc.)

The list is intentionally not enforced as a CHECK constraint or DB enum —
adding new subtypes shouldn't require a migration.  Document new values in
this file's docstring and in the model column comment.

Forward compatibility
---------------------
``metadata_json["event_subtype"]`` is intentionally NOT removed by this
migration.  Live readers will be migrated to the typed column in the same
PR, but leaving the JSON copy for one release cycle protects against any
straggling reader we missed (and any partner-side analytics that may have
been built on the JSON shape).  A future migration can drop the JSON copy
once we've confirmed nothing in the broader stack still reads it.

Revision ID: 040
Revises:     039
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "040"
down_revision: Union[str, None] = "039"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "risk_events",
        sa.Column(
            "event_subtype",
            sa.String(64),
            nullable=True,
            comment=(
                "Precise event classification, distinct from the "
                "ingester-specific ``event_type``. Canonical values: "
                "TARIFF, EXPORT_RESTRICTION, IMPORT_DISRUPTION, "
                "TRADE_CONCENTRATION, REGULATORY_COMPLIANCE, "
                "TRADE_POLICY. NULL for events whose ingester does not "
                "yet emit a subtype (sanctions, generic regulatory "
                "implementation events). Read by hs_node_scorer "
                "(tariff_exposure / export_restriction sub-scores) and "
                "the geopolitical / regulatory aggregators."
            ),
        ),
    )

    # ── Backfill from metadata_json blob ─────────────────────────────────
    # Idempotent: only writes where the typed column is NULL and the JSON
    # blob has a value.  Re-running is a no-op.
    op.execute(
        """
        UPDATE risk_events
        SET event_subtype = (metadata_json->>'event_subtype')
        WHERE event_subtype IS NULL
          AND metadata_json ? 'event_subtype'
          AND length(metadata_json->>'event_subtype') > 0
        """
    )

    # ── Partial index for hs_node_scorer's IN (...) filter ───────────────
    # WHERE event_subtype IS NOT NULL keeps the index small — most events
    # will have NULL until the new ingester writes ship.
    op.create_index(
        "ix_risk_events_event_subtype",
        "risk_events",
        ["event_subtype"],
        postgresql_where=sa.text("event_subtype IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_risk_events_event_subtype", table_name="risk_events")
    op.drop_column("risk_events", "event_subtype")
