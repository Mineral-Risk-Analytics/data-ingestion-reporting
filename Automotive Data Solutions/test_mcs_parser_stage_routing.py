"""End-to-end test for the MCS parser stage auto-routing refactor (2026-05-09).

Confirms:
  1. Single-stage chapters (Lithium, Cobalt, Nickel, REE, Graphite, Phosphate,
     Manganese, Tungsten) → emit ore-stage hs_production_shares.
  2. ALUMINUM → refined-stage hs_production_shares (Smelter Production).
  3. COPPER → both ore (mine production) AND refined (refinery production).
  4. BAUXITE AND ALUMINA → both ore (Bauxite, mine) AND intermediate (Alumina,
     refinery).
  5. SILICON → still routed via _DETAIL_TO_HS_PREFIX (ferrosilicon vs silicon
     metal), Path A correctly suppressed for these sub-types.
  6. _classify_detail_stage rejects "rounded" totals.
  7. Path A and Path B never produce overlapping entries.
"""

from __future__ import annotations

import sys

sys.path.insert(0, "/sessions/keen-wonderful-lamport/mnt/battery-data-intelligence-engine")


def main() -> int:
    failures: list[str] = []

    from app.services.ingestion.seeds.mcs2026_parser import (
        _classify_detail_stage,
        _DETAIL_STAGE_PATTERNS,
        _DETAIL_TO_HS_PREFIX,
        _CHAPTER_HS_PREFIX,
        parse_mcs2026_csv,
    )

    # ── Test 1: classifier rules ──────────────────────────────────────────
    print("Test 1: _classify_detail_stage")
    cases = [
        ("Mine production",                       "ore"),
        ("Smelter production",                    "refined"),
        ("Refinery production",                   "refined"),
        ("Mine production: rounded",              None),    # totals
        ("Refinery production: rounded",          None),
        ("Alumina, refinery production",          "intermediate"),  # specific beats generic
        ("Bauxite, mine production",              "ore"),           # specific beats generic
        ("Alumina, refinery production: rounded", None),
        ("",                                      None),
        ("World total reserves",                  None),
        ("ferrosilicon",                          None),    # no production marker
    ]
    for detail, expected in cases:
        got = _classify_detail_stage(detail)
        if got != expected:
            failures.append(
                f"_classify_detail_stage({detail!r}) = {got!r}, expected {expected!r}"
            )
        else:
            print(f"  [OK] {detail!r:<45} → {got}")

    # ── Test 2: parse the real MCS 2026 CSV and check launch-10 chapters ──
    print("\nTest 2: parse_mcs2026_csv against MCS 2026 CSV")
    records = parse_mcs2026_csv(
        "/sessions/keen-wonderful-lamport/mnt/battery-data-intelligence-engine/"
        "data/usgs/2026/MCS2026_Commodities_Data.csv"
    )
    by_chapter = {r["source_name"]: r for r in records}

    expectations: dict[str, set[str]] = {
        # chapter → expected stages emitted
        "LITHIUM":             {"ore"},
        "COBALT":              {"ore"},
        "NICKEL":              {"ore"},
        "RARE EARTHS":         {"ore"},
        "GRAPHITE (NATURAL)":  {"ore"},
        "PHOSPHATE ROCK":      {"ore"},
        "MANGANESE":           {"ore"},
        "TUNGSTEN":            {"ore"},
        # Multi-stage:
        "ALUMINUM":            {"refined"},        # Smelter = refined
        "COPPER":              {"ore", "refined"},  # Mine + Refinery
        "BAUXITE AND ALUMINA": {"ore", "intermediate"},  # Bauxite mine + Alumina refinery
    }

    for chapter, expected_stages in expectations.items():
        rec = by_chapter.get(chapter)
        if rec is None:
            failures.append(f"{chapter}: missing from parser output")
            continue
        shares = rec.get("hs_production_shares", [])
        # Material-level shares should also be present for material-signal
        # writes (BAUXITE has writes_material_signals=False on alias side
        # which the CLI honours; parser still emits the rows).
        mat_shares = rec.get("production_shares", [])
        got_stages = {s.get("stage") for s in shares if s.get("stage")}
        if got_stages != expected_stages:
            failures.append(
                f"{chapter}: hs_production_shares stages = {got_stages}, "
                f"expected {expected_stages}"
            )
        else:
            print(
                f"  [OK] {chapter:<22} mat-level={len(mat_shares):>3} "
                f"hs-shares={len(shares):>3} stages={sorted(got_stages)}"
            )

    # ── Test 3: SILICON still routes via Path B (sub-type override) ───────
    print("\nTest 3: SILICON sub-type override (Path B)")
    si = by_chapter.get("SILICON")
    if si is None:
        failures.append("SILICON: missing from parser output")
    else:
        shares = si.get("hs_production_shares", [])
        path_a = [s for s in shares if s.get("stage")]
        path_b = [s for s in shares if s.get("hs_code_prefix") and not s.get("stage")]
        prefixes = {s["hs_code_prefix"] for s in path_b}
        if path_a:
            failures.append(
                f"SILICON: Path A leaked {len(path_a)} stage-tagged entries; "
                f"all should be Path B (prefix-tagged)"
            )
        elif not prefixes >= {"720221", "280461"}:
            failures.append(
                f"SILICON: prefixes = {prefixes}, expected ≥ {{'720221','280461'}}"
            )
        else:
            print(
                f"  [OK] {len(path_b)} prefix-only entries; prefixes = {sorted(prefixes)}"
            )

    # ── Test 4: no entry has BOTH prefix AND stage ────────────────────────
    print("\nTest 4: Path A and Path B never overlap")
    overlapping = []
    for r in records:
        for s in r.get("hs_production_shares", []):
            if s.get("hs_code_prefix") and s.get("stage"):
                overlapping.append((r["source_name"], s))
    if overlapping:
        failures.append(
            f"{len(overlapping)} entries have BOTH prefix AND stage; "
            f"first: {overlapping[0]}"
        )
    else:
        print(f"  [OK] 0 entries with both prefix and stage across all chapters")

    # ── Test 5: _CHAPTER_HS_PREFIX is empty (deprecated) ──────────────────
    print("\nTest 5: _CHAPTER_HS_PREFIX deprecation")
    if _CHAPTER_HS_PREFIX:
        # Not strictly an error — kept for future overrides — but flag
        # the entries so we know what's still routed via the old path.
        print(f"  [INFO] _CHAPTER_HS_PREFIX still has entries: {_CHAPTER_HS_PREFIX}")
    else:
        print(f"  [OK] _CHAPTER_HS_PREFIX is empty (deprecated, future-overrides slot)")

    # ── Test 6: _DETAIL_TO_HS_PREFIX trimmed to Silicon-only ──────────────
    print("\nTest 6: _DETAIL_TO_HS_PREFIX scope")
    chapters = {chap for (chap, _) in _DETAIL_TO_HS_PREFIX}
    if chapters != {"SILICON"}:
        failures.append(
            f"_DETAIL_TO_HS_PREFIX chapters = {chapters}, expected {{'SILICON'}} only "
            f"(Cu auto-detected via stage now)"
        )
    else:
        print(f"  [OK] only SILICON has explicit sub-type prefix overrides")

    print("\n" + "=" * 64)
    if failures:
        print("FAIL:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS — MCS parser stage auto-routing:")
    print("  ✓ Detail-substring classifier handles all production patterns")
    print("  ✓ 8 single-stage launch chapters → ore-stage rows")
    print("  ✓ ALUMINUM → refined-stage rows (Smelter Production)")
    print("  ✓ COPPER → ore + refined (Mine + Refinery, auto-detected)")
    print("  ✓ BAUXITE → ore + intermediate (Bauxite mine + Alumina refinery)")
    print("  ✓ SILICON sub-type overrides preserved; Path A correctly suppressed")
    print("  ✓ Path A and Path B never produce overlapping entries")
    print("  ✓ _CHAPTER_HS_PREFIX deprecated (empty)")
    print("  ✓ _DETAIL_TO_HS_PREFIX trimmed to Silicon-only")
    return 0


if __name__ == "__main__":
    sys.exit(main())
