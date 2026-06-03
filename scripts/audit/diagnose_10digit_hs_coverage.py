"""Diagnose the N5 question: does 10-digit HTS coverage carry unique info?

Run against the live dev DB:

    cd /path/to/battery-data-intelligence-engine
    python "Automotive Data Solutions/diagnose_10digit_hs_coverage.py"

Walks every 10-digit row in ``hs_code_material_mappings`` and compares:

  Direct 10-digit resolution
    What ``_resolve_material_id`` returns when handed the full 10-digit
    string — i.e., what current scoring sees for any HS code that exactly
    matches this prefix.

  6-digit-only resolution
    What ``_resolve_material_id`` returns when handed only the first 6
    digits of the prefix.  This simulates "what would scoring resolve
    to if the 10-digit row didn't exist?" — Pass 1 looks for a 6-digit
    exact match, Pass 2 falls back to the 4-digit prefix.

Categorises every 10-digit row into one of four buckets:

  REDUNDANT
    6-digit resolution returns the same material at the same (or higher)
    confidence.  The 10-digit row adds no information; removing it would
    not change scoring.

  UNIQUE_ATTRIBUTION
    6-digit resolution returns a *different* material.  The 10-digit row
    carries unique information — without it, scoring would attribute to
    the wrong material (or to a different one, depending on perspective).

  RECOVERS_FROM_AMBIGUITY
    6-digit resolution returns None (no exact match, OR 4-digit fallback
    hit tied-confidence and returned None).  The 10-digit row is the
    only path to a resolved attribution — removing it would leave rows
    unresolved.

  CONFIDENCE_DELTA_ONLY
    Same material at different confidence.  Informational; the 10-digit
    row tightens (or loosens) the confidence on the attribution but does
    not change which material is selected.

Output: prints a summary table to stdout, plus writes two CSVs to the
working directory:

  ten_digit_summary.csv          per-bucket counts
  ten_digit_samples.csv          up to 100 sample rows from each non-
                                 REDUNDANT bucket for inspection

These are decision inputs for the N5 ticket (10-digit HTS coverage
strategy).  Read the bucket distribution and the sample CSV before
deciding whether to keep, expand, or strip 10-digit coverage.
"""

from __future__ import annotations

import csv
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Optional

# Make `app.*` importable when running this script standalone
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def main() -> int:
    # ── Imports done inside main so import errors surface cleanly ─────
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from app.db.session import get_session_factory
    from app.models.supply import HsCodeMaterialMapping, Material
    from app.services.ingestion.comtrade import (
        _build_hs_material_map,
        _resolve_material_id,
    )

    OUT_DIR = Path(__file__).resolve().parent

    print("=" * 64)
    print("N5 diagnostic — 10-digit HTS vs 6-digit-prefix resolution")
    print("=" * 64)

    SessionLocal = get_session_factory()
    s: Session = SessionLocal()
    try:
        # Build the full hs_material_map (mirrors what the resolver sees)
        hs_map = _build_hs_material_map(s)
        if not hs_map:
            print(
                "\nhs_code_material_mappings is empty. Run "
                "`bdi-ingest seed-hs-mappings` first.",
                file=sys.stderr,
            )
            return 1

        # Material lookup for human-readable output
        materials = {
            m.id: m.canonical_name
            for m in s.scalars(select(Material)).all()
        }

        # All 10-digit mapping rows
        ten_digit_rows = s.scalars(
            select(HsCodeMaterialMapping)
            .where(HsCodeMaterialMapping.digit_count == 10)
        ).all()

        # Some installs may store 10-digit prefixes without setting
        # digit_count=10; treat any prefix string of length 10 as a
        # 10-digit row too.  Union the two without double-counting.
        ten_digit_by_length = s.scalars(
            select(HsCodeMaterialMapping)
            .where(HsCodeMaterialMapping.hs_code_prefix.regexp_match(r"^\d{10}$"))
        ).all()
        all_ten_digit = {r.id: r for r in ten_digit_rows}
        for r in ten_digit_by_length:
            all_ten_digit.setdefault(r.id, r)
        all_ten_digit_list = list(all_ten_digit.values())

        if not all_ten_digit_list:
            print(
                "\nNo 10-digit rows present in hs_code_material_mappings.\n"
                "Either the seed has no 10-digit coverage yet, or the\n"
                "USGS MCS PDF parser hasn't been run.  If the latter:\n"
                "  bdi-ingest ingest-mcs-pdf\n"
                "Re-run this diagnostic afterwards.\n"
            )
            return 0

        print(f"\nFound {len(all_ten_digit_list)} 10-digit rows to evaluate.\n")

        # Bucket counters and samples
        buckets: dict[str, list[dict]] = {
            "REDUNDANT": [],
            "UNIQUE_ATTRIBUTION": [],
            "RECOVERS_FROM_AMBIGUITY": [],
            "CONFIDENCE_DELTA_ONLY": [],
        }
        market_scopes: Counter[str] = Counter()

        for row in all_ten_digit_list:
            ten_digit = row.hs_code_prefix
            six_digit = ten_digit[:6]
            direct_material = row.material_id
            direct_confidence = float(row.confidence or 0.0)

            # 6-digit-only resolution
            six_mid, six_hs_id, six_conf = _resolve_material_id(
                six_digit, hs_map
            )

            entry = {
                "ten_digit_prefix": ten_digit,
                "six_digit_prefix": six_digit,
                "direct_material_id": direct_material,
                "direct_material_name": materials.get(direct_material, "<unknown>"),
                "direct_confidence": direct_confidence,
                "six_digit_material_id": six_mid,
                "six_digit_material_name": (
                    materials.get(six_mid, "<unknown>") if six_mid is not None else None
                ),
                "six_digit_confidence": six_conf,
                "market_scope": row.market_scope,
                "supply_chain_stage": row.supply_chain_stage,
                "description": (row.description or "")[:200],
            }
            market_scopes[row.market_scope] += 1

            if six_mid is None:
                buckets["RECOVERS_FROM_AMBIGUITY"].append(entry)
            elif six_mid != direct_material:
                buckets["UNIQUE_ATTRIBUTION"].append(entry)
            elif six_conf is not None and abs(six_conf - direct_confidence) > 0.001:
                buckets["CONFIDENCE_DELTA_ONLY"].append(entry)
            else:
                buckets["REDUNDANT"].append(entry)

        # ── Summary output ────────────────────────────────────────────
        total = len(all_ten_digit_list)
        print("Bucket distribution:")
        print("-" * 64)
        print(f"{'Bucket':<28} {'Count':>8} {'Pct':>8}")
        print("-" * 64)
        for bucket, entries in buckets.items():
            count = len(entries)
            pct = 100.0 * count / total if total else 0.0
            print(f"{bucket:<28} {count:>8} {pct:>7.1f}%")
        print("-" * 64)
        print(f"{'TOTAL':<28} {total:>8} {100.0:>7.1f}%")

        print("\nBy market_scope:")
        for scope, count in market_scopes.most_common():
            print(f"  {scope:<8} {count:>6}")

        # ── Write CSVs ────────────────────────────────────────────────
        summary_path = OUT_DIR / "ten_digit_summary.csv"
        with summary_path.open("w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["bucket", "count", "percent_of_total"])
            for bucket, entries in buckets.items():
                count = len(entries)
                pct = 100.0 * count / total if total else 0.0
                w.writerow([bucket, count, f"{pct:.2f}"])
            w.writerow(["TOTAL", total, "100.00"])
        print(f"\nWrote summary: {summary_path}")

        # Sample rows from the non-REDUNDANT buckets — these are the
        # interesting cases that justify (or don't) 10-digit coverage.
        samples_path = OUT_DIR / "ten_digit_samples.csv"
        with samples_path.open("w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=[
                "bucket",
                "ten_digit_prefix",
                "six_digit_prefix",
                "direct_material_id",
                "direct_material_name",
                "direct_confidence",
                "six_digit_material_id",
                "six_digit_material_name",
                "six_digit_confidence",
                "market_scope",
                "supply_chain_stage",
                "description",
            ])
            w.writeheader()
            # Up to 100 rows from each non-REDUNDANT bucket
            for bucket in (
                "UNIQUE_ATTRIBUTION",
                "RECOVERS_FROM_AMBIGUITY",
                "CONFIDENCE_DELTA_ONLY",
            ):
                for entry in buckets[bucket][:100]:
                    row_out = {"bucket": bucket, **entry}
                    w.writerow(row_out)
        print(f"Wrote samples: {samples_path}")

        # ── Interpretation hints ──────────────────────────────────────
        print("\nInterpretation:")
        redundant = len(buckets["REDUNDANT"])
        unique = len(buckets["UNIQUE_ATTRIBUTION"])
        recovers = len(buckets["RECOVERS_FROM_AMBIGUITY"])
        conf_only = len(buckets["CONFIDENCE_DELTA_ONLY"])

        if redundant == total:
            print(
                "  Every 10-digit row is REDUNDANT with the 6-digit path.\n"
                "  Strong case for option (a) in the N5 ticket: keep HS-only\n"
                "  longest-prefix routing, deprecate 10-digit coverage as a\n"
                "  separate path.  No information loss.",
            )
        elif (unique + recovers) > 0:
            print(
                f"  {unique + recovers} of {total} 10-digit rows carry information\n"
                f"  the 6-digit path can't recover ({unique} UNIQUE_ATTRIBUTION,\n"
                f"  {recovers} RECOVERS_FROM_AMBIGUITY).\n"
                f"  Inspect the samples CSV — if these cases look like real\n"
                f"  battery-relevant distinctions (different cathode chemistries\n"
                f"  splitting under the same 6-digit code), option (b) in the\n"
                f"  N5 ticket is justified: extend the resolver to use keyword\n"
                f"  data at the 10-digit level.\n"
                f"  If most look spurious (the 6-digit was already correct,\n"
                f"  10-digit just narrowed to a sub-product), close N5 as\n"
                f"  'won't fix — accept HS-only longest-prefix routing'.",
            )
        if conf_only > 0:
            print(
                f"  {conf_only} CONFIDENCE_DELTA_ONLY rows — same material,\n"
                f"  different confidence.  Low signal value; the 10-digit\n"
                f"  precision argument doesn't apply here.",
            )

        return 0
    finally:
        s.close()


if __name__ == "__main__":
    sys.exit(main())
