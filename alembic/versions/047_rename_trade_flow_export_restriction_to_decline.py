"""Rename ``event_subtype='EXPORT_RESTRICTION'`` to ``'EXPORT_DECLINE'`` for
trade-flow-inferred events.

Background
----------
9.2.E audit (2026-06): ``trade_signal_builder.py`` previously emitted
``event_subtype='EXPORT_RESTRICTION'`` for YoY export declines >= 20%.
But the same subtype is also emitted by directly-observed restriction
sources — ``gta.py`` (Global Trade Alert tracked export
taxes/quotas/bans), ``opensanctions.py`` (sanctions designations), and
``ingest_federal_register.py`` (FR documents announcing US export
restrictions).  Conflating the two meant the partner-facing methodology
could not distinguish "we observed a policy restriction" from "we
inferred a decline from volume data."

The trade_signal_builder code now emits the new subtype
``'EXPORT_DECLINE'`` for its inferred declines.  ``evidence_aggregator.py``
was updated in the same commit to match both subtypes for the
``export_restriction_exposure`` Geopolitical-pillar sub-input, so
scoring routing is preserved (a future calibration pass may weight
EXPORT_DECLINE lower than EXPORT_RESTRICTION, since inferred < observed).

This migration
--------------
Rewrites historical rows that were emitted by ``trade_signal_builder``
to the new subtype.  We identify those rows by
``metadata_json->>'source' = 'trade_flow_analysis'`` — the canonical
tag the builder writes for every event it emits.

``content_hash`` is also rewritten so the next
``trade_signal_builder.build_trade_signals`` run hits the existing rows
on its UPSERT path rather than inserting duplicates.  The hash is
SHA-256 of the parameter-stable key
``f"{event_subtype}|material_id={mid}|country={cc}|year={yr}"``; with
``event_subtype`` changing from EXPORT_RESTRICTION → EXPORT_DECLINE the
hash flips deterministically.

Idempotency
-----------
Re-running this migration after a successful first run is a no-op:
the WHERE clause matches only ``EXPORT_RESTRICTION`` rows with
``source='trade_flow_analysis'``.  After the first run those rows
become ``EXPORT_DECLINE`` and stop matching, so subsequent passes
update zero rows.

Revision ID: 047
Revises:     046
"""

from __future__ import annotations

import hashlib
import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "047"
down_revision: Union[str, None] = "046"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _content_hash(stable_key: str) -> str:
    return hashlib.sha256(stable_key.encode()).hexdigest()


def upgrade() -> None:
    bind = op.get_bind()

    # Fetch candidate rows.  We need each row's (material_id, reporter_country,
    # year) so we can recompute the hash with the new event_subtype.  These
    # are all present in metadata_json on rows the trade_signal_builder
    # wrote, since it sets them on every event.
    candidates = bind.execute(
        sa.text(
            """
            SELECT
                id,
                metadata_json
            FROM risk_events
            WHERE event_subtype = 'EXPORT_RESTRICTION'
              AND metadata_json ->> 'source' = 'trade_flow_analysis'
            """
        )
    ).fetchall()

    if not candidates:
        # Nothing to migrate — either no historical rows, or migration was
        # already run.  Either is a no-op success.
        return

    updates: list[dict] = []
    skipped_missing_metadata = 0

    for row in candidates:
        row_id = row[0]
        meta = row[1] or {}

        material_id = meta.get("material_id")
        reporter_country = meta.get("reporter_country")
        year = meta.get("reference_year")

        # Defensive — if the metadata is malformed, skip rather than
        # corrupt the hash.  These rows can be manually inspected after
        # the migration via:
        #   SELECT id, metadata_json FROM risk_events
        #   WHERE event_subtype = 'EXPORT_RESTRICTION'
        #     AND metadata_json ->> 'source' = 'trade_flow_analysis';
        if material_id is None or not reporter_country or year is None:
            skipped_missing_metadata += 1
            continue

        # Mirror the stable-key construction in
        # ``trade_signal_builder._stable_event_key``.  Must stay in sync.
        new_stable_key = (
            f"EXPORT_DECLINE|material_id={material_id}"
            f"|country={reporter_country}|year={year}"
        )
        new_hash = _content_hash(new_stable_key)

        # Also refresh the metadata_json copy of event_subtype that the
        # trade_signal_builder writes (kept for backwards compatibility
        # while the typed column rollout completes).
        new_meta = dict(meta)
        new_meta["event_subtype"] = "EXPORT_DECLINE"

        updates.append(
            {
                "row_id": row_id,
                "new_hash": new_hash,
                "new_meta": new_meta,
            }
        )

    # Apply updates row-by-row.  Bulk UPDATE is awkward here because
    # each row's hash depends on its own (material, country, year)
    # tuple — there's no single SQL expression that produces the new
    # hash from the existing columns.
    for upd in updates:
        bind.execute(
            sa.text(
                """
                UPDATE risk_events
                SET event_subtype = 'EXPORT_DECLINE',
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
    """Revert EXPORT_DECLINE rows back to EXPORT_RESTRICTION.

    Symmetric to upgrade: same rows, same hash-recomputation logic, but
    with the subtype names swapped.  Only touches rows we previously
    migrated (matched on source='trade_flow_analysis').
    """
    bind = op.get_bind()

    candidates = bind.execute(
        sa.text(
            """
            SELECT id, metadata_json
            FROM risk_events
            WHERE event_subtype = 'EXPORT_DECLINE'
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
            f"EXPORT_RESTRICTION|material_id={material_id}"
            f"|country={reporter_country}|year={year}"
        )
        new_hash = _content_hash(new_stable_key)

        new_meta = dict(meta)
        new_meta["event_subtype"] = "EXPORT_RESTRICTION"

        bind.execute(
            sa.text(
                """
                UPDATE risk_events
                SET event_subtype = 'EXPORT_RESTRICTION',
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
