"""End-to-end tests for the launch-list-centric dashboard KPIs (2026-05-11).

Three helpers in `app/api/routes/dashboard.py`:

  K.1   _compute_core_minerals_scored — counts launch-list materials with
        a non-null overall_risk_score, reports unscored names.

  K.2   _compute_recent_risk_events_30d — counts risk_events created in
        the last 30 days, comparison count from the [60d, 30d) window,
        top-5 sources joined through SourceDocument → Source.

  K.3   _compute_coverage_gaps — flags launch-list materials failing any
        of: no_global_score / stale_score / thin_events / thin_pillars.
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
    from app.models.facility import Facility, FacilityMaterialLink
    from app.models.regulatory import RiskEvent, RiskEventMaterial
    from app.models.scoring import MaterialGlobalRiskScore
    from app.models.source import Source
    from app.models.supply import Material
    from app.api.routes.dashboard import (
        _compute_core_minerals_scored,
        _compute_recent_risk_events_30d,
        _compute_coverage_gaps,
    )
    from app.services.scoring.launch_list import LAUNCH_LIST_CANONICAL_NAMES

    print("=== Dashboard launch-list KPIs ===\n")

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
            Facility.__table__,
            FacilityMaterialLink.__table__,
        ],
    )

    NOW = datetime(2026, 5, 11, 12, 0, 0, tzinfo=timezone.utc)
    CUTOFF_30D = NOW - timedelta(days=30)
    OUT_60D = NOW - timedelta(days=45)  # in [60d, 30d) window
    LONG_AGO = NOW - timedelta(days=200)  # outside both windows

    with Session(engine) as s:
        # ── Seed materials: 7 of the 10 launch-list, plus 1 non-launch ───
        # Skip Phosphate, Iron Ore, REE so K.1 and K.3 catch them as gaps.
        seeded_names = [
            "Lithium", "Cobalt", "Nickel", "Manganese",
            "Natural Graphite", "Copper", "Aluminum",
        ]
        materials_by_name: dict[str, Material] = {}
        for name in seeded_names:
            m = Material(canonical_name=name, category="cathode_active")
            s.add(m); s.flush()
            materials_by_name[name] = m
        # Non-launch-list material that should be ignored by everything
        non_ll = Material(canonical_name="Bismuth", category="r_and_d")
        s.add(non_ll); s.flush()
        materials_by_name["Bismuth"] = non_ll

        # ── Seed global risk scores ──────────────────────────────────────
        # Lithium / Cobalt / Nickel / Manganese / Natural Graphite / Copper get scores
        # Aluminum gets a NULL overall_risk_score → counts as unscored
        # Phosphate / Iron Ore (LFP Grade) / Rare Earth Elements aren't seeded
        # at all → also count as unscored
        scored = [
            ("Lithium", 75.0, NOW - timedelta(days=5), [10, 20, 30, 40, 50]),    # fresh + 5 pillars
            ("Cobalt", 60.0, NOW - timedelta(days=10), [10, 20, 30, 40, 0]),     # 4 pillars
            ("Nickel", 50.0, NOW - timedelta(days=2), [10, 20, 0, 0, 0]),        # 2 pillars → thin_pillars
            ("Manganese", 45.0, NOW - timedelta(days=45), [10, 20, 30, 40, 50]), # stale
            ("Natural Graphite", 40.0, NOW - timedelta(days=8), [10, 20, 30, 0, 0]),  # 3 pillars
            ("Copper", 35.0, NOW - timedelta(days=15), [10, 20, 30, 40, 50]),    # fresh
        ]
        # Aluminum row with NULL overall_risk_score
        s.add(MaterialGlobalRiskScore(
            material_id=materials_by_name["Aluminum"].id,
            as_of_date=NOW.date(),
            overall_risk_score=None,
            material_concentration_score=10,
            geopolitical_trade_score=20,
            regulatory_compliance_score=30,
            operational_score=40,
            financial_pressure_score=50,
        ))
        for name, score, as_of, pillar_values in scored:
            mc, geo, reg, op_, fin = pillar_values
            s.add(MaterialGlobalRiskScore(
                material_id=materials_by_name[name].id,
                as_of_date=as_of.date(),
                overall_risk_score=score,
                material_concentration_score=mc,
                geopolitical_trade_score=geo,
                regulatory_compliance_score=reg,
                operational_score=op_,
                financial_pressure_score=fin,
            ))
        s.commit()

        # ── Seed sources + source documents ──────────────────────────────
        src_a = Source(name="GTA", source_type="gta", phase="1", is_active=True, config_json={})
        src_b = Source(name="EUR-Lex", source_type="eurlex", phase="1", is_active=True, config_json={})
        s.add_all([src_a, src_b]); s.flush()
        doc_a = SourceDocument(source_id=src_a.id, external_id="gta-test",
                               title="t", url="u", document_type="td", metadata_json={})
        doc_b = SourceDocument(source_id=src_b.id, external_id="eurlex-test",
                               title="t", url="u", document_type="reg", metadata_json={})
        s.add_all([doc_a, doc_b]); s.flush()

        # ── Seed risk events ────────────────────────────────────────────
        # Need to set RiskEvent.created_at explicitly to test the window logic.
        # Helper: 10 events in last 30d (all GTA), 3 events in [60d,30d) (EUR-Lex),
        # 1 ancient event (outside both windows).
        def _ev(doc, when, mat_id=None, content_hash=None):
            ev = RiskEvent(
                source_document_id=doc.id,
                event_type="test",
                title="t",
                severity_score=0.5,
                content_hash=content_hash or f"ch-{when.isoformat()}-{mat_id}-{doc.id}",
            )
            s.add(ev); s.flush()
            # Force created_at to the test value
            ev.created_at = when
            if mat_id is not None:
                s.add(RiskEventMaterial(
                    risk_event_id=ev.id, material_id=mat_id,
                    relevance_score=1.0, match_reason="test",
                ))
            return ev

        # Lithium gets 10 events in last 30d (well above thin_events threshold of 5)
        for i in range(10):
            _ev(doc_a, NOW - timedelta(days=i + 1), materials_by_name["Lithium"].id,
                content_hash=f"li-30d-{i}")
        # Cobalt gets 3 events in last 30d (below thin_events threshold of 5)
        for i in range(3):
            _ev(doc_a, NOW - timedelta(days=i + 1), materials_by_name["Cobalt"].id,
                content_hash=f"co-30d-{i}")
        # Manganese gets 5 events in last 30d (at threshold)
        for i in range(5):
            _ev(doc_a, NOW - timedelta(days=i + 1), materials_by_name["Manganese"].id,
                content_hash=f"mn-30d-{i}")
        # Nickel + Natural Graphite + Copper + Aluminum get 5+ events in last 30d
        for name in ("Nickel", "Natural Graphite", "Copper", "Aluminum"):
            for i in range(6):
                _ev(doc_a, NOW - timedelta(days=i + 1), materials_by_name[name].id,
                    content_hash=f"{name.replace(' ', '')[:3]}-30d-{i}")

        # 3 events in [60d, 30d) — for prev_period_count
        for i in range(3):
            _ev(doc_b, OUT_60D - timedelta(days=i), materials_by_name["Lithium"].id,
                content_hash=f"li-prev30-{i}")

        # 1 ancient event — outside both windows
        _ev(doc_a, LONG_AGO, materials_by_name["Lithium"].id, content_hash="li-ancient")
        s.commit()

        # ── Seed facilities for materials we expect to pass the gap check.
        # Lithium / Natural Graphite / Copper get a facility with
        # annual_capacity_tpy set so they survive the no_facility_coverage
        # check.  Cobalt / Nickel / Manganese / Aluminum / Phosphate / Iron
        # Ore / REE intentionally get nothing → no_facility_coverage flag
        # fires on whichever of them is in the materials table.  Phosphate
        # in particular is the launch-blocker case we explicitly want to
        # verify.
        for name in ("Lithium", "Natural Graphite", "Copper"):
            fac = Facility(
                facility_type="mine",
                name=f"{name} test facility",
                country="AU",
                status="operating",
            )
            s.add(fac); s.flush()
            s.add(FacilityMaterialLink(
                facility_id=fac.id,
                material_id=materials_by_name[name].id,
                supply_chain_stage="ore",
                annual_capacity_tpy=10_000.0,
            ))
        s.commit()

        # ── K.1: core_minerals_scored ────────────────────────────────────
        print("Test K.1: _compute_core_minerals_scored")
        result = _compute_core_minerals_scored(s)
        if result.total != 10:
            failures.append(f"total={result.total}, expected 10")
        # Scored: Lithium, Cobalt, Nickel, Manganese, Natural Graphite, Copper = 6
        if result.scored != 6:
            failures.append(f"scored={result.scored}, expected 6")
        # Unscored: Aluminum (NULL), Phosphate, Iron Ore, REE
        expected_unscored = {
            "Aluminum",
            "Phosphate (Battery Grade)",
            "Iron Ore (LFP Grade)",
            "Rare Earth Elements",
        }
        if set(result.unscored_names) != expected_unscored:
            failures.append(
                f"unscored={set(result.unscored_names)}, expected {expected_unscored}"
            )
        else:
            print(f"  [OK] scored=6/10, unscored={sorted(result.unscored_names)}")

        # ── K.2: recent_risk_events_30d ──────────────────────────────────
        print("\nTest K.2: _compute_recent_risk_events_30d")
        result2 = _compute_recent_risk_events_30d(s, now=NOW)
        # 10 + 3 + 5 + 6*4 = 42 events in last 30d
        expected_count = 10 + 3 + 5 + 6 * 4
        if result2.count != expected_count:
            failures.append(f"count={result2.count}, expected {expected_count}")
        # 3 events in prev period
        if result2.prev_period_count != 3:
            failures.append(f"prev_period_count={result2.prev_period_count}, expected 3")
        # Top sources: GTA (42) > EUR-Lex (0 in this window)
        if not result2.top_sources or result2.top_sources[0].source_name != "GTA":
            failures.append(f"top_sources={result2.top_sources}, expected GTA first")
        else:
            print(f"  [OK] 30d={result2.count}, prev={result2.prev_period_count}, "
                  f"top={result2.top_sources[0].source_name}({result2.top_sources[0].count})")

        # ── K.3: coverage_gaps ───────────────────────────────────────────
        print("\nTest K.3: _compute_coverage_gaps")
        result3 = _compute_coverage_gaps(s, now=NOW)
        # Expected gaps per material:
        #   Lithium:          no gap (fresh score, 5 pillars, 10 events,
        #                     facility seeded) → NOT in result
        #   Cobalt:           thin_events + no_facility_coverage → IN result
        #   Nickel:           thin_pillars + no_facility_coverage → IN result
        #   Manganese:        stale_score + no_facility_coverage → IN result
        #   Natural Graphite: no gap (fresh + facility) → NOT in result
        #   Phosphate:        material_row_missing (not seeded; the
        #                     launch-blocker case) → IN result
        #   Iron Ore:         material_row_missing → IN result
        #   Copper:           no gap (fresh + facility) → NOT in result
        #   Aluminum:         no_global_score + no_facility_coverage → IN result
        #   REE:              material_row_missing → IN result
        # Total: 7 gap items
        gap_names = {m.canonical_name: m.reasons for m in result3.materials}
        if result3.count != 7:
            failures.append(f"gap count={result3.count}, expected 7. Got: {gap_names}")
        for name, expected_reasons_subset in [
            ("Cobalt", {"thin_events", "no_facility_coverage"}),
            ("Nickel", {"thin_pillars", "no_facility_coverage"}),
            ("Manganese", {"stale_score", "no_facility_coverage"}),
            ("Aluminum", {"no_global_score", "no_facility_coverage"}),
            ("Phosphate (Battery Grade)", {"material_row_missing"}),
            ("Iron Ore (LFP Grade)", {"material_row_missing"}),
            ("Rare Earth Elements", {"material_row_missing"}),
        ]:
            if name not in gap_names:
                failures.append(f"missing gap for {name}")
                continue
            reasons_present = set(gap_names[name])
            if not expected_reasons_subset.issubset(reasons_present):
                failures.append(
                    f"{name}: reasons {reasons_present} should include {expected_reasons_subset}"
                )
        # Verify materials that should NOT be flagged
        for clean_name in ("Lithium", "Natural Graphite", "Copper"):
            if clean_name in gap_names:
                failures.append(
                    f"{clean_name} should not be flagged as a gap; got {gap_names[clean_name]}"
                )
        if not [f for f in failures if "gap" in f.lower() or "thin" in f.lower() or "stale" in f.lower() or "no_global" in f.lower() or "missing" in f.lower()]:
            print(f"  [OK] 7 gaps detected with correct reasons")
            for name, reasons in sorted(gap_names.items()):
                print(f"        {name}: {sorted(reasons)}")

    print("\n" + "=" * 64)
    if failures:
        print("FAIL:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS — Dashboard launch-list KPIs:")
    print("  ✓ K.1 _compute_core_minerals_scored returns scored/total/unscored_names")
    print("  ✓ K.2 _compute_recent_risk_events_30d counts last 30d + prior 30d + top sources")
    print("  ✓ K.3 _compute_coverage_gaps detects no_global_score / stale_score / thin_events / thin_pillars / material_row_missing")
    return 0


if __name__ == "__main__":
    sys.exit(main())
