"""Verify pipeline.py._add_risk_event is now idempotent (2026-05-09).

Before: every call inserted a fresh RiskEvent with no content_hash and
no existence check.  Re-running Census trade against the same data
duplicated events indefinitely.

After: content_hash is computed from (title, summary, event_date) and
checked against existing rows.  Second call with the same draft returns
the prior event without inserting.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Optional

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
    from app.models.documents import SourceDocument
    from app.models.regulatory import (
        RiskEvent,
        RiskEventCompany,
        RiskEventHsMapping,
        RiskEventMaterial,
    )
    from app.models.source import Source
    from app.models.supply import (
        HsCodeMaterialMapping,
        Material,
    )
    from app.services.ingestion.normalizers.event_normalizer import RiskEventDraft

    print("=== pipeline._add_risk_event idempotency ===\n")

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=[
            Source.__table__,
            SourceDocument.__table__,
            Material.__table__,
            HsCodeMaterialMapping.__table__,
            RiskEvent.__table__,
            RiskEventMaterial.__table__,
            RiskEventHsMapping.__table__,
            RiskEventCompany.__table__,
        ],
    )

    with Session(engine) as s:
        # Seed minimal source + document + material
        src = Source(
            name="Census", source_type="census", phase="1",
            is_active=True, config_json={},
        )
        s.add(src); s.flush()
        doc = SourceDocument(
            source_id=src.id, external_id="census_test",
            title="Census trade test", url="https://example.test",
            document_type="trade_data", metadata_json={},
        )
        s.add(doc); s.flush()
        mat = Material(canonical_name="Lithium", category="cathode_active")
        s.add(mat); s.flush()
        hsmap = HsCodeMaterialMapping(
            hs_code_prefix="283691", material_id=mat.id,
            confidence=1.0, digit_count=6, market_scope="global",
        )
        s.add(hsmap); s.flush()
        s.commit()

        # Stand up a minimal pipeline harness — we don't need the full
        # IngestionPipeline class to exercise _add_risk_event since it
        # only reads/writes the session.  Bind via a small subclass shim.
        from app.services.ingestion.pipeline import IngestionPipeline

        # Build a real pipeline but inject a session.  IngestionPipeline
        # accepts a session_factory; we cheat with a closure.
        class _StubPipeline:
            _db = s
            _add_risk_event = IngestionPipeline._add_risk_event

        pipeline = _StubPipeline()

        # Build a deterministic draft
        draft = RiskEventDraft(
            event_type="GEOPOLITICAL_TRADE",
            title="Census trade test event",
            summary="Lithium imports from CL spiked 40% YoY in Q3 2024.",
            event_date=datetime(2024, 9, 30, tzinfo=timezone.utc),
            severity_score=0.6,
            confidence_score=0.85,
            risk_categories=["geopolitical_trade"],
            geography={"primary": "CL"},
            metadata={"sample": True},
        )

        # ── T.1: first call inserts; content_hash set ─────────────────
        print("Test T.1: first call inserts a fresh RiskEvent with content_hash")
        ev1 = pipeline._add_risk_event(
            doc, draft, [],
            material_id=mat.id, hs_mapping_id=hsmap.id,
            mapping_confidence=1.0,
        )
        s.commit()
        if ev1.content_hash is None or len(ev1.content_hash) < 32:
            failures.append(f"content_hash not set or too short: {ev1.content_hash}")
        else:
            print(f"  [OK] event_id={ev1.id} inserted with content_hash={ev1.content_hash[:16]}...")
        # Junction rows present
        mat_links = s.execute(
            select(func.count()).select_from(RiskEventMaterial).where(
                RiskEventMaterial.risk_event_id == ev1.id
            )
        ).scalar_one()
        hs_links = s.execute(
            select(func.count()).select_from(RiskEventHsMapping).where(
                RiskEventHsMapping.risk_event_id == ev1.id
            )
        ).scalar_one()
        if mat_links != 1 or hs_links != 1:
            failures.append(f"junction rows mismatch: mat={mat_links}, hs={hs_links}")
        else:
            print(f"  [OK] 1 RiskEventMaterial + 1 RiskEventHsMapping junction written")

        # ── T.2: second call with same draft returns existing, no new row ──
        print("\nTest T.2: second call with same draft is a no-op")
        n_events_before = s.execute(
            select(func.count()).select_from(RiskEvent)
        ).scalar_one()

        ev2 = pipeline._add_risk_event(
            doc, draft, [],
            material_id=mat.id, hs_mapping_id=hsmap.id,
            mapping_confidence=1.0,
        )
        s.commit()

        n_events_after = s.execute(
            select(func.count()).select_from(RiskEvent)
        ).scalar_one()

        if n_events_after != n_events_before:
            failures.append(
                f"second call inserted a duplicate: {n_events_before} → {n_events_after}"
            )
        elif ev2.id != ev1.id:
            failures.append(f"returned event_id={ev2.id}, expected existing {ev1.id}")
        else:
            print(f"  [OK] event count unchanged ({n_events_after}); same event_id returned")

        # No duplicate junction rows
        mat_links_after = s.execute(
            select(func.count()).select_from(RiskEventMaterial).where(
                RiskEventMaterial.risk_event_id == ev1.id
            )
        ).scalar_one()
        if mat_links_after != 1:
            failures.append(
                f"duplicate junctions: {mat_links_after} RiskEventMaterial rows, expected 1"
            )
        else:
            print(f"  [OK] no duplicate junction rows (still 1 each)")

        # ── T.3: different draft → new event ──────────────────────────
        print("\nTest T.3: different draft inserts a new event")
        draft2 = RiskEventDraft(
            event_type="GEOPOLITICAL_TRADE",
            title="Different Census event",
            summary="Cobalt YoY change.",
            event_date=datetime(2024, 12, 31, tzinfo=timezone.utc),
            severity_score=0.5,
            confidence_score=0.8,
            risk_categories=["geopolitical_trade"],
            geography={"primary": "CD"},
            metadata={},
        )
        ev3 = pipeline._add_risk_event(doc, draft2, [])
        s.commit()
        if ev3.id == ev1.id:
            failures.append(f"different draft returned existing event_id={ev3.id}")
        elif ev3.content_hash == ev1.content_hash:
            failures.append("different draft produced same content_hash")
        else:
            print(f"  [OK] new event_id={ev3.id}, distinct content_hash")

        # ── T.4: draft with None summary / None event_date still hashes ──
        print("\nTest T.4: hash is stable with None summary / event_date")
        draft_partial = RiskEventDraft(
            event_type="REG",
            title="Standing regulatory obligation",
            summary=None,
            event_date=None,
            severity_score=0.4,
            confidence_score=1.0,
            risk_categories=["regulatory_compliance"],
            geography={},
            metadata={},
        )
        ev4 = pipeline._add_risk_event(doc, draft_partial, [])
        s.commit()
        if ev4.content_hash is None:
            failures.append("content_hash None for None-summary/date draft")
        else:
            # Re-run with same partial draft
            ev5 = pipeline._add_risk_event(doc, draft_partial, [])
            s.commit()
            if ev5.id != ev4.id:
                failures.append(
                    f"None-summary draft re-run created new event ({ev4.id} → {ev5.id})"
                )
            else:
                print(f"  [OK] None summary/event_date → stable hash; re-run is no-op")

    print("\n" + "=" * 64)
    if failures:
        print("FAIL:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS — pipeline._add_risk_event idempotency:")
    print("  ✓ content_hash computed and persisted")
    print("  ✓ second call with same draft → existing event returned (no new row)")
    print("  ✓ no duplicate junction-row violations")
    print("  ✓ different draft → new event inserted")
    print("  ✓ None summary / None event_date hash cleanly and re-run is no-op")
    return 0


if __name__ == "__main__":
    sys.exit(main())
