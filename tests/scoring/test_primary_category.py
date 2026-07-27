"""Build 1 (migration 062): single primary scoring category per event.

Covers three layers of the fix for cross-pillar double-counting:

1. ``derive_primary_category`` — pure precedence function.
2. The ``before_insert`` autofill listener on ``RiskEvent`` — new events
   get a primary category derived from their display tags unless they are
   display-only (``sec_filing_signal`` event type, or
   ``metadata_json.scoring == "display_only"``) or explicitly set.
3. ``evidence_query`` regression — a multi-tagged geo+reg event is
   selected by exactly ONE pillar (its primary), never both.  Before
   Build 1 the ``risk_categories_json.contains()`` filter returned the
   same event to every pillar it was tagged with.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from app.constants import (
    PRIMARY_CATEGORY_PRECEDENCE,
    RiskCategory,
    derive_primary_category,
)
from app.models.regulatory import RiskEvent, RiskEventMaterial
from app.models.supply import Material
from app.services.scoring.evidence_query import get_events_for_material

AS_OF = date(2026, 7, 1)
EVENT_DATE = datetime(2026, 6, 15, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# 1. Pure precedence function
# ---------------------------------------------------------------------------

class TestDerivePrimaryCategory:
    def test_single_tag(self):
        assert derive_primary_category(["regulatory_compliance"]) == "regulatory_compliance"

    def test_operational_beats_everything(self):
        cats = [
            "material_concentration", "financial_pressure",
            "regulatory_compliance", "geopolitical_trade", "operational",
        ]
        assert derive_primary_category(cats) == "operational"

    def test_geopolitical_beats_regulatory(self):
        # The sanctions case that motivated Build 1: events tagged both
        # geo + reg were counted by BOTH pillars.  Precedence says the
        # geopolitical pillar owns them.
        cats = ["regulatory_compliance", "geopolitical_trade"]
        assert derive_primary_category(cats) == "geopolitical_trade"

    def test_empty_and_none(self):
        assert derive_primary_category([]) is None
        assert derive_primary_category(None) is None

    def test_unknown_only(self):
        assert derive_primary_category(["not_a_category"]) is None

    def test_precedence_covers_all_pillars(self):
        assert set(PRIMARY_CATEGORY_PRECEDENCE) == {c.value for c in RiskCategory}


# ---------------------------------------------------------------------------
# 2. before_insert autofill listener
# ---------------------------------------------------------------------------

def _make_event(**overrides) -> RiskEvent:
    kwargs = dict(
        event_type="MANUAL",
        title="Test event",
        event_date=EVENT_DATE,
        risk_categories_json=["regulatory_compliance"],
    )
    kwargs.update(overrides)
    return RiskEvent(**kwargs)


class TestAutofillListener:
    def test_single_tag_autofill(self, sqlite_session):
        ev = _make_event(risk_categories_json=["operational"])
        sqlite_session.add(ev)
        sqlite_session.commit()
        assert ev.primary_category == "operational"

    def test_multi_tag_uses_precedence(self, sqlite_session):
        ev = _make_event(
            risk_categories_json=["regulatory_compliance", "geopolitical_trade"],
        )
        sqlite_session.add(ev)
        sqlite_session.commit()
        assert ev.primary_category == "geopolitical_trade"

    def test_no_tags_stays_null(self, sqlite_session):
        ev = _make_event(risk_categories_json=[])
        sqlite_session.add(ev)
        sqlite_session.commit()
        assert ev.primary_category is None

    def test_sec_filing_signal_skipped(self, sqlite_session):
        # SEC events are quarantined display-only pending link triage.
        ev = _make_event(
            event_type="sec_filing_signal",
            risk_categories_json=["financial_pressure"],
        )
        sqlite_session.add(ev)
        sqlite_session.commit()
        assert ev.primary_category is None

    def test_metadata_display_only_skipped(self, sqlite_session):
        # Derived trade-signal stats set metadata scoring=display_only.
        ev = _make_event(
            event_type="GEOPOLITICAL_TRADE",
            risk_categories_json=["geopolitical_trade"],
            metadata_json={"scoring": "display_only"},
        )
        sqlite_session.add(ev)
        sqlite_session.commit()
        assert ev.primary_category is None

    def test_explicit_value_respected(self, sqlite_session):
        # Curation may deliberately override precedence.
        ev = _make_event(
            risk_categories_json=["geopolitical_trade", "regulatory_compliance"],
            primary_category="regulatory_compliance",
        )
        sqlite_session.add(ev)
        sqlite_session.commit()
        assert ev.primary_category == "regulatory_compliance"


# ---------------------------------------------------------------------------
# 3. Evidence-query double-count regression
# ---------------------------------------------------------------------------

class TestEvidenceQueryDoubleCount:
    def test_multi_tagged_event_scored_by_one_pillar_only(self, sqlite_session):
        m = Material(canonical_name="Cobalt")
        sqlite_session.add(m)
        sqlite_session.flush()

        ev = _make_event(
            title="Sanctions on refiner",
            risk_categories_json=["geopolitical_trade", "regulatory_compliance"],
            severity_score=0.8,
        )
        sqlite_session.add(ev)
        sqlite_session.flush()
        assert ev.primary_category == "geopolitical_trade"

        sqlite_session.add(RiskEventMaterial(
            risk_event_id=ev.id, material_id=m.id, relevance_score=1.0,
        ))
        sqlite_session.commit()

        geo = get_events_for_material(
            sqlite_session, m.id, RiskCategory.GEOPOLITICAL_TRADE, AS_OF,
        )
        reg = get_events_for_material(
            sqlite_session, m.id, RiskCategory.REGULATORY_COMPLIANCE, AS_OF,
        )
        assert [e.event.id for e in geo] == [ev.id]
        assert reg == []  # pre-Build-1 this ALSO returned the event

    def test_display_only_event_scored_by_no_pillar(self, sqlite_session):
        m = Material(canonical_name="Cobalt")
        sqlite_session.add(m)
        sqlite_session.flush()

        ev = _make_event(
            event_type="GEOPOLITICAL_TRADE",
            risk_categories_json=["geopolitical_trade"],
            metadata_json={"scoring": "display_only"},
        )
        sqlite_session.add(ev)
        sqlite_session.flush()

        sqlite_session.add(RiskEventMaterial(
            risk_event_id=ev.id, material_id=m.id, relevance_score=1.0,
        ))
        sqlite_session.commit()

        geo = get_events_for_material(
            sqlite_session, m.id, RiskCategory.GEOPOLITICAL_TRADE, AS_OF,
        )
        assert geo == []
