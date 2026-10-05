"""READ-ONLY (DB) reset backstop: per-source event counts, snapshot + diff.

Phase 4 step 6 of the triage plan: "a silent re-ingest shortfall looks
exactly like success."  Reset #1 verified counts by hand; this makes it
mechanical for reset #2.

Three modes (never writes to the database; snapshots go to a JSON file):

  snapshot   Write current per-source counts to a JSON file.
                 python scripts/diagnose_source_counts.py snapshot \
                     --out reset2_counts_before.json
  diff       Compare the live DB against a snapshot.
                 python scripts/diagnose_source_counts.py diff \
                     --against reset2_counts_before.json
  show       Just print the current counts.

What is counted per source: total events, by triage_status, duplicate-
marked rows, verified rows, distinct gta_ids (GTA only), and events with
no source document (should stay ~126 legacy manual rows — growth means a
provenance regression).

Reading a post-reset diff: feed-backed sources should come back at
comparable totals (GTA will differ by the 2023 floor and any newly
admitted rows, e.g. the 7204 Iron Ore mapping); manual_walkthrough and
EUR-Lex must be UNCHANGED (they are preserved, not re-ingested);
operational-news counts depend on the exclude-from-delete decision.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

from sqlalchemy import select

from app.db.session import get_session_factory
from app.models.documents import SourceDocument
from app.models.regulatory import RiskEvent
from app.models.source import Source


def _collect(session) -> dict:
    rows = session.execute(
        select(
            Source.name,
            RiskEvent.id,
            RiskEvent.triage_status,
            RiskEvent.duplicate_of_id,
            RiskEvent.verified,
            RiskEvent.metadata_json,
        )
        .outerjoin(SourceDocument, SourceDocument.id == RiskEvent.source_document_id)
        .outerjoin(Source, Source.id == SourceDocument.source_id)
    ).all()

    out: dict[str, dict] = {}
    for name, _ev_id, status, dup_of, verified, meta in rows:
        key = name or "(no source document)"
        s = out.setdefault(key, {
            "total": 0, "by_status": {}, "duplicate_marked": 0,
            "verified": 0, "gta_ids": 0,
        })
        s["total"] += 1
        status_key = status or "(null)"
        s["by_status"][status_key] = s["by_status"].get(status_key, 0) + 1
        if dup_of is not None:
            s["duplicate_marked"] += 1
        if verified:
            s["verified"] += 1
        if (meta or {}).get("gta_id"):
            s["gta_ids"] += 1
    return {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "sources": dict(sorted(out.items())),
        "grand_total": sum(s["total"] for s in out.values()),
    }


def _print_counts(snap: dict) -> None:
    print(f"captured_at: {snap['captured_at']}  ·  grand total: {snap['grand_total']}")
    for name, s in snap["sources"].items():
        statuses = ", ".join(f"{k}={v}" for k, v in sorted(s["by_status"].items()))
        extras = []
        if s["duplicate_marked"]:
            extras.append(f"dup_marked={s['duplicate_marked']}")
        if s["verified"]:
            extras.append(f"verified={s['verified']}")
        if s["gta_ids"]:
            extras.append(f"gta_ids={s['gta_ids']}")
        print(f"  {name:45s} {s['total']:6d}  [{statuses}]"
              + (f"  ({'; '.join(extras)})" if extras else ""))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["snapshot", "diff", "show"])
    parser.add_argument("--out", default=None, help="snapshot output path")
    parser.add_argument("--against", default=None, help="snapshot to diff against")
    args = parser.parse_args()

    session = get_session_factory()()
    try:
        current = _collect(session)
    finally:
        session.close()

    if args.mode == "show":
        _print_counts(current)
        return 0

    if args.mode == "snapshot":
        if not args.out:
            print("snapshot mode needs --out", file=sys.stderr)
            return 2
        with open(args.out, "w") as fh:
            json.dump(current, fh, indent=2)
        _print_counts(current)
        print(f"\nsnapshot written to {args.out}")
        return 0

    # diff
    if not args.against:
        print("diff mode needs --against <snapshot.json>", file=sys.stderr)
        return 2
    with open(args.against) as fh:
        before = json.load(fh)

    print(f"BEFORE {before['captured_at']}  →  NOW {current['captured_at']}")
    print(f"grand total: {before['grand_total']} → {current['grand_total']} "
          f"({current['grand_total'] - before['grand_total']:+d})")
    names = sorted(set(before["sources"]) | set(current["sources"]))
    concerns = 0
    for name in names:
        b = before["sources"].get(name, {"total": 0, "verified": 0})
        c = current["sources"].get(name, {"total": 0, "verified": 0})
        delta = c["total"] - b["total"]
        flag = ""
        # Preserved sources must not shrink; verified rows must never drop.
        if name in ("manual_walkthrough", "EUR-Lex") and delta != 0:
            flag = "  ⚠ PRESERVED SOURCE CHANGED"
            concerns += 1
        if c.get("verified", 0) < b.get("verified", 0):
            flag += "  ⚠ VERIFIED ROWS LOST"
            concerns += 1
        if delta != 0 or flag:
            print(f"  {name:45s} {b['total']:6d} → {c['total']:6d} ({delta:+d}){flag}")
    if concerns:
        print(f"\n{concerns} concern(s) flagged — investigate before declaring "
              "the reset complete.")
        return 1
    print("\nno preservation concerns flagged.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
