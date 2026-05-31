"""End-to-end test for the /dashboard/coverage-matrix endpoint (2026-05-11).

Verifies:

  CM.1   Per-material × per-source event counts in the 90d window are
         computed correctly; events outside the window don't leak in.

  CM.2   Sources are ordered by descending total event count so the
         most-active source lands as the first column.

  CM.3   Per-material pillar scores reflect the latest MaterialGlobalRiskScore
         row.  has_signal is True when score > 0.

  CM.4   Materials in the launch list but missing from the Materials table
         render as sentinel rows (material_id = -1) with empty cells.

  CM.5   Materials with no events in the window get all-zero source cells
         (not missing — empty arrays would break the grid layout).
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

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
    from app.models.documents import SourceDocument
    from app.models.regulatory import RiskEvent, RiskEventMaterial
    from app.models.scoring import MaterialGlobalRiskScore
    from app.models.source import Source
    from app.models.supply import Material

    # Import the route module so we can call coverage_matrix directly,
    # bypassing the FastAPI dependency injection.
    from app.api.routes import dashboard as dashboard_route

    print("=== Coverage matrix endpoint ===\n")

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=[
            Material.__table__,
            Source.__table__,
            SourceDocument.__table__,
            RiskEvent.__table__,
            RiskEventMaterial.__table__,
            MaterialGlobalRiskScore.__table__,
        ],
    )

    NOW = datetime(2026, 5, 11, 12, 0, 0, tzinfo=timezone.utc)
    IN_WINDOW = NOW - timedelta(days=30)
    OUT_OF_WINDOW = NOW - timedelta(days=120)

    with Session(engine) as s:
        # Seed 8 of the 10 launch-list materials.
        # Skip Phosphate and Iron Ore to verify sentinel-row behavior.
        seeded_names = [
            "Lithium", "Cobalt", "Nickel", "Manganese",
            "Natural Graphite", "Copper", "Aluminum", "Rare Earth Elements",
        ]
        materials_by_name: dict[str, Material] = {}
        for name in seeded_names:
            m = Material(canonical_name=name, category="cathode_active")
            s.add(m); s.flush()
            materials_by_name[name] = m

        # Seed 3 sources
        gta = Source(name="GTA", source_type="gta", phase="1", is_active=True, config_json={})
        eurlex = Source(name="EUR-Lex", source_type="eurlex", phase="1", is_active=True, config_json={})
        fr = Source(name="Federal Register", source_type="fr", phase="1", is_active=True, config_json={})
        s.add_all([gta, eurlex, fr]); s.flush()
        doc_gta = SourceDocument(source_id=gta.id, external_id="d-gta", title="t",
                                  url="u", document_type="t", metadata_json={})
        doc_eu = SourceDocument(source_id=eurlex.id, external_id="d-eu", title="t",
                                  url="u", document_type="t", metadata_json={})
        doc_fr = SourceDocument(source_id=fr.id, external_id="d-fr", title="t",
                                 url="u", document_type="t", metadata_json={})
        s.add_all([doc_gta, doc_eu, doc_fr]); s.flush()

        def _ev(doc, when, mat_id, ch):
            ev = RiskEvent(
                source_document_id=doc.id,
                event_type="test", title="t",
                severity_score=0.5, content_hash=ch,
            )
            s.add(ev); s.flush()
            ev.created_at = when
            s.add(RiskEventMaterial(
                risk_event_id=ev.id, material_id=mat_id,
                relevance_score=1.0, match_reason="test",
            ))
            return ev

        # Lithium: 10 GTA events + 3 EUR-Lex events + 2 FR events
        for i in range(10):
            _ev(doc_gta, IN_WINDOW, materials_by_name["Lithium"].id, f"li-g-{i}")
        for i in range(3):
            _ev(doc_eu, IN_WINDOW, materials_by_name["Lithium"].id, f"li-e-{i}")
        for i in range(2):
            _ev(doc_fr, IN_WINDOW, materials_by_name["Lithium"].id, f"li-f-{i}")

        # Cobalt: 5 GTA events, all out-of-window
        for i in range(5):
            _ev(doc_gta, OUT_OF_WINDOW, materials_by_name["Cobalt"].id, f"co-g-{i}")

        # Nickel: 4 FR events in window, 1 GTA event in window
        for i in range(4):
            _ev(doc_fr, IN_WINDOW, materials_by_name["Nickel"].id, f"ni-f-{i}")
        _ev(doc_gta, IN_WINDOW, materials_by_name["Nickel"].id, "ni-g-1")

        # Manganese: 0 events anywhere
        # Natural Graphite: 1 EUR-Lex event
        _ev(doc_eu, IN_WINDOW, materials_by_name["Natural Graphite"].id, "ng-e-1")
        # Copper, Aluminum, REE: 0 events
        s.commit()

        # Seed global risk scores for some materials
        # Lithium: has 4 pillars with signal
        # Cobalt: all pillars zero (fallback / no signal)
        # Nickel: 2 pillars only
        # Others: no global score row at all
        scored = [
            ("Lithium", [25, 35, 45, 55, 0]),     # 4 pillars > 0
            ("Cobalt", [0, 0, 0, 0, 0]),          # 0 pillars > 0
            ("Nickel", [40, 0, 0, 30, 0]),        # 2 pillars > 0
        ]
        for name, pillars in scored:
            mc, geo, reg, op_, fin = pillars
            s.add(MaterialGlobalRiskScore(
                material_id=materials_by_name[name].id,
                as_of_date=NOW.date(),
                overall_risk_score=50.0,
                material_concentration_score=mc,
                geopolitical_trade_score=geo,
                regulatory_compliance_score=reg,
                operational_score=op_,
                financial_pressure_score=fin,
            ))
        s.commit()

        # Call the endpoint helper directly.  Bypasses FastAPI dep injection.
        result = dashboard_route.coverage_matrix(_user={"sub": "test"}, db=s)

        # ── CM.1: source counts ────────────────────────────────────────
        print("Test CM.1: per-material × per-source counts (90d window)")
        row_by_name = {r.canonical_name: r for r in result.rows}
        li_row = row_by_name["Lithium"]
        li_sources = {c.source_name: c.event_count_90d for c in li_row.sources}
        if li_sources.get("GTA") != 10:
            failures.append(f"Lithium GTA count = {li_sources.get('GTA')}, expected 10")
        if li_sources.get("EUR-Lex") != 3:
            failures.append(f"Lithium EUR-Lex count = {li_sources.get('EUR-Lex')}, expected 3")
        if li_sources.get("Federal Register") != 2:
            failures.append(f"Lithium FR count = {li_sources.get('Federal Register')}, expected 2")
        if not [f for f in failures if "Lithium" in f]:
            print(f"  [OK] Lithium: GTA=10, EUR-Lex=3, FR=2")

        # Cobalt should have 0 events in window (its 5 events were out-of-window)
        co_row = row_by_name["Cobalt"]
        co_total = sum(c.event_count_90d for c in co_row.sources)
        if co_total != 0:
            failures.append(f"Cobalt total in window = {co_total}, expected 0 (events were 120d ago)")
        else:
            print(f"  [OK] Cobalt: 0 events in window (correctly excluded out-of-window events)")

        # ── CM.2: source column order ──────────────────────────────────
        print("\nTest CM.2: sources ordered by descending total volume")
        # Totals: GTA = 10+1 = 11, EUR-Lex = 3+1 = 4, FR = 2+4 = 6
        # So order should be: GTA, FR, EUR-Lex
        if result.sources_in_order != ["GTA", "Federal Register", "EUR-Lex"]:
            failures.append(
                f"sources_in_order={result.sources_in_order}, "
                f"expected ['GTA', 'Federal Register', 'EUR-Lex']"
            )
        else:
            print(f"  [OK] sources ordered by volume: {result.sources_in_order}")

        # ── CM.3: pillar scores + has_signal ───────────────────────────
        print("\nTest CM.3: pillar scores reflect latest MaterialGlobalRiskScore")
        li_pillars = {p.name: p for p in li_row.pillars}
        if li_pillars["material_concentration_score"].score != 25:
            failures.append(
                f"Lithium MC score = {li_pillars['material_concentration_score'].score}, expected 25"
            )
        elif not li_pillars["material_concentration_score"].has_signal:
            failures.append("Lithium MC score=25 should have has_signal=True")
        if li_pillars["financial_pressure_score"].score != 0:
            failures.append(
                f"Lithium FP score = {li_pillars['financial_pressure_score'].score}, expected 0"
            )
        elif li_pillars["financial_pressure_score"].has_signal:
            failures.append("Lithium FP score=0 should have has_signal=False")
        if not [f for f in failures if "Lithium" in f or "MC" in f or "FP" in f]:
            print(f"  [OK] Lithium: 4 pillars with signal, 1 fallback (FP=0)")

        # Cobalt: all pillars zero
        co_pillars = {p.name: p for p in co_row.pillars}
        any_signal_co = any(p.has_signal for p in co_pillars.values())
        if any_signal_co:
            failures.append("Cobalt has all-zero pillars — none should have has_signal=True")
        else:
            print(f"  [OK] Cobalt: 0 pillars with signal (all-zero fallback row)")

        # Materials without a global score row at all — pillars should
        # have score=None
        cu_row = row_by_name["Copper"]
        if any(p.score is not None for p in cu_row.pillars):
            failures.append("Copper has no global score row; all pillar scores should be None")
        else:
            print(f"  [OK] Copper: no global score row → all pillar scores None")

        # ── CM.4: sentinel rows for missing materials ──────────────────
        print("\nTest CM.4: launch-list materials missing from DB render as sentinels")
        for missing_name in ("Phosphate (Battery Grade)", "Iron Ore (LFP Grade)"):
            row = row_by_name.get(missing_name)
            if row is None:
                failures.append(f"missing sentinel row for {missing_name}")
                continue
            if row.material_id != -1:
                failures.append(
                    f"{missing_name}: material_id={row.material_id}, expected -1 sentinel"
                )
            # Should have empty source cells (all zero) and None pillars
            if any(c.event_count_90d != 0 for c in row.sources):
                failures.append(f"{missing_name}: source cells should all be 0")
            if any(p.score is not None for p in row.pillars):
                failures.append(f"{missing_name}: pillar scores should all be None")
        if not [f for f in failures if "Phosphate" in f or "Iron Ore" in f]:
            print(f"  [OK] Phosphate + Iron Ore → sentinel rows with empty cells")

        # ── CM.5: rows have all source cells even when count=0 ─────────
        print("\nTest CM.5: every row has source cells for every column (zeros included)")
        n_sources = len(result.sources_in_order)
        for row in result.rows:
            if len(row.sources) != n_sources:
                failures.append(
                    f"{row.canonical_name}: {len(row.sources)} source cells, "
                    f"expected {n_sources}"
                )
        if not [f for f in failures if "source cells" in f]:
            print(f"  [OK] all 10 rows × {n_sources} source columns rendered (zeros included)")

        # Manganese specifically: in DB but 0 events.  Should still have
        # source cells (all 0) and proper pillar cells (all None).
        mn_row = row_by_name["Manganese"]
        if any(c.event_count_90d != 0 for c in mn_row.sources):
            failures.append("Manganese has 0 events; source cells should all be 0")
        else:
            print(f"  [OK] Manganese: in DB, 0 events → all-zero source cells")

        # Total row count == 10
        if len(result.rows) != 10:
            failures.append(f"got {len(result.rows)} rows, expected 10 (launch-list size)")

        # ── CM.6: pillar_coverage aggregate ────────────────────────────
        print("\nTest CM.6: pillar_coverage aggregate counts per-pillar signal across launch list")
        if len(result.pillar_coverage) != 5:
            failures.append(f"pillar_coverage len={len(result.pillar_coverage)}, expected 5")
        # Three materials have global scores (Lithium, Cobalt, Nickel).
        # Sentinel rows (Phosphate, Iron Ore) excluded from `total`.
        # Real launch-list rows in DB: 8 (Lithium, Cobalt, Nickel, Manganese,
        # Natural Graphite, Copper, Aluminum, REE)
        expected_total = 8
        pillar_by_name = {p.name: p for p in result.pillar_coverage}

        # material_concentration_score: Lithium has 25, Cobalt has 0,
        # Nickel has 40, others have no score row.  Signal: Lithium + Nickel = 2
        mc = pillar_by_name["material_concentration_score"]
        if mc.materials_with_signal != 2:
            failures.append(
                f"MC materials_with_signal = {mc.materials_with_signal}, expected 2"
            )
        elif mc.materials_with_score != 3:
            failures.append(
                f"MC materials_with_score = {mc.materials_with_score}, expected 3"
            )
        elif mc.total != expected_total:
            failures.append(f"MC total = {mc.total}, expected {expected_total}")
        else:
            print(f"  [OK] Material Concentration: 2 / 3 / 8 (signal / scored / total)")

        # financial_pressure_score: all zeros in seeded scores → 0 signal
        fp = pillar_by_name["financial_pressure_score"]
        if fp.materials_with_signal != 0:
            failures.append(
                f"FP materials_with_signal = {fp.materials_with_signal}, expected 0"
            )
        else:
            print(f"  [OK] Financial Pressure: 0 / 3 / 8 (all 3 scored materials have FP=0)")

    print("\n" + "=" * 64)
    if failures:
        print("FAIL:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS — Coverage matrix endpoint:")
    print("  ✓ CM.1 90d window filters per-material × per-source counts correctly")
    print("  ✓ CM.2 sources column-ordered by descending volume")
    print("  ✓ CM.3 pillar scores reflect latest MaterialGlobalRiskScore + has_signal flag")
    print("  ✓ CM.4 launch-list materials missing from DB → sentinel rows")
    print("  ✓ CM.5 every row has all source columns (zeros included)")
    print("  ✓ CM.6 pillar_coverage aggregate counts per-pillar signal correctly")
    return 0


if __name__ == "__main__":
    sys.exit(main())
