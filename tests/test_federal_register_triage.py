"""Federal Register — suggestion inversion + procedural demotion tests.

Written 2026-07-31 (triage plan Phase 1).  The FR ingester previously had
no ingest-level tests; these cover the two behaviours added by the
inversion: suggestion fields instead of authoritative assignments, and the
AD/CVD procedural-step demotion (decided by Nicole 2026-07-31 after the
Türkiye-aluminum four-events-in-a-week finding).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.db.base import Base
from app.models.documents import SourceDocument
from app.models.regulatory import RiskEvent
from app.models.source import Source
from app.services.ingestion.ingest_federal_register import (
    _PROCEDURAL_SEVERITY_CAP,
    _get_or_create_source,
    _insert_risk_event,
    _is_procedural_step,
    TARGETED_QUERIES,
)


def _patch_sqlite_jsonb() -> None:
    from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler  # type: ignore[import]

    if not hasattr(SQLiteTypeCompiler, "visit_JSONB"):
        SQLiteTypeCompiler.visit_JSONB = SQLiteTypeCompiler.visit_JSON  # type: ignore[attr-defined]


@pytest.fixture()
def session() -> Session:
    _patch_sqlite_jsonb()
    engine = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(
        engine,
        tables=[Source.__table__, SourceDocument.__table__, RiskEvent.__table__],
    )
    s = sessionmaker(bind=engine)()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


def _make_doc(session: Session, title: str) -> SourceDocument:
    source = _get_or_create_source(session)
    doc = SourceDocument(
        source_id=source.id,
        external_id=f"test-{abs(hash(title))}",
        title=title,
        document_type="api_json",
    )
    session.add(doc)
    session.flush()
    return doc


def _insert(session: Session, title: str, severity: float = 0.54) -> RiskEvent:
    doc = _make_doc(session, title)
    query = TARGETED_QUERIES[2]  # trade_remedy → TARIFF
    return _insert_risk_event(
        session,
        doc=doc,
        title=title,
        summary="test",
        event_date=datetime(2026, 7, 1, tzinfo=timezone.utc),
        severity=severity,
        risk_categories=list(query.risk_categories),
        query=query,
        geo_primary="US",
        agencies=["international-trade-administration"],
        content_hash=f"hash-{abs(hash(title))}",
    )


class TestProceduralDetection:
    @pytest.mark.parametrize("title", [
        "Common Alloy Aluminum Sheet From Türkiye: Postponement of Preliminary Determination",
        "Certain Aluminum Foil From China: Preliminary Results of Antidumping Duty Review",
        "Silicon Metal From Australia; Supplemental Schedule for the Final Determination",
        "Common Alloy Aluminum Sheet From Türkiye: Notice of Court Decision",
        "Large Diameter Graphite Electrodes From India: Rescission of Review",
        "Opportunity To Request Administrative Review of Antidumping Orders",
    ])
    def test_procedural_titles_detected(self, title):
        assert _is_procedural_step(title) is True

    @pytest.mark.parametrize("title", [
        "Silicon Metal From Bosnia and Herzegovina: Final Results of Review",
        "Initiation of Less-Than-Fair-Value Investigation: Graphite Electrodes From India",
        "Imposition of Antidumping Duties on Aluminum Foil From China",
        "Addition of Entities to the Entity List",
    ])
    def test_substantive_titles_not_detected(self, title):
        # Final results and new investigations/measures stay full events.
        assert _is_procedural_step(title) is False


class TestInversionOnInsert:
    def test_substantive_event_lands_pending_with_suggestions(self, session):
        ev = _insert(session, "Imposition of Antidumping Duties on Aluminum Foil From China")
        assert ev.primary_category is None
        assert ev.suggested_category == "geopolitical_trade"
        assert ev.direction == "restrictive"
        assert ev.triage_status == "pending_triage"
        assert ev.severity_score == 0.54
        assert ev.metadata_json["category_mapping"] == "query_config"
        assert ev.metadata_json["is_procedural_step"] is False

    def test_procedural_step_demoted_to_display_only(self, session):
        ev = _insert(
            session,
            "Common Alloy Aluminum Sheet From Türkiye: Postponement of Preliminary Determination",
        )
        assert ev.triage_status == "display_only"
        assert ev.severity_score <= _PROCEDURAL_SEVERITY_CAP
        assert ev.primary_category is None
        assert ev.metadata_json["triage_route"] == "auto_display_only_procedural"
        assert ev.metadata_json["is_procedural_step"] is True
