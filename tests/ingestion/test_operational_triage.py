"""Tests for the operational triage flow (promote / reject / list).

The contracts:

1. Promotion with a taxonomy subtype fills severity from the ladder;
   an explicit severity overrides; a free-form subtype without severity
   errors.
2. Promotion flips exactly the right switches: primary_category,
   event_subtype, severity_score, verified, metadata (scoring flag gone,
   triage audit present) — and the event would now pass the scoring
   pillar's gates.
3. Facility attach at promotion mirrors the ingester's anchoring
   (facility + geo + material + operator links) and never duplicates.
4. Rejection keeps the row, blocks scoring, and drops it from the queue.
5. Triage refuses to touch non-candidate events (walkthrough/manual rows).
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.constants import OPERATIONAL_SUBTYPE_DEFAULT_SEVERITY
from app.db.base import Base
from app.models import (
    Company,
    CompanyFacility,
    Facility,
    FacilityMaterialLink,
    Material,
    RiskEvent,
    RiskEventCompany,
    RiskEventFacility,
    RiskEventGeography,
    RiskEventMaterial,
)
from app.services.ingestion.ingest_operational_news import EVENT_TYPE_CANDIDATE
from app.services.ingestion.operational_triage import (
    TriageError,
    list_pending_candidates,
    promote_candidate,
    reject_candidate,
)


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(engine)
    Session_ = sessionmaker(bind=engine)
    s = Session_()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


def _make_candidate(session: Session, title: str = "Mine halted") -> RiskEvent:
    ev = RiskEvent(
        event_type=EVENT_TYPE_CANDIDATE,
        title=title,
        risk_categories_json=["operational"],
        metadata_json={
            "scoring": "display_only",
            "display_only_reason": "pending_triage",
            "source_feed": "google_news",
            "suggested_subtype": "operations_halt",
        },
        verified=False,
    )
    ev.primary_category = None
    session.add(ev)
    session.flush()
    return ev


@pytest.fixture()
def curated_facility(session: Session) -> dict:
    mat = Material(canonical_name="Nickel", category="metal")
    op = Company(canonical_name="Vale")
    session.add_all([mat, op])
    session.flush()
    fac = Facility(
        facility_type="mine", name="Onca Puma", country="BR",
        status="operating", data_source="partner_facility_seed",
    )
    session.add(fac)
    session.flush()
    session.add(CompanyFacility(company_id=op.id, facility_id=fac.id))
    session.add(FacilityMaterialLink(
        facility_id=fac.id, material_id=mat.id, is_primary_product=True,
    ))
    session.flush()
    return {"facility": fac, "material": mat, "company": op}


class TestPromotion:
    def test_taxonomy_default_severity(self, session):
        ev = _make_candidate(session)
        result = promote_candidate(session, ev.id, subtype="facility_shutdown")
        assert result["severity"] == OPERATIONAL_SUBTYPE_DEFAULT_SEVERITY["facility_shutdown"]
        assert result["severity_source"] == "taxonomy_default"
        assert ev.primary_category == "operational"
        assert ev.event_subtype == "facility_shutdown"
        assert ev.severity_score == 0.70
        assert ev.verified is True
        # Display-only guards removed; audit trail present.
        assert "scoring" not in ev.metadata_json
        assert "display_only_reason" not in ev.metadata_json
        assert ev.metadata_json["triage"]["action"] == "promoted"

    def test_explicit_severity_overrides_default(self, session):
        ev = _make_candidate(session)
        result = promote_candidate(
            session, ev.id, subtype="labor_strike", severity=0.9, note="Escondida-scale",
        )
        assert result["severity"] == 0.9
        assert result["severity_source"] == "override"
        assert ev.metadata_json["triage"]["note"] == "Escondida-scale"

    def test_freeform_subtype_requires_explicit_severity(self, session):
        ev = _make_candidate(session)
        with pytest.raises(TriageError, match="not in the controlled taxonomy"):
            promote_candidate(session, ev.id, subtype="bespoke_walkthrough_thing")
        # With a severity it's allowed (walkthrough vocabulary stays open).
        result = promote_candidate(
            session, ev.id, subtype="bespoke_walkthrough_thing", severity=0.42,
        )
        assert result["severity_source"] == "explicit"

    def test_severity_range_checked(self, session):
        ev = _make_candidate(session)
        with pytest.raises(TriageError, match=r"\[0, 1\]"):
            promote_candidate(session, ev.id, subtype="incident", severity=1.5)

    def test_double_promotion_refused(self, session):
        ev = _make_candidate(session)
        promote_candidate(session, ev.id, subtype="incident")
        with pytest.raises(TriageError, match="already promoted"):
            promote_candidate(session, ev.id, subtype="incident")

    def test_non_candidate_events_refused(self, session):
        manual = RiskEvent(
            event_type="OPERATIONAL_DISRUPTION", title="Walkthrough event",
        )
        session.add(manual)
        session.flush()
        with pytest.raises(TriageError, match="not an 'operational_news_candidate'"):
            promote_candidate(session, manual.id, subtype="incident")

    def test_facility_attach_mirrors_ingester_anchoring(self, session, curated_facility):
        ev = _make_candidate(session)
        result = promote_candidate(
            session, ev.id, subtype="permit_or_court_stoppage",
            facility_name="Onca Puma",
        )
        assert result["links_added"] == {
            "facility": 1, "geography": 1, "material": 1, "company": 1,
        }
        fac_link = session.scalars(select(RiskEventFacility).where(
            RiskEventFacility.risk_event_id == ev.id)).one()
        assert fac_link.match_reason == "triage_attach"
        geo = session.scalars(select(RiskEventGeography).where(
            RiskEventGeography.risk_event_id == ev.id)).one()
        assert (geo.country_code, geo.geography_context) == ("BR", "primary")
        mat = session.scalars(select(RiskEventMaterial).where(
            RiskEventMaterial.risk_event_id == ev.id)).one()
        assert mat.material_id == curated_facility["material"].id
        comp = session.scalars(select(RiskEventCompany).where(
            RiskEventCompany.risk_event_id == ev.id)).one()
        assert (comp.relevance_score, comp.match_reason) == (0.85, "facility_operator")

    def test_facility_attach_unknown_or_mrds_refused(self, session, curated_facility):
        mrds = Facility(
            facility_type="mine", name="MRDS Site", country="US",
            status="operating", data_source="mrds", mrds_dep_id="123",
        )
        session.add(mrds)
        session.flush()
        ev = _make_candidate(session)
        with pytest.raises(TriageError, match="No curated facility"):
            promote_candidate(
                session, ev.id, subtype="incident", facility_name="MRDS Site",
            )


class TestRejection:
    def test_reject_keeps_row_and_blocks_scoring(self, session):
        ev = _make_candidate(session)
        reject_candidate(session, ev.id, reason="Duplicate coverage of GTA event")
        assert ev.primary_category is None
        assert ev.metadata_json["display_only_reason"] == "rejected"
        assert ev.metadata_json["triage"]["reason"] == "Duplicate coverage of GTA event"

    def test_reject_requires_reason(self, session):
        ev = _make_candidate(session)
        with pytest.raises(TriageError, match="reason is required"):
            reject_candidate(session, ev.id, reason="  ")

    def test_cannot_reject_promoted_event(self, session):
        ev = _make_candidate(session)
        promote_candidate(session, ev.id, subtype="incident")
        with pytest.raises(TriageError, match="already promoted"):
            reject_candidate(session, ev.id, reason="changed my mind")


class TestQueue:
    def test_queue_shows_pending_only(self, session):
        pending = _make_candidate(session, title="Pending one")
        promoted = _make_candidate(session, title="Promoted one")
        rejected = _make_candidate(session, title="Rejected one")
        promote_candidate(session, promoted.id, subtype="incident")
        reject_candidate(session, rejected.id, reason="noise")

        rows = list_pending_candidates(session)
        assert [r["title"] for r in rows] == ["Pending one"]
        assert rows[0]["suggested_subtype"] == "operations_halt"
        assert rows[0]["feed"] == "google_news"
