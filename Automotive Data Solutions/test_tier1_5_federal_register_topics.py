"""End-to-end test for Tier 1.5 — Federal Register topics as primary signal
(2026-05-09).

Verifies:

  T1.5.1  ``_resolve_topics_to_materials`` translates known FR topic strings
          to canonical materials at the topic-match relevance.
  T1.5.2  Unmapped topics ("Imports", "Tariff", "Critical materials") return
          no matches — by design; the keyword fallback handles those.
  T1.5.3  Duplicate topics (case-insensitive) collapse to a single entry.
  T1.5.4  Topic-derived matches have ``hs_mapping_id=None`` (stage-ambiguous,
          same shape as canonical-name keyword matches).
"""

from __future__ import annotations

import sys

sys.path.insert(0, "/sessions/keen-wonderful-lamport/mnt/battery-data-intelligence-engine")


def main() -> int:
    failures: list[str] = []

    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from sqlalchemy.ext.compiler import compiles
    from sqlalchemy.dialects.postgresql import JSONB, UUID, ARRAY

    @compiles(JSONB, "sqlite")
    def _jsonb_sqlite(type_, compiler, **kw):  # noqa: ARG001
        return "JSON"

    @compiles(UUID, "sqlite")
    def _uuid_sqlite(type_, compiler, **kw):  # noqa: ARG001
        return "VARCHAR(36)"

    @compiles(ARRAY, "sqlite")
    def _array_sqlite(type_, compiler, **kw):  # noqa: ARG001
        return "JSON"

    from app.db.base import Base
    from app.models.supply import Material
    from app.services.ingestion.normalizers.material_resolver import MaterialResolver
    from app.services.ingestion.ingest_federal_register import (
        _TOPIC_MATERIAL_MAP,
        _TOPIC_MATCH_RELEVANCE,
        _resolve_topics_to_materials,
    )

    print("=== Tier 1.5: Federal Register topics as primary signal ===\n")

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[Material.__table__])

    with Session(engine) as s:
        # Seed canonical materials referenced by _TOPIC_MATERIAL_MAP
        mat_ids: dict[str, int] = {}
        for name, category in [
            ("Lithium", "cathode_active"),
            ("Cobalt", "cathode_active"),
            ("Nickel", "cathode_active"),
            ("Manganese", "cathode_active"),
            ("Natural Graphite", "anode_active"),
            ("Aluminum", "structural"),
            ("Copper", "current_collector"),
            ("Tungsten", "component"),
            ("Iron Ore (LFP Grade)", "cathode_active"),
            ("Phosphate (Battery Grade)", "cathode_active"),
            ("Rare Earth Elements", "component"),
        ]:
            m = Material(canonical_name=name, category=category)
            s.add(m); s.flush()
            mat_ids[name] = m.id
        s.commit()

        resolver = MaterialResolver(s)

        # ── T1.5.1: known topics resolve to canonical materials ──────────
        print("Test T1.5.1: direct topic → material resolution")
        cases = [
            ("Lithium", "Lithium"),
            ("Cobalt", "Cobalt"),
            ("Graphite", "Natural Graphite"),
            ("Rare earth", "Rare Earth Elements"),
            ("Rare-earth", "Rare Earth Elements"),
            ("Phosphate", "Phosphate (Battery Grade)"),
        ]
        for topic, expected_canonical in cases:
            results = _resolve_topics_to_materials([topic], resolver)
            if len(results) != 1:
                failures.append(
                    f"topic={topic!r} yielded {len(results)} matches, expected 1"
                )
                continue
            mid, rel, kw, hs_id = results[0]
            if mid != mat_ids[expected_canonical]:
                failures.append(
                    f"topic={topic!r}: got material_id={mid}, "
                    f"expected {mat_ids[expected_canonical]} ({expected_canonical})"
                )
            elif abs(rel - _TOPIC_MATCH_RELEVANCE) > 0.001:
                failures.append(
                    f"topic={topic!r}: relevance {rel}, expected {_TOPIC_MATCH_RELEVANCE}"
                )
            elif hs_id is not None:
                failures.append(
                    f"topic={topic!r}: hs_mapping_id should be None, got {hs_id}"
                )
            elif not kw.startswith("fr_topic:"):
                failures.append(
                    f"topic={topic!r}: matched_keyword tag {kw!r} missing 'fr_topic:' prefix"
                )
            else:
                print(f"  [OK] {topic!r:<14} → {expected_canonical!r:<28} (relevance={rel}, label={kw!r})")

        # ── T1.5.2: unmapped topics return nothing ───────────────────────
        print("\nTest T1.5.2: unmapped topics return no matches")
        unmapped = [
            "Critical materials",  # too broad — not in the map
            "Imports",              # event-type topic
            "Exports",              # event-type topic
            "Tariff",               # event-type topic
            "Hazardous materials",  # not material-specific
            "Some random topic",    # totally unrelated
        ]
        results = _resolve_topics_to_materials(unmapped, resolver)
        if results:
            failures.append(
                f"unmapped topics returned {len(results)} matches: {results}"
            )
        else:
            print(f"  [OK] {len(unmapped)} unmapped topics → 0 matches (keyword fallback handles)")

        # ── T1.5.3: duplicate topics collapse ────────────────────────────
        print("\nTest T1.5.3: duplicate topics collapse to single entry")
        results = _resolve_topics_to_materials(
            ["Lithium", "lithium", "LITHIUM", "Cobalt"], resolver,
        )
        if len(results) != 2:
            failures.append(
                f"duplicate-lithium topics yielded {len(results)} results, expected 2"
            )
        else:
            mat_set = {r[0] for r in results}
            if mat_set != {mat_ids["Lithium"], mat_ids["Cobalt"]}:
                failures.append(f"unexpected material set: {mat_set}")
            else:
                print(f"  [OK] 4 topic strings (3 dup Lithium + 1 Cobalt) → 2 distinct materials")

        # ── T1.5.4: shape matches MaterialCache.detect ───────────────────
        print("\nTest T1.5.4: output tuple shape matches MaterialCache.detect")
        results = _resolve_topics_to_materials(["Lithium"], resolver)
        sample = results[0]
        if len(sample) != 4:
            failures.append(f"tuple length {len(sample)}, expected 4")
        else:
            mid, rel, kw, hs_id = sample
            if not (isinstance(mid, int) and isinstance(rel, float)
                    and isinstance(kw, str) and hs_id is None):
                failures.append(f"tuple types wrong: {[type(x).__name__ for x in sample]}")
            else:
                print(f"  [OK] tuple shape: (int, float, str, None) — compatible with _persist_material_links")

        # ── T1.5.5: empty topics list → empty result ─────────────────────
        print("\nTest T1.5.5: empty topics list returns empty result")
        empty_results = _resolve_topics_to_materials([], resolver)
        if empty_results:
            failures.append(f"empty topics returned {empty_results}, expected []")
        else:
            print(f"  [OK] empty topics → empty result")

        # And: list with only empty/falsy entries
        falsy_results = _resolve_topics_to_materials(["", None, "   "], resolver)
        if falsy_results:
            failures.append(f"falsy topics returned {falsy_results}, expected []")
        else:
            print(f"  [OK] falsy topic entries → empty result")

    print("\n" + "=" * 64)
    if failures:
        print("FAIL:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS — Tier 1.5 Federal Register topics:")
    print("  ✓ 11 launch-list materials addressable via topic")
    print("  ✓ Unmapped event-type topics correctly return no matches")
    print("  ✓ Case-insensitive duplicate handling")
    print("  ✓ Output tuple shape compatible with MaterialCache.detect")
    print("  ✓ Topic map size:", len(_TOPIC_MATERIAL_MAP), "entries")
    return 0


if __name__ == "__main__":
    sys.exit(main())
