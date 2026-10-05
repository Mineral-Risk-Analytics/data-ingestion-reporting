"""Offline repair pass for stored GTA risk events.  Added 2026-07-30.

Fixes three defects on rows written before the 2026-07-30 GTA ingester
changes, using only data already in the database — no GTA API key needed:

  1.  **Permalink backfill.**  Every GTA event's ``metadata_json`` gains
      ``permalink`` (``https://globaltradealert.org/intervention/{gta_id}``,
      or the state-act URL as fallback), and ``source_url`` is corrected to
      point at it.  The old ``source_url`` — the bulk-CSV download URL, the
      same value on every event and now returning 404 — is preserved under
      ``bulk_source_url``.  The materials API falls back to
      ``metadata_json['permalink']`` when ``SourceDocument.url`` is NULL, so
      this alone makes source links render.

  2.  **Identity re-key.**  ``content_hash`` is re-keyed from the legacy
      hash (title + summary + date — which made summary length part of row
      identity) to the stable ``_gta_identity_hash`` keyed on the GTA
      intervention id.  Without this, the repair re-ingest would identify
      rows only through the unindexed JSONB ``gta_id`` fallback.

  3.  **Future-date repair.**  Events dated in the future satisfy every
      trailing evidence window forever and are clamped to the *maximum*
      decay multiplier — weighted above events happening today.  Any GTA
      event with ``event_date`` in the future whose metadata carries a past
      ``effective_date`` (the announcement) is re-dated to that
      announcement; the forward date is preserved as
      ``scheduled_implementation_date`` with
      ``in_force_status = 'announced_not_yet_in_force'``.  Future-dated
      events with no past anchor are left untouched and REPORTED — they
      need a human decision, not a guess.

What this script never touches, by construction: ``verified``,
``primary_category``, ``severity_score``, ``confidence_score``,
``risk_categories_json``, ``summary``, and every material / HS / geography
link.  Summaries cannot be repaired offline — the full text was never
stored; that repair requires the API re-ingest (``ingest-gta --use-api``).

Usage (run from the repo root, same env as the app):

    python scripts/backfill_gta_quality.py --dry-run   # report only
    python scripts/backfill_gta_quality.py             # apply

Idempotent: a second run finds nothing to change.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

# Allow running as ``python scripts/backfill_gta_quality.py`` from repo root.
sys.path.insert(0, ".")

from app.db.session import get_session_factory                     # noqa: E402
from app.models.documents import SourceDocument                    # noqa: E402
from app.models.regulatory import RiskEvent                        # noqa: E402
from app.models.source import Source                               # noqa: E402
from app.services.ingestion.gta import (                           # noqa: E402
    _gta_identity_hash,
    gta_permalink,
)

GTA_SOURCE_NAME = "Global Trade Alert"


def _gta_event_ids(session: Session) -> list[int]:
    """All risk_event ids whose source document belongs to the GTA source."""
    return list(
        session.scalars(
            select(RiskEvent.id)
            .join(SourceDocument, SourceDocument.id == RiskEvent.source_document_id)
            .join(Source, Source.id == SourceDocument.source_id)
            .where(Source.name == GTA_SOURCE_NAME)
        )
    )


def backfill(session: Session, *, dry_run: bool) -> dict:
    now = datetime.now(timezone.utc)
    event_ids = _gta_event_ids(session)

    stats = {
        "gta_events": len(event_ids),
        "permalink_added": 0,
        "source_url_corrected": 0,
        "hash_rekeyed": 0,
        "redated": 0,
        "future_dated_unresolved": [],   # ids needing a human decision
        "no_gta_id": 0,
        "dry_run": dry_run,
    }

    for event_id in event_ids:
        ev = session.get(RiskEvent, event_id)
        meta = dict(ev.metadata_json or {})
        changed = False

        gta_id = meta.get("gta_id") or 0
        try:
            gta_id = int(gta_id)
        except (TypeError, ValueError):
            gta_id = 0
        state_act_id = meta.get("state_act_id")
        try:
            state_act_id = int(state_act_id) if state_act_id else None
        except (TypeError, ValueError):
            state_act_id = None

        # ── 1. Permalink ────────────────────────────────────────────────────
        permalink = gta_permalink(gta_id, state_act_id)
        if permalink is None:
            stats["no_gta_id"] += 1
        else:
            if meta.get("permalink") != permalink:
                meta["permalink"] = permalink
                changed = True
                stats["permalink_added"] += 1
            if meta.get("source_url") != permalink:
                # Preserve whatever source_url used to claim, once.
                if meta.get("source_url") and "bulk_source_url" not in meta:
                    meta["bulk_source_url"] = meta["source_url"]
                meta["source_url"] = permalink
                changed = True
                stats["source_url_corrected"] += 1

        # ── 2. Identity re-key ──────────────────────────────────────────────
        if ev.event_date is not None:
            new_hash = _gta_identity_hash(gta_id, ev.title or "", ev.event_date)
            if ev.content_hash != new_hash:
                if not dry_run:
                    ev.content_hash = new_hash
                stats["hash_rekeyed"] += 1

        # ── 3. Future-date repair ───────────────────────────────────────────
        if ev.event_date is not None and ev.event_date > now:
            effective_raw = meta.get("effective_date")
            effective = None
            if effective_raw:
                try:
                    effective = datetime.fromisoformat(str(effective_raw))
                    if effective.tzinfo is None:
                        effective = effective.replace(tzinfo=timezone.utc)
                except ValueError:
                    effective = None
            if effective is not None and effective <= now:
                meta["scheduled_implementation_date"] = ev.event_date.isoformat()
                meta["in_force_status"] = "announced_not_yet_in_force"
                changed = True
                if not dry_run:
                    ev.event_date = effective
                    # The identity hash for id-less rows depends on the date;
                    # re-key against the corrected date.
                    ev.content_hash = _gta_identity_hash(
                        gta_id, ev.title or "", effective
                    )
                stats["redated"] += 1
            else:
                stats["future_dated_unresolved"].append(
                    {
                        "id": ev.id,
                        "title": (ev.title or "")[:80],
                        "event_date": ev.event_date.isoformat(),
                    }
                )

        if changed and not dry_run:
            # Reassign rather than mutate — SQLAlchemy does not track
            # in-place mutation of a plain JSONB dict.
            ev.metadata_json = meta

    if dry_run:
        session.rollback()
    else:
        session.commit()
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would change; write nothing.",
    )
    args = parser.parse_args()

    factory = get_session_factory()
    session = factory()
    try:
        stats = backfill(session, dry_run=args.dry_run)
        print(json.dumps({"ok": True, **stats}, indent=2, default=str))
        if stats["future_dated_unresolved"]:
            print(
                f"\nWARNING: {len(stats['future_dated_unresolved'])} future-dated "
                "event(s) have no past effective_date to re-anchor to and were "
                "left untouched.  Review them by id above.",
                file=sys.stderr,
            )
    finally:
        session.close()


if __name__ == "__main__":
    main()
