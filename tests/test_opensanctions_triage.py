"""OpenSanctions — provenance + suggestion-inversion tests (2026-07-31).

Real-database (SQLite) tests for the Phase 1 changes, complementing the
mock-based suite in test_opensanctions.py: snapshot provenance documents,
first_seen-anchored event dates, triage routing (listings pending,
geography aggregates display_only), and curation-surviving upserts.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.db.base import Base
from app.models.company import Company, CompanyAlias, CompanyMaterialExposure
from app.models.country import Country
from app.models.documents import SourceDocument
from app.models.regulatory import (
    RiskEvent,
    RiskEventCompany,
    RiskEventGeography,
    RiskEventMaterial,
)
from app.models.source import Source
from app.models.supply import Material, MaterialProductionShare
from app.services.ingestion.opensanctions import ingest_opensanctions


def _patch_sqlite_jsonb() -> None:
    from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler  # type: ignore[import]

    if not hasattr(SQLiteTypeCompiler, "visit_JSONB"):
        SQLiteTypeCompiler.visit_JSONB = SQLiteTypeCompiler.visit_JSON  # type: ignore[attr-defined]


_TABLES = (
    Country.__table__,
    Source.__table__,
    SourceDocument.__table__,
    Material.__table__,
    MaterialProductionShare.__table__,
    Company.__table__,
    CompanyAlias.__table__,
    CompanyMaterialExposure.__table__,
    RiskEvent.__table__,
    RiskEventCompany.__table__,
    RiskEventGeography.__table__,
    RiskEventMaterial.__table__,
)


@pytest.fixture()
def session() -> Session:
    _patch_sqlite_jsonb()
    engine = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(engine, tables=list(_TABLES))
    s = sessionmaker(bind=engine)()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


@pytest.fixture()
def seeded(session: Session) -> dict:
    session.add(Country(iso2="RU", iso3="RUS", name="Russia", common_names=["russia"]))
    m = Material(canonical_name="Nickel", category="metal")
    session.add(m)
    session.flush()
    c = Company(canonical_name="Norilsk Nickel")
    session.add(c)
    session.flush()
    session.add(CompanyMaterialExposure(
        company_id=c.id, material_id=m.id, exposure_score=0.9,
        supply_chain_stage="mining", source_geography="RU",
    ))
    session.add(MaterialProductionShare(
        country_code="RU", material_id=m.id, production_share=0.4,
        reference_year=2025,
    ))
    session.commit()
    return {"company": c, "material": m}


_FIRST_SEEN = datetime(2024, 5, 10, tzinfo=timezone.utc)


def _entity(**over):
    base = {
        "opensanctions_id": "os-1",
        "name": "Norilsk Nickel",
        "aliases": [],
        "leis": [],
        "countries": ["RU"],
        "datasets": ["us_ofac_sdn"],
        "first_seen": _FIRST_SEEN,
        "last_seen": None,
    }
    base.update(over)
    return base


def _run(session, entities, geos=None):
    with (
        patch("app.services.ingestion.opensanctions.download_sanctions_csv"),
        patch("app.services.ingestion.opensanctions.parse_sanctions_csv",
              return_value=entities),
    ):
        return ingest_opensanctions(
            session=session,
            high_concentration_geos=geos if geos is not None else [],
            min_interval_days=0,
        )


class TestProvenanceAndInversion:
    def test_snapshot_document_created_and_events_pointed(self, session, seeded):
        _run(session, [_entity()])
        src = session.scalar(select(Source).where(Source.name == "OpenSanctions"))
        assert src is not None
        docs = session.scalars(select(SourceDocument)).all()
        assert len(docs) == 1
        assert docs[0].external_id.startswith("opensanctions_snapshot_")
        assert docs[0].url == "https://www.opensanctions.org/datasets/sanctions/"
        ev = session.scalars(select(RiskEvent)).one()
        assert ev.source_document_id == docs[0].id

    def test_listing_lands_pending_with_suggestions_and_anchored_date(
        self, session, seeded
    ):
        _run(session, [_entity()])
        ev = session.scalars(select(RiskEvent)).one()
        assert ev.event_type == "sanctions_listing"
        assert ev.primary_category is None
        assert ev.suggested_category == "geopolitical_trade"
        assert ev.direction == "restrictive"
        assert ev.triage_status == "pending_triage"
        # Anchored to the entity's first_seen, NOT the run time.
        assert ev.event_date.date() == _FIRST_SEEN.date()
        assert ev.metadata_json["category_mapping"] == "hardcoded_source_rule"
        links = session.scalars(select(RiskEventMaterial)).all()
        assert links and all(l.status == "suggested" for l in links)

    def test_geography_aggregate_lands_display_only(self, session, seeded):
        _run(session, [_entity()], geos=["RU"])
        geo = session.scalar(select(RiskEvent).where(
            RiskEvent.event_type == "geography_sanctions_exposure"))
        assert geo is not None
        assert geo.triage_status == "display_only"
        assert geo.primary_category is None
        assert geo.metadata_json["triage_route"] == "auto_display_only_statistic"

    def test_upsert_preserves_confirmed_material_link(self, session, seeded):
        _run(session, [_entity()])
        ev = session.scalars(select(RiskEvent)).one()
        link = session.scalars(select(RiskEventMaterial)).one()
        # Human confirms the link in triage...
        link.status = "confirmed"
        link.relevance_score = 0.42   # human-adjusted weight
        session.commit()
        # ...then the next ingest run upserts the same event.
        _run(session, [_entity()])
        links = session.scalars(select(RiskEventMaterial)).all()
        assert len(links) == 1
        assert links[0].status == "confirmed"
        assert links[0].relevance_score == 0.42  # curation survived

    def test_upsert_repairs_null_provenance(self, session, seeded):
        _run(session, [_entity()])
        ev = session.scalars(select(RiskEvent)).one()
        ev.source_document_id = None  # simulate a pre-2026-07-31 orphan row
        session.commit()
        _run(session, [_entity()])
        ev = session.scalars(select(RiskEvent)).one()
        assert ev.source_document_id is not None
