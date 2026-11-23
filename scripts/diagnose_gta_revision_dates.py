"""READ-ONLY diagnostic: do GTA revisions shift event dates on re-ingest?

Pre-reset-#2 verification (triage plan fix batch, suspicion recorded
2026-08-04): when GTA amends an old measure, the API row may come back
with changed ``date_implemented`` / ``date_announced``, so a
delete-and-re-ingest would re-date old measures to amendment vintage —
and every evidence window is trailing, so a re-dated 2019 measure would
decay as if it happened this year.

Method (no writes anywhere):
  1. Load stored GTA events (source "Global Trade Alert") with their
     ``metadata_json->gta_id`` and stored ``event_date``.
  2. Fetch the same universe from the live API (same HS filter and
     evaluation gate the ingester uses), windowed by --since/--until.
  3. Join on intervention id and recompute the event date with the
     ingester's own ``_resolve_event_date`` on today's API payload.
  4. Report drift: stored vs recomputed, alongside date_announced /
     date_implemented / last_updated / latest_action_date so drift can
     be attributed to revisions.

Requires GTA_API_KEY in the environment (run on Nicole's machine).

Usage:
    python scripts/diagnose_gta_revision_dates.py --since 2023-01-01
    python scripts/diagnose_gta_revision_dates.py --since 2023-01-01 \
        --csv-out gta_date_drift.csv --max-pages 10
"""
from __future__ import annotations

import argparse
import csv
import sys
from datetime import date, datetime, timezone

from sqlalchemy import select

from app.core.config import get_settings
from app.db.session import get_session_factory
from app.models.documents import SourceDocument
from app.models.regulatory import RiskEvent
from app.models.source import Source
from app.services.ingestion.gta import (
    _derive_hs_codes_for_api_from_db,
    _parse_date,
    _resolve_event_date,
    fetch_gta_interventions_api,
)

_GTA_SOURCE_NAME = "Global Trade Alert"


def _load_stored(session) -> dict[int, dict]:
    """{gta_id: {event_id, event_date, title, triage_status}} for stored GTA events."""
    rows = session.execute(
        select(RiskEvent)
        .join(SourceDocument, SourceDocument.id == RiskEvent.source_document_id)
        .join(Source, Source.id == SourceDocument.source_id)
        .where(Source.name == _GTA_SOURCE_NAME)
    ).scalars()
    out: dict[int, dict] = {}
    for ev in rows:
        meta = ev.metadata_json or {}
        gta_id = meta.get("gta_id")
        try:
            gta_id = int(gta_id)
        except (TypeError, ValueError):
            continue
        if not gta_id:
            continue
        # Duplicate gta_ids should not exist post-P0 (identity is keyed on
        # gta_id) — if they do, keep the first and count the rest.
        if gta_id in out:
            out[gta_id]["dup_rows"] += 1
            continue
        out[gta_id] = {
            "event_id": ev.id,
            "event_date": ev.event_date,
            "title": ev.title,
            "triage_status": getattr(ev, "triage_status", None),
            "dup_rows": 0,
        }
    return out


def _dt(raw) -> datetime | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, datetime):
        return raw
    return _parse_date(str(raw))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", default="2023-01-01",
                        help="announcement_period start (default: the reset's 2023 floor)")
    parser.add_argument("--until", default=None,
                        help="announcement_period end (default: today)")
    parser.add_argument("--max-pages", type=int, default=None,
                        help="cap API pages (1000 rows/page) for a quick sample run")
    parser.add_argument("--csv-out", default=None,
                        help="write per-row drift detail to this CSV")
    parser.add_argument("--drift-days", type=int, default=30,
                        help="report rows whose recomputed date moved by more than this (default 30)")
    args = parser.parse_args()

    settings = get_settings()
    if not settings.gta_api_key:
        print("GTA_API_KEY not configured — this diagnostic needs the API. "
              "Run on the machine that holds the key.", file=sys.stderr)
        return 2

    since = date.fromisoformat(args.since)
    until = date.fromisoformat(args.until) if args.until else None

    session = get_session_factory()()
    try:
        stored = _load_stored(session)
        # F-GTA-API-1: the API matches FULL HS codes only — 4-digit prefixes
        # return zero results.  Derive the 6-digit universe exactly the way
        # the real ingest does (from hs_code_material_mappings), so this
        # diagnostic queries the same universe a reset re-ingest would.
        api_hs_codes = _derive_hs_codes_for_api_from_db(session)
    finally:
        session.close()
    print(f"stored GTA events with gta_id: {len(stored)}", file=sys.stderr)
    print(f"derived 6-digit HS codes for API filter: {len(api_hs_codes)}", file=sys.stderr)
    dup_rows = sum(v["dup_rows"] for v in stored.values())
    if dup_rows:
        print(f"WARNING: {dup_rows} stored rows share a gta_id with another row "
              f"(identity should be unique post-P0)", file=sys.stderr)

    print(f"fetching API window {since} → {until or 'today'} "
          f"({len(api_hs_codes)} HS codes)…", file=sys.stderr)
    interventions = fetch_gta_interventions_api(
        settings.gta_api_key,
        hs_prefixes=api_hs_codes,
        since_date=since,
        until_date=until,
        max_pages=args.max_pages,
    )
    print(f"API returned {len(interventions)} interventions", file=sys.stderr)

    now = datetime.now(timezone.utc)
    matched = 0
    drifted: list[dict] = []
    unresolvable = 0
    revised_count = 0

    for iv in interventions:
        gta_id = iv.get("intervention_id")
        try:
            gta_id = int(gta_id)
        except (TypeError, ValueError):
            continue
        row = stored.get(gta_id)
        if row is None:
            continue
        matched += 1

        d_impl = _dt(iv.get("date_implemented"))
        d_ann = _dt(iv.get("date_announced"))
        recomputed, _sched = _resolve_event_date(d_impl, d_ann, now=now)
        last_updated = _dt(iv.get("last_updated"))
        latest_action = _dt(iv.get("latest_action_date"))

        stored_dt = row["event_date"]
        # normalise tz-naive stored datetimes for comparison
        if stored_dt is not None and stored_dt.tzinfo is None:
            stored_dt = stored_dt.replace(tzinfo=timezone.utc)
        if recomputed is not None and recomputed.tzinfo is None:
            recomputed = recomputed.replace(tzinfo=timezone.utc)

        if recomputed is None:
            unresolvable += 1
            continue

        drift_days = (abs((recomputed - stored_dt).days)
                      if stored_dt is not None else None)
        was_revised = bool(
            latest_action and d_ann and latest_action.date() > d_ann.date()
        )
        if was_revised:
            revised_count += 1

        if drift_days is None or drift_days > args.drift_days:
            drifted.append({
                "gta_id": gta_id,
                "event_id": row["event_id"],
                "triage_status": row["triage_status"],
                "stored_event_date": stored_dt.date().isoformat() if stored_dt else "",
                "recomputed_event_date": recomputed.date().isoformat(),
                "drift_days": drift_days if drift_days is not None else "",
                "date_announced": d_ann.date().isoformat() if d_ann else "",
                "date_implemented": d_impl.date().isoformat() if d_impl else "",
                "last_updated": last_updated.date().isoformat() if last_updated else "",
                "latest_action_date": latest_action.date().isoformat() if latest_action else "",
                "revision_after_announcement": was_revised,
                "title": (row["title"] or "")[:120],
            })

    print("\n" + "=" * 68)
    print("GTA revision-date drift diagnostic (READ-ONLY)")
    print("=" * 68)
    print(f"  stored GTA events with gta_id       : {len(stored)}")
    print(f"  API interventions returned          : {len(interventions)}")
    print(f"  stored events matched to API window : {matched}")

    if matched == 0:
        # An empty join proves NOTHING about drift — refuse to conclude.
        print("\n  VERDICT: INCONCLUSIVE — the join connected zero rows, so no")
        print("  drift claim can be made either way.  Debug info:")
        sample_stored = list(stored.keys())[:5]
        sample_api = [iv.get("intervention_id") for iv in interventions[:5]]
        print(f"    sample stored gta_ids : {sample_stored}")
        print(f"    sample API ids        : {sample_api}")
        if len(interventions) == 0:
            print("    API returned nothing — check the HS-code filter and the")
            print("    --since window (the API needs full 6-digit codes; this")
            print("    script now derives them from the DB like the ingester).")
        elif len(stored) == 0:
            print("    No stored GTA events carried metadata_json.gta_id — check")
            print("    the source name join ('Global Trade Alert').")
        return 3
    print(f"  interventions with post-announcement revisions: {revised_count}")
    print(f"  rows whose RECOMPUTED date moved > {args.drift_days}d vs stored: {len(drifted)}")
    print(f"  rows now unresolvable (both dates future): {unresolvable}")
    if drifted:
        drift_vals = [d["drift_days"] for d in drifted if isinstance(d["drift_days"], int)]
        if drift_vals:
            drift_vals.sort()
            print(f"  drift days — min {drift_vals[0]}, median "
                  f"{drift_vals[len(drift_vals)//2]}, max {drift_vals[-1]}")
        big = [d for d in drifted if isinstance(d["drift_days"], int) and d["drift_days"] > 365]
        print(f"  rows moving more than a YEAR (decay-relevant): {len(big)}")
        rev = [d for d in drifted if d["revision_after_announcement"]]
        print(f"  drifted rows that were revised after announcement: {len(rev)} "
              f"(these confirm the suspicion if high)")
        print("\n  worst 10 by drift:")
        for d in sorted(drifted, key=lambda x: -(x["drift_days"] or 0))[:10]:
            print(f"    gta {d['gta_id']} ev {d['event_id']}: stored "
                  f"{d['stored_event_date']} → recomputed {d['recomputed_event_date']} "
                  f"({d['drift_days']}d) revised={d['revision_after_announcement']} "
                  f"| {d['title'][:60]}")
    else:
        print(f"  → no material drift across {matched} matched rows: a re-ingest "
              "would reproduce stored dates. Suspicion NOT confirmed for this window.")

    if args.csv_out and drifted:
        with open(args.csv_out, "w", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(drifted[0].keys()))
            writer.writeheader()
            writer.writerows(drifted)
        print(f"\n  detail written to {args.csv_out}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
