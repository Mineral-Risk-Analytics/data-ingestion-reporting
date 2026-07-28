"""Tests for the triage queue's duplicate-candidate hints (2026-07-28).

Why this exists: nothing in the pipeline stops the partner from curating a
halt by hand in the walkthrough and the news ingester landing the same halt
a day later from a wire story. ``content_hash`` only catches byte-identical
re-ingestion, and ``primary_category`` only stops double-counting when both
rows land in the SAME pillar. If the reviewer promotes the news candidate,
the operational pillar counts one physical outage twice — and because V1
operational is a top-3 mean, a duplicate in a thin cell moves the score
materially.

The contracts:

1. A candidate sharing an entity anchor with a similar existing event is
   hinted, and the hint says whether that event ALREADY SCORES.
2. Unrelated events (different material, different story) are not hinted.
3. A candidate with no material/facility/company links reports
   ``checked=False`` — honestly "could not check", never a silent pass.
4. ``checked=True, hints=[]`` (a real negative) is distinguishable from (3).
5. Rejected and pending-triage neighbours are hinted but tagged as such,
   so the reviewer knows a promotion would NOT double-count scoring.
6. Events already marked ``duplicate_of_id`` are excluded from the pool.
7. The hints are advisory: they never remove a row from the queue, and
   ``check_duplicates=False`` omits the block entirely.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.db.base import Base
from app.models import (
    Company,
    Facility,
    Material,
    RiskEvent,
    RiskEventCompany,
    RiskEventFacility,
    RiskEventMaterial,
)
from app.services.ingestion.event_dedupe import find_similar_events
from app.services.ingestion.ingest_operational_news import EVENT_TYPE_CANDIDATE
from app.services.ingestion.operational_triage import (
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


@pytest.fixture()
def world(session: Session) -> dict:
    """Two materials, two facilities, one operator — enough to separate a
    genuine near-duplicate from a same-material-different-story neighbour."""
    nickel = Material(canonical_name="Nickel", category="metal")
    cobalt = Material(canonical_name="Cobalt", category="metal")
    vale = Company(canonical_name="Vale")
    session.add_all([nickel, cobalt, vale])
    session.flush()
    onca = Facility(
        facility_type="mine", name="Onca Puma", country="BR",
        status="operating", data_source="partner_facility_seed",
    )
    session.add(onca)
    session.flush()
    return {"nickel": nickel, "cobalt": cobalt, "vale": vale, "facility": onca}


def _event(
    session: Session,
    title: str,
    *,
    event_type: str = "OPERATIONAL_DISRUPTION",
    subtype: str | None = None,
    category: str | None = "operational",
    date: datetime | None = None,
    material: Material | None = None,
    facility: Facility | None = None,
    company: Company | None = None,
    meta: dict | None = None,
) -> RiskEvent:
    ev = RiskEvent(
        event_type=event_type,
        title=title,
        event_subtype=subtype,
        event_date=date or datetime(2026, 7, 20),
        risk_categories_json=["operational"],
        metadata_json=meta or {},
    )
    ev.primary_category = category
    session.add(ev)
    session.flush()
    if material is not None:
        session.add(RiskEventMaterial(
            risk_event_id=ev.id, material_id=material.id,
            relevance_score=1.0, is_direct=True, match_reason="test",
        ))
    if facility is not None:
        session.add(RiskEventFacility(
            risk_event_id=ev.id, facility_id=facility.id,
            relevance_score=1.0, match_reason="test",
        ))
    if company is not None:
        session.add(RiskEventCompany(
            risk_event_id=ev.id, company_id=company.id,
            relevance_score=1.0, match_reason="test",
        ))
    session.flush()
    return ev


def _candidate(session: Session, title: str, **kw) -> RiskEvent:
    kw.setdefault("meta", {
        "scoring": "display_only",
        "display_only_reason": "pending_triage",
        "source_feed": "google_news",
        "suggested_subtype": "operations_halt",
    })
    return _event(
        session, title,
        event_type=EVENT_TYPE_CANDIDATE, category=None, **kw,
    )


class TestHitsAndMisses:
    def test_hits_a_scoring_event_about_the_same_story(self, session, world):
        curated = _event(
            session, "Vale suspends nickel production at Onca Puma mine",
            subtype="facility_shutdown", date=datetime(2026, 7, 18),
            material=world["nickel"], facility=world["facility"],
        )
        cand = _candidate(
            session, "Vale suspends nickel output at Onca Puma after furnace fault",
            date=datetime(2026, 7, 20),
            material=world["nickel"], facility=world["facility"],
        )

        result = find_similar_events(session, [cand.id])[cand.id]
        assert result["checked"] is True
        ids = [h["event_id"] for h in result["hints"]]
        assert curated.id in ids
        hit = next(h for h in result["hints"] if h["event_id"] == curated.id)
        # The reviewer's decisive fact: promoting would double-count.
        assert hit["scores_already"] is True
        assert hit["status"] == "scoring"
        assert hit["similarity"] >= 0.35
        assert "Nickel" in hit["shared"] and "Onca Puma" in hit["shared"]

    def test_unrelated_event_is_not_hinted(self, session, world):
        _event(
            session, "Cobalt refinery expansion approved in Kolwezi",
            material=world["cobalt"],
        )
        cand = _candidate(
            session, "Vale suspends nickel output at Onca Puma after furnace fault",
            material=world["nickel"], facility=world["facility"],
        )
        result = find_similar_events(session, [cand.id])[cand.id]
        # Checked (it has anchors) but genuinely clean — NOT the same as skipped.
        assert result == {"checked": True, "reason": None, "hints": []}

    def test_shared_material_but_different_story_is_not_hinted(self, session, world):
        _event(
            session, "Indonesia raises royalty rates on nickel smelter output",
            material=world["nickel"],
        )
        cand = _candidate(
            session, "Vale suspends nickel output at Onca Puma after furnace fault",
            material=world["nickel"], facility=world["facility"],
        )
        result = find_similar_events(session, [cand.id])[cand.id]
        assert result["checked"] is True
        assert result["hints"] == []

    def test_company_only_anchor_still_matches(self, session, world):
        """Exchange-feed candidates land company-only, with no material row —
        the xlsx report's (material AND geography) bucketing would miss them."""
        curated = _event(
            session, "Vale declares force majeure on nickel shipments",
            subtype="force_majeure", company=world["vale"], category="operational",
        )
        cand = _candidate(
            session, "Vale declares force majeure on nickel shipments from Brazil",
            company=world["vale"],
        )
        result = find_similar_events(session, [cand.id])[cand.id]
        assert [h["event_id"] for h in result["hints"]] == [curated.id]

    def test_date_incompatible_pairs_are_dropped(self, session, world):
        _event(
            session, "Vale suspends nickel production at Onca Puma mine",
            date=datetime(2019, 3, 1),
            material=world["nickel"], facility=world["facility"],
        )
        cand = _candidate(
            session, "Vale suspends nickel production at Onca Puma mine",
            date=datetime(2026, 7, 20),
            material=world["nickel"], facility=world["facility"],
        )
        result = find_similar_events(session, [cand.id])[cand.id]
        assert result["hints"] == []

    def test_limit_caps_hints_and_orders_by_similarity(self, session, world):
        for i in range(5):
            _event(
                session,
                f"Vale suspends nickel production at Onca Puma mine report {i}",
                material=world["nickel"], facility=world["facility"],
            )
        cand = _candidate(
            session, "Vale suspends nickel production at Onca Puma mine",
            material=world["nickel"], facility=world["facility"],
        )
        result = find_similar_events(session, [cand.id], limit=2)[cand.id]
        assert len(result["hints"]) == 2
        sims = [h["similarity"] for h in result["hints"]]
        assert sims == sorted(sims, reverse=True)


class TestHonestNegatives:
    def test_candidate_with_no_links_reports_unchecked(self, session, world):
        _event(
            session, "Vale suspends nickel production at Onca Puma mine",
            material=world["nickel"], facility=world["facility"],
        )
        cand = _candidate(
            session, "Vale suspends nickel production at Onca Puma mine",
        )  # deliberately no material/facility/company rows
        result = find_similar_events(session, [cand.id])[cand.id]
        assert result["checked"] is False
        assert result["reason"] == "no_material_facility_or_company_links"
        assert result["hints"] == []

    def test_unknown_event_id_reports_not_found(self, session, world):
        assert find_similar_events(session, [99999])[99999] == {
            "checked": False, "reason": "event_not_found", "hints": [],
        }

    def test_empty_input_is_a_no_op(self, session):
        assert find_similar_events(session, []) == {}


class TestStatusTagging:
    def test_rejected_neighbour_is_tagged_not_scoring(self, session, world):
        prior = _candidate(
            session, "Vale suspends nickel production at Onca Puma mine",
            material=world["nickel"], facility=world["facility"],
        )
        reject_candidate(session, prior.id, reason="Opinion piece, no new facts")
        cand = _candidate(
            session, "Vale suspends nickel production at Onca Puma mine again",
            material=world["nickel"], facility=world["facility"],
        )
        hit = next(
            h for h in find_similar_events(session, [cand.id])[cand.id]["hints"]
            if h["event_id"] == prior.id
        )
        assert hit["status"] == "rejected"
        assert hit["scores_already"] is False

    def test_pending_neighbour_is_tagged_pending(self, session, world):
        other = _candidate(
            session, "Vale suspends nickel production at Onca Puma mine",
            material=world["nickel"], facility=world["facility"],
        )
        cand = _candidate(
            session, "Vale suspends nickel production at Onca Puma mine today",
            material=world["nickel"], facility=world["facility"],
        )
        hit = next(
            h for h in find_similar_events(session, [cand.id])[cand.id]["hints"]
            if h["event_id"] == other.id
        )
        assert hit["status"] == "pending_triage"
        assert hit["scores_already"] is False

    def test_promoted_neighbour_flips_to_scoring(self, session, world):
        other = _candidate(
            session, "Vale suspends nickel production at Onca Puma mine",
            material=world["nickel"], facility=world["facility"],
        )
        promote_candidate(session, other.id, subtype="facility_shutdown")
        cand = _candidate(
            session, "Vale suspends nickel production at Onca Puma mine today",
            material=world["nickel"], facility=world["facility"],
        )
        hit = next(
            h for h in find_similar_events(session, [cand.id])[cand.id]["hints"]
            if h["event_id"] == other.id
        )
        assert hit["status"] == "scoring"
        assert hit["scores_already"] is True

    def test_already_marked_duplicates_are_excluded(self, session, world):
        canonical = _event(
            session, "Vale suspends nickel production at Onca Puma mine",
            material=world["nickel"], facility=world["facility"],
        )
        dup = _event(
            session, "Vale suspends nickel production at Onca Puma mine",
            material=world["nickel"], facility=world["facility"],
        )
        dup.duplicate_of_id = canonical.id
        session.flush()
        cand = _candidate(
            session, "Vale suspends nickel production at Onca Puma mine today",
            material=world["nickel"], facility=world["facility"],
        )
        ids = [h["event_id"] for h in find_similar_events(session, [cand.id])[cand.id]["hints"]]
        assert canonical.id in ids
        assert dup.id not in ids


class TestQueueIntegration:
    def test_queue_attaches_hints_without_filtering(self, session, world):
        curated = _event(
            session, "Vale suspends nickel production at Onca Puma mine",
            subtype="facility_shutdown",
            material=world["nickel"], facility=world["facility"],
        )
        cand = _candidate(
            session, "Vale suspends nickel output at Onca Puma mine",
            material=world["nickel"], facility=world["facility"],
        )
        rows = list_pending_candidates(session)
        # Advisory only: the row is still in the queue.
        assert [r["id"] for r in rows] == [cand.id]
        hints = rows[0]["duplicate_hints"]
        assert hints["checked"] is True
        assert curated.id in [h["event_id"] for h in hints["hints"]]

    def test_check_duplicates_false_omits_the_block(self, session, world):
        _candidate(
            session, "Vale suspends nickel output at Onca Puma mine",
            material=world["nickel"], facility=world["facility"],
        )
        rows = list_pending_candidates(session, check_duplicates=False)
        assert "duplicate_hints" not in rows[0]

    def test_queue_row_keys_are_stable(self, session, world):
        _candidate(session, "Some halt", material=world["nickel"])
        row = list_pending_candidates(session)[0]
        assert set(row) == {
            "id", "date", "title", "feed", "suggested_subtype",
            "linked_facilities", "candidate_facilities", "url",
            "duplicate_hints",
        }
