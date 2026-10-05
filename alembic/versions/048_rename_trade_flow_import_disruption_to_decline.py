"""Rename ``event_subtype='IMPORT_DISRUPTION'`` to ``'IMPORT_DECLINE'`` for
trade-flow-inferred events.

Background
----------
9.3.B audit (2026-06): ``trade_signal_builder.py`` previously emitted
``event_subtype='IMPORT_DISRUPTION'`` for YoY import declines.  But
the same subtype is also emitted by ``gta.py`` for directly-observed
"Import tariff/quota/ban" policy interventions, AND
``hs_node_scorer._TARIFF_SUBTYPES`` routes IMPORT_DISRUPTION events to
the ``tariff_exposure`` Geopolitical sub-input.

Sharing the subtype meant our volume-decline-inferred signals were
contaminating the tariff exposure score.  A 30% YoY drop in a country's
imports could be a tariff — OR demand collapse, OR substitution, OR
inventory drawdown, OR currency moves.  Treating any of those as
"tariff exposure" overstates what we know.

The trade_signal_builder code now emits the new subtype
``'IMPORT_DECLINE'`` for its inferred declines.  ``hs_node_scorer``
keeps ``_TARIFF_SUBTYPES = {"TARIFF", "IMPORT_DISRUPTION"}`` —
deliberately NOT including IMPORT_DECLINE — so volume-drop noise is
removed from tariff scoring while GTA's policy events remain correctly
wired.  This fixes a pre-existing scoring bug.

This migration
--------------
Rewrites historical rows that were emitted by ``trade_signal_builder``
to the new subtype.  Same approach as migration 047 (for the export-side
EXPORT_RESTRICTION → EXPORT_DECLINE rename): identify rows by
``metadata_json->>'source' = 'trade_flow_analysis'`` and recompute the
parameter-stable hash so the next ``build_trade_signals`` UPSERT hits
the existing rows rather than inserting duplicates.

Scoring impact note
-------------------
Tariff exposure scores derived from existing IMPORT_DISRUPTION rows
that came from trade_signal_builder will decrease after this migration —
those rows lose their contribution to ``tariff_exposure`` once renamed
to IMPORT_DECLINE.  This is the bug-fix direction; the previous numbers
were inflated.  Run ``rescore-all`` after the migration completes.

Idempotency
-----------
Re-running this migration after a successful first run is a no-op: the
WHERE clause matches only ``IMPORT_DISRUPTION`` rows with
``source='trade_flow_analysis'``.  After the first run those rows
become ``IMPORT_DECLINE`` and stop matching.

Revision ID: 048
Revises:     047
"""

from __future__ import annotations

import hashlib
import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "048"
down_revision: Union[str, None] = "047"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _content_hash(stable_key: str) -> str:
    return hashlib.sha256(stable_key.encode()).hexdigest()


def upgrade() -> None:
    bind = op.get_bind()

    candidates = bind.execute(
        sa.text(
            """
            SELECT
                id,
                metadata_json
            FROM risk_events
            WHERE event_subtype = 'IMPORT_DISRUPTION'
              AND metadata_json ->> 'source' = 'trade_flow_analysis'
            """
        )
    ).fetchall()

    if not candidates:
        return

    updates: list[dict] = []
    skipped_missing_metadata = 0

    for row in candidates:
        row_id = row[0]
        meta = row[1] or {}

        material_id = meta.get("material_id")
        reporter_country = meta.get("reporter_country")
        year = meta.get("reference_year")

        if material_id is None or not reporter_country or year is None:
            skipped_missing_metadata += 1
            continue

        # Mirror ``trade_signal_builder._stable_event_key`` — must stay in sync.
        new_stable_key = (
            f"IMPORT_DECLINE|material_id={material_id}"
            f"|country={reporter_country}|year={year}"
        )
        new_hash = _content_hash(new_stable_key)

        new_meta = dict(meta)
        new_meta["event_subtype"] = "IMPORT_DECLINE"

        updates.append(
            {
                "row_id": row_id,
                "new_hash": new_hash,
                "new_meta": new_meta,
            }
        )

    for upd in updates:
        bind.execute(
            sa.text(
                """
                UPDATE risk_events
                SET event_subtype = 'IMPORT_DECLINE',
                    content_hash = :new_hash,
                    metadata_json = CAST(:new_meta AS JSONB)
                WHERE id = :row_id
                """
            ),
            {
                "row_id": upd["row_id"],
                "new_hash": upd["new_hash"],
                "new_meta": json.dumps(upd["new_meta"]),
            },
        )


def downgrade() -> None:
    """Revert IMPORT_DECLINE rows back to IMPORT_DISRUPTION."""
    bind = op.get_bind()

    candidates = bind.execute(
        sa.text(
            """
            SELECT id, metadata_json
            FROM risk_events
            WHERE event_subtype = 'IMPORT_DECLINE'
              AND metadata_json ->> 'source' = 'trade_flow_analysis'
            """
        )
    ).fetchall()

    if not candidates:
        return

    for row in candidates:
        row_id = row[0]
        meta = row[1] or {}

        material_id = meta.get("material_id")
        reporter_country = meta.get("reporter_country")
        year = meta.get("reference_year")
        if material_id is None or not reporter_country or year is None:
            continue

        new_stable_key = (
            f"IMPORT_DISRUPTION|material_id={material_id}"
            f"|country={reporter_country}|year={year}"
        )
        new_hash = _content_hash(new_stable_key)

        new_meta = dict(meta)
        new_meta["event_subtype"] = "IMPORT_DISRUPTION"

        bind.execute(
            sa.text(
                """
                UPDATE risk_events
                SET event_subtype = 'IMPORT_DISRUPTION',
                    content_hash = :new_hash,
                    metadata_json = CAST(:new_meta AS JSONB)
                WHERE id = :row_id
                """
            ),
            {
                "row_id": row_id,
                "new_hash": new_hash,
                "new_meta": json.dumps(new_meta),
            },
        )
