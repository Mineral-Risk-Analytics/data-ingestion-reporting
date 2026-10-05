"""READ-ONLY diagnostic: did the reset-#1 re-ingest recover IEA summaries?

Closes the open verification from the triage plan §7: 2,497 GTA and 407
IEA summaries were historically sliced at exactly 500 characters (a
display cap that leaked into storage). The GTA path was fixed in P0
(cap 500 → 8000) and repaired by re-ingest; the IEA re-ingest was
assumed to recover full text from the CSV, but nobody verified it.

If IEA summaries still cluster at exactly 500 characters, the IEA
ingest path still truncates and MUST be fixed before reset #2 —
otherwise the re-ingest bakes the truncation in again.

Checks every source, not just IEA, so the output doubles as a corpus
health snapshot. No writes.

Usage:
    python scripts/diagnose_iea_summaries.py
    python scripts/diagnose_iea_summaries.py --cap 500 --samples 5
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter, defaultdict

from sqlalchemy import select

from app.db.session import get_session_factory
from app.models.documents import SourceDocument
from app.models.regulatory import RiskEvent
from app.models.source import Source


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cap", type=int, default=500,
                        help="suspect truncation length (default 500)")
    parser.add_argument("--samples", type=int, default=5,
                        help="suspect rows to print per source")
    args = parser.parse_args()

    session = get_session_factory()()
    try:
        rows = session.execute(
            select(
                Source.name,
                RiskEvent.id,
                RiskEvent.title,
                RiskEvent.summary,
            )
            .join(SourceDocument, SourceDocument.id == RiskEvent.source_document_id)
            .join(Source, Source.id == SourceDocument.source_id)
        ).all()
    finally:
        session.close()

    by_source: dict[str, list] = defaultdict(list)
    for name, ev_id, title, summary in rows:
        by_source[name].append((ev_id, title or "", summary or ""))

    cap = args.cap
    print("=" * 72)
    print(f"Summary-length health by source (suspect cap = {cap} chars) — READ-ONLY")
    print("=" * 72)

    worst: list[tuple[str, int, int]] = []
    for name in sorted(by_source):
        events = by_source[name]
        lengths = [len(s) for _, _, s in events]
        n = len(lengths)
        at_cap = sum(1 for L in lengths if L == cap)
        near_cap = sum(1 for L in lengths if cap - 3 <= L <= cap)
        # mid-word cut heuristic: exactly at/near cap AND no sentence-final
        # punctuation at the end — a natural 500-char summary usually ends
        # with '.', ')' or '"'.
        midword = sum(
            1 for _, _, s in events
            if cap - 3 <= len(s) <= cap and s and s[-1] not in ".!?)\"'…"
        )
        buckets = Counter(
            "0" if L == 0 else
            "1-250" if L <= 250 else
            "251-499" if L < cap else
            f"=={cap}" if L == cap else
            f"{cap+1}-2000" if L <= 2000 else ">2000"
            for L in lengths
        )
        print(f"\n{name}  ({n} events)")
        print(f"  length buckets: " + ", ".join(f"{k}: {v}" for k, v in sorted(buckets.items())))
        print(f"  exactly at cap: {at_cap}  · within 3 of cap: {near_cap}"
              f"  · near-cap ending mid-word: {midword}")
        if midword:
            worst.append((name, midword, n))
            shown = 0
            for ev_id, title, s in events:
                if shown >= args.samples:
                    break
                if cap - 3 <= len(s) <= cap and s and s[-1] not in ".!?)\"'…":
                    print(f"    · #{ev_id} len={len(s)} …{s[-60:]!r}")
                    shown += 1

    print("\n" + "=" * 72)
    if worst:
        print("VERDICT: truncation still present — fix the ingest path(s) below "
              "BEFORE reset #2:")
        for name, midword, n in sorted(worst, key=lambda x: -x[1]):
            print(f"  {name}: {midword} of {n} summaries look cap-truncated")
    else:
        print("VERDICT: no cap-truncation signature in any source — the IEA "
              "verification item can be closed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
