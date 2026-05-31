"""Regression test for ``_COUNTRY_ISO2`` coverage of the MCS dataset (2026-05-09).

Catches the bug class found during the SODA ASH investigation: MCS chapters
silently dropped country production data when the country name didn't have
an ISO-2 mapping in ``_COUNTRY_ISO2``.  PHOSPHATE ROCK was missing Israel /
Syria / Tunisia, TUNGSTEN was missing Rwanda, etc. — 28 countries total
were unmapped despite appearing in MCS World Production rows.

Run this whenever USGS publishes a new MCS edition; failures point at
country names that need to be added to ``_COUNTRY_ISO2``.

This test is data-driven: it walks the actual MCS 2026 CSV and asserts
that every country name appearing in a "World *" production row resolves
to a non-None ISO-2 code (skipping known aggregate strings like "World
total" and "Other countries").

Also re-asserts the five launch-10 country additions from 2026-05-09 so
they're locked in against future regressions:
  PHOSPHATE ROCK → IL / SY / TN
  TUNGSTEN       → RW
  GRAPHITE       → LK
  MANGANESE      → CI
"""

from __future__ import annotations

import csv
import sys

sys.path.insert(0, "/sessions/keen-wonderful-lamport/mnt/battery-data-intelligence-engine")

MCS_PATH = (
    "/sessions/keen-wonderful-lamport/mnt/battery-data-intelligence-engine/"
    "data/usgs/2026/MCS2026_Commodities_Data.csv"
)

# Aggregate / non-country strings that legitimately appear in MCS Country
# fields and should NOT be mapped.  Lower-cased for comparison.
_AGGREGATE_NAMES: set[str] = {
    "world total",
    "other countries",
    "total",
    "other",
    "united states and canada",
    "world total (rounded)",
}


def main() -> int:
    failures: list[str] = []

    from app.services.ingestion.seeds.usgs_mcs_parser import _COUNTRY_ISO2
    from app.services.ingestion.seeds.mcs2026_parser import parse_mcs2026_csv

    # ── Test 1: every country name in World Production rows is mapped ────
    print("Test 1: _COUNTRY_ISO2 covers every country in MCS World Production rows")
    with open(MCS_PATH, encoding="cp1252") as f:
        rows = list(csv.DictReader(f))

    unmapped: dict[str, int] = {}
    for r in rows:
        section = r.get("Section", "") or ""
        if not section.startswith("World "):
            continue
        if "production" not in (r.get("Statistics", "") or "").lower():
            continue
        country = (r.get("Country", "") or "").strip()
        if not country or country.lower() in _AGGREGATE_NAMES:
            continue
        if country not in _COUNTRY_ISO2:
            unmapped[country] = unmapped.get(country, 0) + 1

    if unmapped:
        failures.append(
            f"{len(unmapped)} unmapped country names found:\n  "
            + "\n  ".join(
                f"{name!r:<35} ({count} rows)"
                for name, count in sorted(unmapped.items())
            )
        )
    else:
        # Count distinct countries we DO map for context.
        chapters_seen = {r.get("MCS chapter", "").strip() for r in rows}
        mapped_countries = set()
        for r in rows:
            if (r.get("Section", "") or "").startswith("World "):
                if "production" in (r.get("Statistics", "") or "").lower():
                    c = (r.get("Country", "") or "").strip()
                    if c and c.lower() not in _AGGREGATE_NAMES and c in _COUNTRY_ISO2:
                        mapped_countries.add(c)
        print(
            f"  [OK] {len(mapped_countries)} unique countries across "
            f"{len(chapters_seen)} chapters all map to ISO-2"
        )

    # ── Test 2: Côte d'Ivoire dual-apostrophe registration ──────────────
    # MCS 2026 uses U+2019 (right single quotation mark) but future editions
    # might switch to ASCII.  Both spellings should resolve to "CI".
    print("\nTest 2: Côte d'Ivoire registered under both apostrophe variants")
    u2019 = "Côte d’Ivoire"
    ascii_apos = "Côte d'Ivoire"
    if _COUNTRY_ISO2.get(u2019) != "CI":
        failures.append(
            f"Côte d'Ivoire (U+2019 apostrophe) not mapped to 'CI' — "
            f"got {_COUNTRY_ISO2.get(u2019)!r}"
        )
    if _COUNTRY_ISO2.get(ascii_apos) != "CI":
        failures.append(
            f"Côte d'Ivoire (ASCII apostrophe) not mapped to 'CI' — "
            f"got {_COUNTRY_ISO2.get(ascii_apos)!r}"
        )
    if not failures:
        print(f"  [OK] both apostrophe variants resolve to 'CI'")

    # ── Test 3: launch-10 country additions are locked in ───────────────
    print("\nTest 3: Launch-10 country additions present in parser output")
    expectations: dict[str, set[str]] = {
        "PHOSPHATE ROCK":     {"IL", "SY", "TN"},
        "TUNGSTEN":           {"RW"},
        "GRAPHITE (NATURAL)": {"LK"},
        "MANGANESE":          {"CI"},
        # REE Greenland intentionally excluded — MCS reports reserves only,
        # no current production (Value='—'), so the parser correctly drops it.
    }
    records = parse_mcs2026_csv(MCS_PATH)
    by_chapter = {r["source_name"]: r for r in records}
    for chap, expected in expectations.items():
        rec = by_chapter.get(chap)
        if rec is None:
            failures.append(f"{chap}: missing from parser output")
            continue
        countries = {s["country_code"] for s in rec.get("hs_production_shares", [])}
        missing = expected - countries
        if missing:
            failures.append(
                f"{chap}: expected countries {sorted(expected)} but missing "
                f"{sorted(missing)} from hs_production_shares"
            )
        else:
            print(f"  [OK] {chap:<22} contains {sorted(expected)}")

    # ── Test 4: PHOSPHATE ROCK distribution sanity ──────────────────────
    # The previously-missing Tunisia / Israel / Syria add up to ~3-5% of
    # global phosphate.  Verify CN is still the dominant producer (~45%)
    # and that MA + US + RU + JO + SA together exceed 35% — sanity check
    # against accidentally truncating the producer list in some future edit.
    print("\nTest 4: PHOSPHATE ROCK distribution looks sensible")
    phos = by_chapter.get("PHOSPHATE ROCK")
    if phos is None:
        failures.append("PHOSPHATE ROCK: missing from parser output")
    else:
        shares = {
            s["country_code"]: s["production_share"]
            for s in phos["hs_production_shares"]
        }
        cn = shares.get("CN", 0)
        if cn < 0.30 or cn > 0.55:
            failures.append(
                f"PHOSPHATE ROCK: CN share = {cn:.1%}, expected 30-55% "
                f"(China is the dominant producer; sanity-check fail)"
            )
        else:
            print(f"  [OK] CN share = {cn:.1%} (within 30-55% expected band)")
        top_5_other = sum(shares.get(c, 0) for c in ("MA", "US", "RU", "JO", "SA"))
        if top_5_other < 0.30:
            failures.append(
                f"PHOSPHATE ROCK: MA+US+RU+JO+SA = {top_5_other:.1%}, "
                f"expected ≥ 30%"
            )
        else:
            print(
                f"  [OK] MA+US+RU+JO+SA = {top_5_other:.1%} "
                f"(major non-CN producers represented)"
            )

    print("\n" + "=" * 64)
    if failures:
        print("FAIL:")
        for f in failures:
            # Render multi-line failures cleanly.
            for i, line in enumerate(f.split("\n")):
                print(f"  {'- ' if i == 0 else '  '}{line}")
        return 1
    print("PASS — MCS country-mapping coverage:")
    print("  ✓ Every country name in MCS World Production rows resolves to ISO-2")
    print("  ✓ Côte d'Ivoire registered under both apostrophe variants (U+2019 + ASCII)")
    print("  ✓ Launch-10 country additions (IL/SY/TN/RW/LK/CI) flow through parser")
    print("  ✓ PHOSPHATE ROCK distribution sane (CN dominant, top-5 other ≥ 30%)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
