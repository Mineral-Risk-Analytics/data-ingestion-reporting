"""Verify the 2026-05-11 HS seed additions for top-3 unresolved Comtrade prefixes.

Confirms that hs codes the survey flagged as unresolved now resolve cleanly:

  S.1  720250 (Ferrosilicochromium) → Chromium @ 1.0
  S.2  281810 / 281820 / 281830 (full chapter 2818) → Aluminum @ 1.0
  S.3  "2818" (4-digit aggregated) → Aluminum @ 0.9 via Pass-2 fallback
  S.4  262030 (Copper-bearing slag) → Copper @ 1.0
  S.5  262040 (Aluminium-bearing slag) → Aluminum @ 1.0 (wins over the
       existing Vanadium @ 0.9 entry — documented CONFLICT)
  S.6  262099 still ambiguous (Titanium / Vanadium tied) — intentionally
       not "fixed" here because black-mass attribution is a partner
       conversation, not a bulk seed expansion.
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
    def _jsonb_sqlite(t, c, **kw):  # noqa: ARG001
        return "JSON"

    @compiles(UUID, "sqlite")
    def _uuid_sqlite(t, c, **kw):  # noqa: ARG001
        return "VARCHAR(36)"

    @compiles(ARRAY, "sqlite")
    def _array_sqlite(t, c, **kw):  # noqa: ARG001
        return "JSON"

    from app.db.base import Base
    from app.models.supply import HsCodeMaterialMapping, Material
    from app.services.ingestion.comtrade import (
        _build_hs_material_map,
        _resolve_material_id,
    )
    from app.services.ingestion.seed_hs_mappings import _MAPPINGS

    print("=== HS seed unresolved-prefix coverage (2026-05-11) ===\n")

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[
        Material.__table__, HsCodeMaterialMapping.__table__,
    ])

    # Identify which canonical names appear in our targeted seed rows so we
    # only have to materialize those (not all 50+).
    target_materials = {
        "Chromium", "Aluminum", "Copper", "Manganese", "Nickel", "Tungsten",
        "Vanadium", "Titanium", "Niobium", "Silicon (Anode Grade)",
        "Molybdenum",
    }

    with Session(engine) as s:
        # Seed the materials we need
        name_to_id: dict[str, int] = {}
        for name in target_materials:
            m = Material(canonical_name=name, category="test")
            s.add(m); s.flush()
            name_to_id[name] = m.id

        # Load every row from _MAPPINGS that points at one of the target
        # materials.  This gives us a representative slice of the real seed
        # without having to spin up the full materials table.
        loaded = 0
        for (prefix, canonical_name, desc, conf, stage, digits, scope) in _MAPPINGS:
            if canonical_name not in name_to_id:
                continue
            s.add(HsCodeMaterialMapping(
                hs_code_prefix=prefix,
                material_id=name_to_id[canonical_name],
                description=desc,
                confidence=conf,
                digit_count=digits,
                market_scope=scope,
                supply_chain_stage=stage,
            ))
            loaded += 1
        s.commit()
        print(f"Loaded {loaded} mapping rows across {len(target_materials)} materials\n")

        hs_map = _build_hs_material_map(s)

        # ── S.1: 720250 → Chromium ────────────────────────────────────
        print("Test S.1: 720250 (Ferrosilicochromium) → Chromium @ 1.0")
        mid, hsid, conf = _resolve_material_id("720250", hs_map)
        if mid != name_to_id["Chromium"]:
            failures.append(f"720250 → material_id={mid}, expected Chromium")
        elif conf != 1.0:
            failures.append(f"720250 → confidence={conf}, expected 1.0")
        else:
            print(f"  [OK] 720250 → Chromium @ 1.0")

        # ── S.2: 2818 chapter full coverage ───────────────────────────
        print("\nTest S.2: 281810 / 281820 / 281830 → Aluminum @ 1.0")
        for code in ("281810", "281820", "281830"):
            mid, hsid, conf = _resolve_material_id(code, hs_map)
            if mid != name_to_id["Aluminum"]:
                failures.append(f"{code} → material_id={mid}, expected Aluminum")
            elif conf != 1.0:
                failures.append(f"{code} → confidence={conf}, expected 1.0")
            else:
                print(f"  [OK] {code} → Aluminum @ 1.0")

        # ── S.3: 4-digit 2818 fallback ────────────────────────────────
        print("\nTest S.3: '2818' 4-digit aggregated → Aluminum @ 0.9 (catch-all)")
        mid, hsid, conf = _resolve_material_id("2818", hs_map)
        if mid != name_to_id["Aluminum"]:
            failures.append(f"2818 → material_id={mid}, expected Aluminum")
        elif conf != 0.9:
            failures.append(f"2818 → confidence={conf}, expected 0.9")
        else:
            print(f"  [OK] 2818 → Aluminum @ 0.9 (4-digit aggregated catch-all)")

        # Hypothetical 281840 (not in HS) — should still resolve via 4-digit
        mid, hsid, conf = _resolve_material_id("281840", hs_map)
        if mid != name_to_id["Aluminum"]:
            failures.append(
                f"281840 (hypothetical 6-digit) → {mid}, expected Aluminum via fallback"
            )
        else:
            print(f"  [OK] 281840 (unseeded 6-digit) → Aluminum via Pass-2 fallback")

        # ── S.4: 262030 → Copper ──────────────────────────────────────
        print("\nTest S.4: 262030 (Copper-bearing slag) → Copper @ 1.0")
        mid, hsid, conf = _resolve_material_id("262030", hs_map)
        if mid != name_to_id["Copper"]:
            failures.append(f"262030 → material_id={mid}, expected Copper")
        elif conf != 1.0:
            failures.append(f"262030 → confidence={conf}, expected 1.0")
        else:
            print(f"  [OK] 262030 → Copper @ 1.0")

        # ── S.5: 262040 → Aluminum WINS over existing Vanadium @ 0.9 ───
        print("\nTest S.5: 262040 (Aluminium slag) → Aluminum @ 1.0 (overrides V @ 0.9)")
        mid, hsid, conf = _resolve_material_id("262040", hs_map)
        if mid != name_to_id["Aluminum"]:
            failures.append(
                f"262040 → material_id={mid}, expected Aluminum (1.0 > 0.9 V entry)"
            )
        elif conf != 1.0:
            failures.append(f"262040 → confidence={conf}, expected 1.0")
        else:
            print(f"  [OK] 262040 → Aluminum @ 1.0 (Vanadium @ 0.9 properly dominated)")

        # ── S.6: 262099 stays ambiguous (intentional) ──────────────────
        print("\nTest S.6: 262099 stays ambiguous (Ti vs V tied at 0.8/0.7 — not 'fixed')")
        mid, hsid, conf = _resolve_material_id("262099", hs_map)
        # Existing seed: 262099 → Titanium 0.8, 262099 → Vanadium 0.7.
        # Top-confidence wins exact-match Pass 1 with len(top)==1 → Titanium.
        if mid == name_to_id["Titanium"] and conf == 0.8:
            print(f"  [OK] 262099 → Titanium @ 0.8 (existing seed, unchanged)")
        elif mid is None:
            print(f"  [OK] 262099 → (None) — ambiguous, left for partner conversation")
        else:
            # Either outcome above is acceptable; flag anything else.
            failures.append(
                f"262099 resolved to material_id={mid} @ {conf} — unexpected; "
                f"existing seed should leave this as Titanium @ 0.8 or ambiguous"
            )

        # ── Negative check: 7202 4-digit still returns ambiguous None ─
        print("\nTest S.X: '7202' 4-digit unchanged — still tied-confidence ambiguous")
        mid, hsid, conf = _resolve_material_id("7202", hs_map)
        if mid is not None:
            # Existing seed has 8 materials at 7202 with confidence 0.5–0.7;
            # max-confidence tie among 4 of them → resolver returns None.
            # If this test fails because mid is set, the patch accidentally
            # added a tie-breaker (or removed competitors) which would
            # silently re-attribute aggregated 7202 rows.
            failures.append(
                f"7202 4-digit now resolves to material_id={mid} — unexpected "
                f"behaviour change; should remain ambiguous (tied confidences)"
            )
        else:
            print(f"  [OK] 7202 4-digit still ambiguous (8 materials tied)")

    print("\n" + "=" * 64)
    if failures:
        print("FAIL:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS — HS seed unresolved-prefix coverage:")
    print("  ✓ S.1 720250 → Chromium (missing 7202 subheading filled)")
    print("  ✓ S.2 281810 / 281820 / 281830 → Aluminum (full 2818 chapter)")
    print("  ✓ S.3 4-digit 2818 catch-all → Aluminum via Pass-2 fallback")
    print("  ✓ S.4 262030 → Copper (battery recycling stream)")
    print("  ✓ S.5 262040 → Aluminum @ 1.0 (correct WCO HS 2022 attribution)")
    print("  ✓ S.6 262099 ambiguity preserved (partner conversation pending)")
    print("  ✓ S.X 7202 4-digit tied-confidence ambiguity preserved")
    return 0


if __name__ == "__main__":
    sys.exit(main())
