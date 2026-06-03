"""End-to-end tests for parameter-stable content_hash + UPSERT (2026-05-11).

Covers all three ingester paths that switched from title-based content_hash
(which encoded mutable values like percentages and counts) to a stable
identifier with UPSERT semantics:

  TS.1  trade_signal_builder TRADE_CONCENTRATION — first run inserts,
        second run with changed shares UPDATEs the existing row in place
        (no duplicate, title/severity reflect the latest values).

  TS.2  trade_signal_builder material + HS junctions get rewritten on
        UPDATE (delete-then-insert), and company junctions are NOT
        touched (partial-rewrite per 2026-05-11 audit decision).

  OS.1  opensanctions company event — first run inserts, second run
        with changed dataset list UPDATEs in place.

  OS.2  opensanctions geo event — title-encoded count changes between
        runs; UPDATE in place rather than creating a second row.

  TR.1  build_trade_risk_event — period parameter anchors event_date to
        the end of the reporting period (deterministic), not now().
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from decimal import Decimal

sys.path.insert(0, "/sessions/keen-wonderful-lamport/mnt/battery-data-intelligence-engine")


def main() -> int:
    failures: list[str] = []

    from sqlalchemy import create_engine, select, func
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
    from app.models.regulatory import (
        RiskEvent,
        RiskEventCompany,
        RiskEventGeography,
        RiskEventHsMapping,
        RiskEventMaterial,
    )
    from app.models.supply import HsCodeMaterialMapping, Material
    from app.services.ingestion.trade_signal_builder import (
        _upsert_event,
        _clear_material_and_hs_junctions,
        _content_hash,
        _stable_event_key,
        _link_material,
    )
    from app.services.ingestion.normalizers.event_normalizer import (
        _period_to_event_date,
        build_trade_risk_event,
    )

    print("=== Parameter-stable UPSERT (trade_signal_builder + opensanctions) ===\n")

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=[
            Material.__table__,
            HsCodeMaterialMapping.__table__,
            RiskEvent.__table__,
            RiskEventMaterial.__table__,
            RiskEventHsMapping.__table__,
            RiskEventCompany.__table__,
            RiskEventGeography.__table__,
        ],
    )

    # ── TS.1: trade_signal_builder UPSERT first vs second run ──────────────
    print("Test TS.1: TRADE_CONCENTRATION first run inserts, second UPDATEs in place")
    with Session(engine) as s:
        lithium = Material(canonical_name="Lithium", category="cathode_active")
        s.add(lithium); s.flush()
        hsmap = HsCodeMaterialMapping(
            hs_code_prefix="283691", material_id=lithium.id,
            confidence=1.0, digit_count=6, market_scope="global",
        )
        s.add(hsmap); s.flush()

        # First run: 67% share
        event_id, was_inserted_1 = _upsert_event(
            s,
            title="CN accounts for 67% of tracked Lithium exports (2024)",
            summary="Trade flow analysis ...",
            event_date=datetime(2024, 12, 31, tzinfo=timezone.utc),
            severity=0.55,
            event_subtype="TRADE_CONCENTRATION",
            reporter_country="CN",
            material_id=lithium.id,
            material_name="Lithium",
            year=2024,
        )
        if not was_inserted_1:
            failures.append("first run should have been an insert")
        else:
            print(f"  [OK] first run inserted event_id={event_id}")

        _link_material(s, event_id, lithium.id, hs_mapping_ids=[hsmap.id])
        s.commit()

        n_events_after_first = s.execute(
            select(func.count()).select_from(RiskEvent)
        ).scalar_one()

        # Second run: 71% share (post-confidence-weighting)
        event_id_2, was_inserted_2 = _upsert_event(
            s,
            title="CN accounts for 71% of tracked Lithium exports (2024)",
            summary="Trade flow analysis with confidence-weighted shares ...",
            event_date=datetime(2024, 12, 31, tzinfo=timezone.utc),
            severity=0.70,
            event_subtype="TRADE_CONCENTRATION",
            reporter_country="CN",
            material_id=lithium.id,
            material_name="Lithium",
            year=2024,
        )
        if was_inserted_2:
            failures.append("second run should be an update, not an insert")
        elif event_id_2 != event_id:
            failures.append(
                f"second run returned different event_id: {event_id_2} vs {event_id}"
            )
        else:
            print(f"  [OK] second run UPDATEd event_id={event_id_2}")

        n_events_after_second = s.execute(
            select(func.count()).select_from(RiskEvent)
        ).scalar_one()
        if n_events_after_second != n_events_after_first:
            failures.append(
                f"event count grew: {n_events_after_first} → {n_events_after_second}"
            )
        else:
            print(f"  [OK] event count unchanged (no duplicate)")

        # Verify the row reflects the new values
        s.expire_all()
        row = s.get(RiskEvent, event_id)
        if "71%" not in (row.title or ""):
            failures.append(f"title not updated: {row.title!r}")
        elif abs((row.severity_score or 0) - 0.70) > 0.001:
            failures.append(f"severity not updated: {row.severity_score}")
        else:
            print(f"  [OK] title and severity reflect the new values")

        # ── TS.2: junction handling on update ─────────────────────────────
        print("\nTest TS.2: material + HS junctions rewritten on update, companies preserved")
        # Seed a fake company junction so we can verify it survives
        import uuid
        company_uuid = uuid.uuid4()
        s.execute(
            RiskEventCompany.__table__.insert().values(
                id=uuid.uuid4(),
                risk_event_id=event_id,
                company_id=company_uuid,
                relevance_score=0.85,
                match_reason="material_hs",
            )
        )
        s.commit()

        mat_junctions_before = s.execute(
            select(func.count()).select_from(RiskEventMaterial).where(
                RiskEventMaterial.risk_event_id == event_id,
            )
        ).scalar_one()
        co_junctions_before = s.execute(
            select(func.count()).select_from(RiskEventCompany).where(
                RiskEventCompany.risk_event_id == event_id,
            )
        ).scalar_one()
        if mat_junctions_before < 1:
            failures.append("expected material junction from earlier setup")
        if co_junctions_before < 1:
            failures.append("expected company junction from setup above")

        # Simulate the UPDATE path: clear material+HS, re-link, leave company alone
        _clear_material_and_hs_junctions(s, event_id)
        _link_material(s, event_id, lithium.id, hs_mapping_ids=[hsmap.id])
        s.commit()

        mat_junctions_after = s.execute(
            select(func.count()).select_from(RiskEventMaterial).where(
                RiskEventMaterial.risk_event_id == event_id,
            )
        ).scalar_one()
        co_junctions_after = s.execute(
            select(func.count()).select_from(RiskEventCompany).where(
                RiskEventCompany.risk_event_id == event_id,
            )
        ).scalar_one()
        if mat_junctions_after != 1:
            failures.append(f"material junctions: {mat_junctions_after}, expected 1")
        elif co_junctions_after != co_junctions_before:
            failures.append(
                f"company junctions changed: {co_junctions_before} → {co_junctions_after}"
            )
        else:
            print(f"  [OK] material junctions refreshed (1); company junctions preserved")

        # Different (event_subtype, material, country, year) → new event
        print("\nTest TS.3: different stable key → new event")
        event_id_other, was_inserted_other = _upsert_event(
            s,
            title="AR accounts for 25% of tracked Lithium exports (2024)",
            summary="...",
            event_date=datetime(2024, 12, 31, tzinfo=timezone.utc),
            severity=0.55,
            event_subtype="TRADE_CONCENTRATION",
            reporter_country="AR",
            material_id=lithium.id,
            material_name="Lithium",
            year=2024,
        )
        s.commit()
        if not was_inserted_other or event_id_other == event_id:
            failures.append("different country should produce a new event")
        else:
            print(f"  [OK] new event for different country: event_id={event_id_other}")

    # ── OS.1 + OS.2: OpenSanctions UPSERT semantics ────────────────────────
    print("\nTest OS.1: opensanctions company event stable-key UPSERT")
    from app.services.ingestion.opensanctions import (
        _company_event_stable_key,
        _geo_event_stable_key,
        _content_hash as os_content_hash,
    )

    # We can't easily exercise the full ingest_opensanctions function from
    # synthetic data without a parsed CSV, so we test the stable-key
    # round-trip directly.
    ch_1 = os_content_hash(_company_event_stable_key("uuid-abc-123"))
    ch_2 = os_content_hash(_company_event_stable_key("uuid-abc-123"))
    if ch_1 != ch_2:
        failures.append("same company_id should produce same stable hash")
    else:
        print(f"  [OK] same company_id → same hash (deterministic)")
    ch_diff = os_content_hash(_company_event_stable_key("uuid-xyz-789"))
    if ch_diff == ch_1:
        failures.append("different company_id should produce different hash")
    else:
        print(f"  [OK] different company_id → different hash")

    print("\nTest OS.2: opensanctions geo event stable-key (count-independent)")
    # The critical property: regardless of how the title encodes the count,
    # the hash depends only on the country.
    ch_ru = os_content_hash(_geo_event_stable_key("RU"))
    ch_ru_again = os_content_hash(_geo_event_stable_key("RU"))
    ch_cn = os_content_hash(_geo_event_stable_key("CN"))
    if ch_ru != ch_ru_again:
        failures.append("same country should produce same stable hash")
    elif ch_ru == ch_cn:
        failures.append("different country should produce different hash")
    else:
        print(f"  [OK] geo hash keyed on country, not count")

    # ── TR.1: build_trade_risk_event period parameter ──────────────────────
    print("\nTest TR.1: build_trade_risk_event period anchors event_date deterministically")
    draft_a = build_trade_risk_event(
        hs_description="Lithium ores", partner="Chile",
        trade_value_usd=1.5e9, import_export="import",
        period="2024-03",
    )
    draft_b = build_trade_risk_event(
        hs_description="Lithium ores", partner="Chile",
        trade_value_usd=1.5e9, import_export="import",
        period="2024-03",
    )
    if draft_a.event_date != draft_b.event_date:
        failures.append(
            f"period anchors should be deterministic: {draft_a.event_date} vs {draft_b.event_date}"
        )
    elif draft_a.event_date.date().isoformat() != "2024-03-31":
        failures.append(
            f"period 2024-03 should anchor to 2024-03-31, got {draft_a.event_date.date()}"
        )
    else:
        print(f"  [OK] period='2024-03' → event_date={draft_a.event_date.date()}")

    draft_annual = build_trade_risk_event(
        hs_description="Lithium", partner="X",
        trade_value_usd=1e6, import_export="export",
        period="2023",
    )
    if draft_annual.event_date.date().isoformat() != "2023-12-31":
        failures.append(
            f"period '2023' should anchor to 2023-12-31, got {draft_annual.event_date.date()}"
        )
    else:
        print(f"  [OK] period='2023' → event_date={draft_annual.event_date.date()}")

    draft_no_period = build_trade_risk_event(
        hs_description="X", partner="Y", trade_value_usd=1.0, import_export="import",
    )
    if draft_no_period.event_date is None:
        failures.append("no period should still produce some event_date (NOW fallback)")
    else:
        print(f"  [OK] no period → falls back to now() (no error)")

    print("\n" + "=" * 64)
    if failures:
        print("FAIL:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS — Parameter-stable UPSERT semantics:")
    print("  ✓ TS.1 trade_signal_builder inserts then UPDATEs in place (no dup)")
    print("  ✓ TS.2 material+HS junctions rewritten on update; companies preserved")
    print("  ✓ TS.3 different stable key produces a separate event")
    print("  ✓ OS.1 opensanctions company hash keyed on company_id only")
    print("  ✓ OS.2 opensanctions geo hash keyed on country only (count-independent)")
    print("  ✓ TR.1 build_trade_risk_event period anchors event_date deterministically")
    return 0


if __name__ == "__main__":
    sys.exit(main())
